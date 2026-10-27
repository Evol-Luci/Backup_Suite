"""Schema-3 upgrades preserve frozen schema-2 stores and historical replay."""
from contextlib import closing, contextmanager
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import test_catalog
import test_task_schema
from test_task_schema import create_v1
from solace_core.catalog import ProjectCatalog, readonly_database
from solace_core.errors import CoreError
from solace_core.protocol import fingerprint
from solace_core.tasks import TaskCatalog

# Exported verbatim from 6af0b2c; these fixtures never call current v2 DDL.
V2 = """
CREATE TABLE source_head (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 checkpoint_id TEXT REFERENCES checkpoints(id)
);
CREATE TABLE project_prompt_policy (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 revision INTEGER NOT NULL CHECK(revision>=1),
 policy TEXT NOT NULL CHECK(policy IN ('days','retain','none')),
 days INTEGER,
 CHECK((policy='days' AND days IS NOT NULL AND days>0) OR (policy<>'days' AND days IS NULL))
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY,
 parent_id TEXT REFERENCES tasks(id),
 title TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('created','active','blocked','ready','cancelled')),
 revision INTEGER NOT NULL CHECK(revision>=1),
 profile_json TEXT NOT NULL,
 block_reason TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX tasks_parent ON tasks(parent_id);
CREATE TABLE task_prompts (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 policy TEXT NOT NULL CHECK(policy IN ('days','retain','none')),
 days INTEGER,
 status TEXT NOT NULL CHECK(status IN ('missing','available','discarded','deleted','expired')),
 body TEXT,
 expires_at TEXT,
 redacted_at TEXT,
 CHECK((policy='days' AND days IS NOT NULL AND days>0) OR (policy<>'days' AND days IS NULL)),
 CHECK((status='available' AND body IS NOT NULL AND policy<>'none')
    OR (status<>'available' AND body IS NULL))
);
CREATE TABLE workspaces (
 id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
 path TEXT NOT NULL UNIQUE,
 mount_id INTEGER NOT NULL, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 base_checkpoint TEXT NOT NULL REFERENCES checkpoints(id),
 head_checkpoint TEXT NOT NULL REFERENCES checkpoints(id),
 observed_at TEXT, observed_json TEXT
);
CREATE TABLE checkpoint_context (
 checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
 purpose TEXT NOT NULL CHECK(purpose IN ('source','task_base','child_base','workspace','ready')),
 task_id TEXT REFERENCES tasks(id),
 workspace_id TEXT,
 profile_json TEXT NOT NULL,
 published INTEGER NOT NULL CHECK(published IN (0,1))
);
CREATE TABLE task_checkpoint_refs (
 task_id TEXT NOT NULL REFERENCES tasks(id),
 checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
 role TEXT NOT NULL CHECK(role IN ('base','head','result')),
 created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,checkpoint_id,role)
);
CREATE TABLE task_requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE task_claims (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 operation_id TEXT NOT NULL REFERENCES operations(id)
);
CREATE TABLE workspace_intents (
 operation_id TEXT PRIMARY KEY REFERENCES operations(id),
 task_id TEXT NOT NULL REFERENCES tasks(id), workspace_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('start','relink')),
 phase TEXT NOT NULL CHECK(phase IN ('reserved','staged','published','committed','abandoned')),
 staging_path TEXT, final_path TEXT NOT NULL,
 staging_identity_json TEXT, final_identity_json TEXT,
 base_checkpoint TEXT REFERENCES checkpoints(id),
 expected_json TEXT NOT NULL,
 recovery_operation_id TEXT REFERENCES operations(id)
);
CREATE UNIQUE INDEX workspace_intents_live ON workspace_intents(workspace_id)
 WHERE phase NOT IN ('committed','abandoned');
CREATE TABLE workspace_caches (
 checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id),
 cache_id TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
 identity_json TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 logical_bytes INTEGER NOT NULL CHECK(logical_bytes>=0),
 created_at TEXT NOT NULL
);

"""
V2_VALIDATOR = """def validate(self, *, require_root=True):
        r = self.registration
        root = Path(r['root'])
        if require_root and (root.is_symlink() or not root.is_dir()):
            raise CoreError('PROJECT_UNAVAILABLE', 'The source directory is unavailable.', 'Explicitly relink the source root.')
        if self.store.is_symlink() or not self.store.is_dir():
            raise CoreError('STORE_UNAVAILABLE', 'The configured store is unavailable.', 'Reconnect storage or explicitly relink.')
        location = self.policy.check(self.store)
        if location.mount_id != r['mount_id']:
            raise CoreError('STORE_UNAVAILABLE', 'The configured store mount changed.', 'Explicitly relink to validate the store on its current mount.')
        if self.store == root or root.is_relative_to(self.store):
            raise CoreError('UNSUPPORTED_STORAGE', 'The store cannot contain its source root.', 'Explicitly relink to a separate source root.')
        try:
            with readonly_database(self.database) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported checkpoint schema.', 'Use a compatible Solace version; preserve this store.')
                row = db.execute('SELECT schema_version, project_id, store_uuid FROM core_metadata WHERE singleton=1').fetchone()
                if row is None or row[0] not in (1, 2):
                    raise CoreError('SCHEMA_VERSION_UNSUPPORTED', 'Unsupported core schema.', 'Use a compatible Solace version; preserve this store.')
                if row[1:] != (r['id'], r['store_uuid']):
                    raise CoreError('STORE_IDENTITY_MISMATCH', 'This store belongs to a different project.', 'Select the original store; no data has been replaced.')
            objects = self.store / 'objects'
            if objects.is_symlink() or not objects.is_dir():
                raise CoreError('STORE_UNAVAILABLE', 'The objects directory is unavailable.', 'Preserve the store and inspect it with doctor.')
        except sqlite3.DatabaseError as error:
            raise classify(error) from error
        return location
"""


def create_v2(registration, policy):
    catalog = create_v1(registration, policy)
    with catalog.connect(write=True) as db:
        for statement in V2.split(';'):
            if statement.strip():
                db.execute(statement)
        db.execute('INSERT INTO source_head VALUES (1,NULL)')
        db.execute("INSERT INTO project_prompt_policy VALUES (1,1,'days',30)")
        db.execute('UPDATE core_metadata SET schema_version=2')
    return catalog


class IntegrationSchemaTests(unittest.TestCase):
    request = test_task_schema.SchemaTests.request

    def setUp(self):
        test_catalog.CatalogTests.setUp(self)
        self.catalog = create_v2(self.registration, self.policy)

    def upgrade(self):
        from solace_core.schema import apply_upgrade, preview_upgrade
        preview = preview_upgrade(self.catalog)
        request = self.request()
        request['params']['plan_token'] = preview['plan_token']
        return apply_upgrade(self.catalog, preview['plan_token'], request), request

    def assert_version(self, version):
        from solace_core.schema import schema_version
        self.assertEqual(schema_version(self.catalog), version)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def seed_historical_v2_upgrade_receipt(self):
        request = self.request()
        request['params']['plan_token'] = 'a' * 64
        result = {"schema_version": 2, "backup": "/historical/backup"}
        signature = fingerprint('project.upgrade', request['params'], 'test')
        self.assertEqual(signature, '8d4f5adcb8bb18373740839ca27a393039d0a2dde9631e173738f094509bfdb9')
        with self.catalog.connect(write=True) as db:
            db.execute('INSERT INTO task_requests VALUES (?,?,?,?)',
                       (request['id'], signature, json.dumps(result), 'historical'))
        return request, result

    def test_v2_to_v3_keeps_original_upgrade_receipt(self):
        from solace_core.schema import apply_upgrade, preview_upgrade, schema_version
        old_request, old_result = self.seed_historical_v2_upgrade_receipt()
        preview = preview_upgrade(self.catalog)
        request = self.request()
        request['params']['plan_token'] = preview['plan_token']
        result = apply_upgrade(self.catalog, preview['plan_token'], request)
        self.assertEqual(schema_version(self.catalog), 3)
        self.assertEqual(apply_upgrade(self.catalog, old_request['params']['plan_token'],
                                      old_request), old_result)
        with readonly_database(Path(result['backup'])) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0], 2)

    def test_original_receipt_precedes_preview_and_root_availability(self):
        from solace_core import schema
        request, result = self.seed_historical_v2_upgrade_receipt()
        self.upgrade()
        self.project.rename(self.base / 'source-offline')
        with patch.object(schema, 'preview_upgrade', side_effect=AssertionError('must replay first')):
            self.assertEqual(schema.apply_upgrade(self.catalog, 'a' * 64, request), result)
        self.assertEqual(len(list((self.store / 'schema-backups').glob('*/checkpoints.sqlite3'))), 1)
        request['actor'] = 'different'
        with self.assertRaises(CoreError) as caught:
            schema.apply_upgrade(self.catalog, 'a' * 64, request)
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')

    def test_operation_request_namespace_checked_before_preview(self):
        from solace_core import schema
        request = self.request()
        self.catalog.accept_operation(request)
        request['params']['plan_token'] = 'a' * 64
        with patch.object(schema, 'preview_upgrade', side_effect=AssertionError('must check ID first')):
            with self.assertRaises(CoreError) as caught:
                schema.apply_upgrade(self.catalog, 'a' * 64, request)
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')
        self.assertFalse((self.store / 'schema-backups').exists())

    def test_schema_two_tasks_need_no_upgrade(self):
        request = dict(id=str(uuid.uuid4()), method='task.create', actor='test',
                       params=dict(project='p', title='v2 task', prompt='private body'))
        tasks = TaskCatalog(self.catalog)
        result = tasks.create(request)
        self.assertEqual(tasks.create(request), result)
        self.assertEqual(tasks.show(result['task_id'])['state'], 'created')
        self.assertEqual(self.catalog.find_operation(request), result)
        self.assert_version(2)

    def seed_source_and_task(self):
        from solace_core.lineage import Lineage
        repo = self.catalog.open_existing()
        (self.project / 'a').write_text('source')
        first = repo.create_checkpoint('source', publication_hook=lambda db, cp: Lineage(self.catalog).publish_source(db, cp, []))
        (self.project / 'a').write_text('task checkpoint')
        repo.create_checkpoint('global latest is not source', parent_id=first.id)
        task = TaskCatalog(self.catalog).create(dict(id=str(uuid.uuid4()), method='task.create', actor='test',
             params=dict(project='p', title='retained task', prompt='PROMPT CANARY', prompt_policy='retain')))
        return first.id, task['task_id']

    def test_upgrade_keeps_explicit_source_head_tasks_and_prompts(self):
        from solace_core.schema import preview_upgrade
        source, task = self.seed_source_and_task()
        tables = ('source_head', 'tasks', 'task_prompts', 'checkpoint_context', 'project_prompt_policy')
        with self.catalog.connect() as db:
            before = {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in tables}
        preview = preview_upgrade(self.catalog)
        self.assertEqual(preview['snapshot']['source_head'], source)
        result, request = self.upgrade()
        self.assertEqual(result['schema_version'], 3)
        self.assert_version(3)
        with self.catalog.connect() as db:
            self.assertEqual(before, {name: [tuple(r) for r in db.execute('SELECT * FROM ' + name)] for name in tables})
        self.assertEqual(TaskCatalog(self.catalog).show(task)['state'], 'created')
        self.assertEqual(self.catalog.find_operation(request), result)

    def test_preview_is_read_only_and_new_stores_are_schema_three(self):
        from solace_core.schema import preview_upgrade, require_integrations
        before = {p.relative_to(self.store): p.read_bytes() for p in self.store.rglob('*') if p.is_file()}
        preview = preview_upgrade(self.catalog)
        self.assertTrue(preview['required'])
        self.assertEqual((preview['from_schema'], preview['to_schema']), (2, 3))
        self.assertEqual(before, {p.relative_to(self.store): p.read_bytes() for p in self.store.rglob('*') if p.is_file()})
        with self.assertRaises(CoreError) as caught:
            require_integrations(self.catalog)
        self.assertEqual(caught.exception.code, 'PROJECT_UPGRADE_REQUIRED')
        self.registration['store'] = str(self.base / 'new-store')
        self.catalog = ProjectCatalog.create(self.registration, self.policy)
        self.assert_version(3)
        require_integrations(self.catalog)
        self.assertFalse(preview_upgrade(self.catalog)['required'])
        with self.catalog.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'integration_%'").fetchone()[0], 11)

    def test_direct_v1_to_v3_keeps_two_checkpoints(self):
        self.registration['store'] = str(self.base / 'v1-store')
        self.catalog = create_v1(self.registration, self.policy)
        repo = self.catalog.open_existing()
        (self.project / 'a').write_bytes(b'\0first')
        first = repo.create_checkpoint('first')
        second = repo.create_checkpoint('second')
        result, request = self.upgrade()
        self.assert_version(3)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT checkpoint_id FROM source_head').fetchone()[0], second.id)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoint_context').fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='project.upgraded'").fetchone()[0], 1)
        with readonly_database(Path(result['backup'])) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 2)
        self.assertTrue(repo.verify_checkpoint(first.id).ok)

    def test_failed_v3_ddl_rolls_back_both_upgrade_steps(self):
        from solace_core import schema
        for version in (1, 2):
            with self.subTest(version=version):
                self.registration['store'] = str(self.base / ('failure-' + str(version)))
                self.catalog = (create_v1 if version == 1 else create_v2)(self.registration, self.policy)
                preview = schema.preview_upgrade(self.catalog)
                create_v3 = schema.create_schema_v3
                def fail(db):
                    create_v3(db)
                    raise RuntimeError('injected DDL failure')
                with patch.object(schema, 'create_schema_v3', side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError, 'injected DDL'):
                        schema.apply_upgrade(self.catalog, preview['plan_token'], self.request())
                self.assert_version(version)
                with self.catalog.connect() as db:
                    self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='integration_plans'").fetchone())
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
                    if version == 1:
                        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='source_head'").fetchone())
                    else:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM task_requests').fetchone()[0], 0)

    def test_backup_is_consistent_private_and_contains_original_rows(self):
        self.seed_source_and_task()
        with self.catalog.connect() as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM task_prompts')]
        result, request = self.upgrade()
        path = Path(result['backup'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
        with readonly_database(path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT * FROM task_prompts').fetchall(), before)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_backup_verification_failure_leaves_catalog_unchanged(self):
        from solace_core import schema
        preview = schema.preview_upgrade(self.catalog)
        @contextmanager
        def corrupt_backup(path):
            if Path(path).parent.parent == self.store / 'schema-backups':
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute('UPDATE core_metadata SET schema_version=99')
            with readonly_database(path) as db:
                yield db
        with patch.object(schema, 'readonly_database', side_effect=corrupt_backup):
            with self.assertRaises(CoreError) as caught:
                schema.apply_upgrade(self.catalog, preview['plan_token'], self.request())
        self.assertEqual(caught.exception.code, 'DATABASE_CORRUPT')
        self.assert_version(2)
        with self.catalog.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='integration_plans'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)

    def test_stale_preview_and_busy_upgrade_do_not_create_backup(self):
        from solace_core.schema import preview_upgrade, apply_upgrade
        preview = preview_upgrade(self.catalog)
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE source_head SET checkpoint_id=NULL')
            self.catalog._event(db, dict(id=None, actor='test'), 'changed', 'succeeded', {})
        with self.assertRaises(CoreError) as caught:
            apply_upgrade(self.catalog, preview['plan_token'], self.request())
        self.assertEqual(caught.exception.code, 'UPGRADE_PREVIEW_STALE')
        self.catalog.accept_operation(dict(id=str(uuid.uuid4()), method='checkpoint.create', params={}, actor='test'))
        preview = preview_upgrade(self.catalog)
        with self.assertRaises(CoreError) as caught:
            apply_upgrade(self.catalog, preview['plan_token'], self.request())
        self.assertEqual(caught.exception.code, 'PROJECT_BUSY')
        self.assertFalse((self.store / 'schema-backups').exists())

    def test_original_v2_validator_refuses_v3_in_subprocess(self):
        self.upgrade()
        script = 'import json, pathlib, sqlite3, sys\nfrom pathlib import Path\nfrom solace_core.catalog import ProjectCatalog, readonly_database\nfrom solace_core.errors import CoreError, classify\nfrom solace_core.storage import StoragePolicy\n' + V2_VALIDATOR + "\n# Override in this disposable subprocess only, using exported historical source.\nProjectCatalog.validate = validate\ncatalog = ProjectCatalog(json.loads(sys.argv[1]), StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\\n', sync_roots=[]))\ntry:\n    catalog.validate()\nexcept CoreError as error:\n    print(error.code)\nelse:\n    raise SystemExit('historical runtime accepted schema 3')\n"
        result = subprocess.run([sys.executable, '-c', script, json.dumps(self.registration)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'SCHEMA_VERSION_UNSUPPORTED')
        self.assert_version(3)

    def test_v3_ddl_enforces_receipt_immutability_and_relations(self):
        self.upgrade()
        source, task = self.seed_source_and_task()
        operation = self.catalog.accept_operation(dict(id=str(uuid.uuid4()), method='checkpoint.create', params={}, actor='test'))['operation_id']
        plan, receipt = 'b' * 32, 'c' * 32
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO integration_plans VALUES (?,?,?,'source',NULL,'prepared',1,'now','now')", (plan, task, source))
            db.execute("INSERT INTO integration_revisions VALUES (?,1,?,?,?,?,NULL,'{}','{}',? ,?,'now','now')", (plan, source, source, source, source, 'a' * 64, 'd' * 64))
            db.execute("INSERT INTO integration_receipts VALUES (?,?,1,?,?,'source',?,?,?,'{}','now')", (receipt, plan, task, source, operation, source, source))
        for query in ("UPDATE integration_receipts SET details_json='[]'", 'DELETE FROM integration_receipts'):
            with self.subTest(query=query):
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                    with self.catalog.connect(write=True) as db:
                        db.execute(query)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.catalog.connect(write=True) as db:
                db.execute("INSERT INTO integration_refs VALUES ('missing','owner','base','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE integration_plans SET state='ready'")
        self.assert_version(3)

    def test_shared_records_are_frozen_and_keep_engine_entries(self):
        from solace_core.integration_types import Identity, Version, MergeConflict, MergeOutcome, Target, PreparedPlan, Journal
        from solace_engine.models import FileEntry
        identity = Identity('/tmp/target', 1, 2, 3)
        version = Version('a' * 32, 'b' * 64, '{}')
        entry = FileEntry('a', 'file', 0o644, 1, 'c' * 64, ())
        conflict = MergeConflict('a', 'both changed', None, entry, entry)
        target = Target('source', None, None, None, identity)
        plan = PreparedPlan('d' * 32, 1, 'conflicted', 'e' * 32, 'a' * 32,
                            target, version, version, version, version, (conflict,))
        journal = Journal('f' * 32, plan.id, 1, Path('/tmp/journal'), identity, identity)
        self.assertIs(MergeOutcome((entry,), (conflict,)).entries[0], entry)
        self.assertEqual(journal.plan_id, plan.id)
        with self.assertRaises(FrozenInstanceError):
            identity.inode = 4
