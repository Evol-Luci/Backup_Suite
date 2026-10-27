from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from solace_core.registry import Registry
from solace_core.errors import CoreError
from solace_core.storage import StoragePolicy
from solace_core.catalog import ProjectCatalog
from solace_engine.repository import ProjectRepository


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.project.mkdir()
        self.policy = StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\n', sync_roots=[])
        self.registry = Registry(self.base / 'data/registry.sqlite3', self.policy)
        self.req = str(uuid.uuid4())

    def add(self):
        return self.registry.add(self.project, None, self.req, 'test')

    def test_add_no_scan_duplicate_alias_and_retry(self):
        with patch.object(ProjectRepository, '_scan', side_effect=AssertionError('scan')):
            record = self.add()
        self.assertEqual(self.add(), record)
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(ProjectCatalog(record, self.policy).open_existing().list_checkpoints(), [])
        alias = self.base / 'alias'
        alias.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(CoreError) as caught:
            self.registry.add(alias, None, str(uuid.uuid4()), 'test')
        self.assertEqual(caught.exception.code, 'PROJECT_ALREADY_REGISTERED')
        with self.assertRaises(CoreError) as caught:
            self.registry.add(self.project, None, self.req, 'different')
        self.assertEqual(caught.exception.code, 'REQUEST_ID_REUSED')

    def test_unregister_preserves_store_and_retry_after_reregister(self):
        record = self.add()
        req = str(uuid.uuid4())
        result = self.registry.unregister(record['id'], req, 'test')
        self.assertTrue(Path(record['store']).is_dir())
        self.assertEqual(self.registry.list(), [])
        self.assertEqual(self.registry.unregister(record['id'], req, 'test'), result)
        newer = self.registry.add(self.project, None, str(uuid.uuid4()), 'test')
        self.assertNotEqual(newer['id'], record['id'])
        self.assertEqual(self.add(), record)

    def test_missing_store_reported_without_replacement(self):
        record = self.add()
        shutil.rmtree(record['store'])
        shown = self.registry.show(record['id'])
        self.assertFalse(shown['available'])
        self.assertEqual(shown['error']['code'], 'STORE_UNAVAILABLE')
        self.assertFalse(Path(record['store']).exists())

    def test_confirmed_relink_and_wrong_identity(self):
        record = self.add()
        source = self.base / 'moved-source'
        self.project.rename(source)
        store = self.base / 'moved-store'
        Path(record['store']).rename(store)
        with self.assertRaises(CoreError) as caught:
            self.registry.relink(record['id'], source, store, '', str(uuid.uuid4()), 'test')
        self.assertEqual(caught.exception.code, 'CONFIRMATION_REQUIRED')
        relink = self.registry.relink(record['id'], source, store, str(source.resolve()), str(uuid.uuid4()), 'test')
        self.assertTrue(relink['available'])
        other_root = self.base / 'other'; other_root.mkdir()
        other = self.registry.add(other_root, None, str(uuid.uuid4()), 'test')
        with self.assertRaises(CoreError) as caught:
            self.registry.relink(record['id'], source, Path(other['store']), str(source), str(uuid.uuid4()), 'test')
        self.assertEqual(caught.exception.code, 'STORE_IDENTITY_MISMATCH')

    def test_nonempty_store_refused_and_registry_readonly_missing(self):
        store = self.base / 'existing'; store.mkdir(); (store/'keep').write_text('keep')
        with self.assertRaises(CoreError):
            self.registry.add(self.project, store, self.req, 'test')
        self.assertEqual((store/'keep').read_text(), 'keep')
        with self.assertRaises(CoreError):
            Registry(self.base/'missing/db', self.policy, read_only=True)
        self.assertFalse((self.base/'missing').exists())

    def test_registration_intents_reconciled_after_crash(self):
        original = ProjectCatalog.create
        def create_then_interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt('crash')
        with patch.object(ProjectCatalog, 'create', side_effect=create_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add()
        self.assertEqual(len(self.registry.reconcile_intents()), 1)
        record = self.add()
        self.assertTrue(self.registry.show(record['id'])['available'])
        self.assertEqual(len(self.registry.list()), 1)

    def test_intent_without_store_never_recreated(self):
        with patch.object(ProjectCatalog, 'create', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.add()
        result = self.registry.reconcile_intents()
        self.assertEqual(result[0]['state'], 'interrupted')
        self.assertEqual(list((self.base/'data/projects').iterdir()), [])

    def test_missing_previous_registry_is_not_replaced(self):
        from solace_core.service import CoreService
        service=CoreService(self.registry);service.close()
        self.registry.path.unlink()
        with self.assertRaises(CoreError): Registry(self.registry.path,self.policy)
        self.assertFalse(self.registry.path.exists())
