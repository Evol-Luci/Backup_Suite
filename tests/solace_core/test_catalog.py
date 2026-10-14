import os
from contextlib import closing
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from solace_core.catalog import ProjectCatalog
from solace_core.errors import CoreError
from solace_core.storage import StoragePolicy
from solace_engine.repository import ProjectRepository
from solace_engine.object_store import ObjectStore
from solace_engine.errors import InvalidProjectPath


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.store = self.base / 'store'
        self.policy = StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\n', sync_roots=[])
        self.registration = dict(id='p', root=str(self.project), store=str(self.store), store_uuid='s', mount_id=1)

    def create(self):
        return ProjectCatalog.create(self.registration, self.policy)

    def test_create_and_existing_open_without_capture(self):
        catalog = self.create()
        repo = catalog.open_existing()
        self.assertEqual(repo.list_checkpoints(), [])
        self.assertTrue((self.store / 'objects').is_dir())
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual((self.store / 'checkpoints.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_missing_store_never_initialized(self):
        with self.assertRaises(InvalidProjectPath):
            ProjectRepository.open_existing(self.project, self.store)
        self.assertFalse(self.store.exists())
        self.create()
        shutil.rmtree(self.store)
        with self.assertRaises(CoreError) as caught:
            ProjectCatalog(self.registration, self.policy).open_existing()
        self.assertEqual(caught.exception.code, 'STORE_UNAVAILABLE')
        self.assertFalse(self.store.exists())

    def test_identity_schema_and_corruption_block_open(self):
        self.create()
        for query, code in [("UPDATE core_metadata SET store_uuid='wrong'", 'STORE_IDENTITY_MISMATCH'),
                            ('UPDATE core_metadata SET schema_version=2', 'SCHEMA_VERSION_UNSUPPORTED'),
                            ('PRAGMA user_version=2', 'SCHEMA_VERSION_UNSUPPORTED')]:
            with self.subTest(query=query):
                with closing(sqlite3.connect(self.store / 'checkpoints.sqlite3')) as db, db:
                    db.execute(query)
                with self.assertRaises(CoreError) as caught:
                    ProjectCatalog(self.registration, self.policy).open_existing()
                self.assertEqual(caught.exception.code, code)
                with closing(sqlite3.connect(self.store / 'checkpoints.sqlite3')) as db, db:
                    db.execute("UPDATE core_metadata SET store_uuid='s', schema_version=1")
                    db.execute('PRAGMA user_version=1')
        (self.store / 'checkpoints.sqlite3').write_bytes(b'corrupt')
        with self.assertRaises(CoreError) as caught:
            ProjectCatalog(self.registration, self.policy).open_existing()
        self.assertEqual(caught.exception.code, 'DATABASE_CORRUPT')

    def test_objects_disappearance_never_recreates_store(self):
        repo = self.create().open_existing()
        (self.project / 'a').write_text('data')
        shutil.rmtree(self.store)
        with self.assertRaises((OSError, InvalidProjectPath)):
            repo.create_checkpoint('gone')
        self.assertFalse(self.store.exists())

    def test_store_replacement_prevents_publication(self):
        repo = self.create().open_existing()
        original = repo._scan
        def replace(exclusions):
            entries = original(exclusions)
            self.store.rename(self.base / 'old')
            shutil.copytree(self.base / 'old', self.store)
            return entries
        with patch.object(repo, '_scan', side_effect=replace):
            with self.assertRaises(InvalidProjectPath):
                repo.create_checkpoint('bad')
        with closing(sqlite3.connect(self.store / 'checkpoints.sqlite3')) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 0)

    def test_missing_objects_symlink_or_remount_refused(self):
        catalog = self.create()
        (self.store / 'objects').rmdir()
        with self.assertRaises(CoreError):
            catalog.open_existing()
        (self.store / 'objects').mkdir()
        remount = StoragePolicy(mountinfo='2 0 0:1 / / rw - ext4 x rw\n', sync_roots=[])
        with self.assertRaises(CoreError):
            ProjectCatalog(self.registration, remount).open_existing()
        self.store.rename(self.base / 'old')
        self.store.symlink_to(self.base / 'old', target_is_directory=True)
        with self.assertRaises(CoreError):
            catalog.open_existing()

    def test_source_root_replacement_prevents_publication(self):
        repo=self.create().open_existing()
        original=repo._scan
        def replace(exclusions):
            result=original(exclusions)
            self.project.rename(self.base/'old-source')
            self.project.mkdir()
            return result
        with patch.object(repo,'_scan',side_effect=replace):
            with self.assertRaises(InvalidProjectPath): repo.create_checkpoint('mixed identity')
        with closing(sqlite3.connect(self.store/'checkpoints.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],0)
