"""Upgrade preview, backup, rollback and version compatibility."""
import json
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import test_catalog
from solace_core.catalog import ProjectCatalog, readonly_database
from solace_core.errors import CoreError
from solace_engine.repository import ProjectRepository

# Frozen core version 1, independent of current creation code.
V1='''CREATE TABLE core_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL,project_id TEXT NOT NULL,store_uuid TEXT NOT NULL);
CREATE TABLE operations(id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL,kind TEXT NOT NULL,actor TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,result_json TEXT,error_json TEXT,params_json TEXT NOT NULL);
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,project_id TEXT NOT NULL,operation_id TEXT,actor TEXT NOT NULL,created_at TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,details_json TEXT NOT NULL);'''

def create_v1(registration,policy):
    root,store=Path(registration['root']),Path(registration['store'])
    repo=ProjectRepository(root,store)
    (store/'objects').mkdir(mode=0o700,exist_ok=True)
    store.chmod(0o700)
    with repo._connect() as db:
        db.executescript(V1)
        db.execute('INSERT INTO core_metadata VALUES (1,1,?,?)',(registration['id'],registration['store_uuid']))
    (store/'checkpoints.sqlite3').chmod(0o600)
    return ProjectCatalog(registration,policy)

class SchemaTests(unittest.TestCase):
    setUp=test_catalog.CatalogTests.setUp

    def request(self):
        return dict(id=str(uuid.uuid4()),method='project.upgrade',params={'project':'p','preview':False},actor='test')

    def test_new_store_and_explicit_upgrade_backup_replay(self):
        from solace_core.schema import schema_version,preview_upgrade,apply_upgrade
        catalog=create_v1(self.registration,self.policy)
        repo=catalog.open_existing();(self.project/'a').write_bytes(b'\0immutable')
        first=repo.create_checkpoint('one');second=repo.create_checkpoint('two')
        before={p.relative_to(self.store):p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        preview=preview_upgrade(catalog)
        self.assertEqual(before,{p.relative_to(self.store):p.read_bytes() for p in self.store.rglob('*') if p.is_file()})
        request=self.request();request['params']['plan_token']=preview['plan_token']
        outcome=apply_upgrade(catalog,preview['plan_token'],request)
        self.assertEqual(schema_version(catalog),3)
        self.assertEqual(apply_upgrade(catalog,preview['plan_token'],request),outcome)
        with readonly_database(Path(outcome['backup'])) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],2)
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT checkpoint_id FROM source_head').fetchone()[0],second.id)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='project.upgraded'").fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
        self.assertTrue(repo.verify_checkpoint(first.id).ok)

    def test_failed_upgrade_rolls_back_tables_and_version(self):
        from solace_core import schema
        catalog=create_v1(self.registration,self.policy)
        preview=schema.preview_upgrade(catalog)
        def fail(db):
            db.execute('CREATE TABLE source_head(singleton INTEGER PRIMARY KEY, checkpoint_id TEXT)')
            raise RuntimeError('injected migration failure')
        with patch.object(schema,'create_schema_v2',side_effect=fail):
            with self.assertRaises(RuntimeError): schema.apply_upgrade(catalog,preview['plan_token'],self.request())
        self.assertEqual(schema.schema_version(catalog),1)
        with catalog.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='source_head'").fetchone())
        self.assertEqual(len(list((self.store/'schema-backups').glob('*/checkpoints.sqlite3'))),1)

    def test_stale_upgrade_creates_no_backup(self):
        from solace_core.schema import preview_upgrade,apply_upgrade
        catalog=create_v1(self.registration,self.policy);preview=preview_upgrade(catalog)
        with catalog.connect(write=True) as db:
            db.execute("INSERT INTO events(project_id,actor,created_at,kind,state,details_json) VALUES ('p','test','now','changed','succeeded','{}')")
        with self.assertRaises(CoreError) as caught: apply_upgrade(catalog,preview['plan_token'],self.request())
        self.assertEqual(caught.exception.code,'UPGRADE_PREVIEW_STALE')
        self.assertFalse((self.store/'schema-backups').exists())

    def test_new_store_uses_v5_and_source_head_is_independent(self):
        from solace_core.schema import schema_version
        from solace_core.lineage import Lineage
        catalog=ProjectCatalog.create(self.registration,self.policy)
        self.assertEqual(schema_version(catalog),5)
        repo=catalog.open_existing();lineage=Lineage(catalog)
        (self.project/'a').write_bytes(b'abc')
        source=repo.create_checkpoint('source',parent_id=None,publication_hook=lambda db,cp:lineage.publish_source(db,cp,[]))
        (self.project/'a').write_bytes(b'longer task data')
        repo.create_checkpoint('not source',parent_id=source.id)
        self.assertEqual(lineage.source_head(),source.id)
        self.assertEqual([cp['id'] for cp in lineage.published()],[source.id])
        self.assertEqual(lineage.source_usage()['logical_bytes'],3)
        self.assertIsNone(lineage.source_usage()['reclaimable_bytes'])
