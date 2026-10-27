"""Retired catalogs keep ownership across a boot mount-ID change."""
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from solace_core.errors import CoreError
from solace_core.registry import Registry
from solace_core.service import CoreService
from solace_core.storage import StoragePolicy
from solace_core.workspace_paths import CoreConfig


class RetainedMountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('SOLACE_TEST_TMPDIR', '/var/tmp'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.policy = StoragePolicy(mountinfo='56 0 0:1 / / rw - ext4 x rw\n', sync_roots=[])
        self.registry = Registry(self.base / 'data/registry.sqlite3', self.policy)
        self.service = CoreService(self.registry, config=CoreConfig((), self.base / 'workspaces'))
        self.addCleanup(self.service.close)
        self.original = self.base / 'original'
        self.original.mkdir()
        self.retired = self.call('project.add', root=str(self.original))
        self.call('project.unregister', project=self.retired['id'])

    def call(self, method, **params):
        response = self.service.handle(dict(version=1, id=str(uuid.uuid4()), method=method,
                                            params=params, actor='test'))
        self.assertTrue(response['ok'], response)
        return response['result']

    def add_after_remount(self):
        self.policy.mountinfo = '58 0 0:1 / / rw - ext4 x rw\n'
        fresh = self.base / 'fresh'
        fresh.mkdir()
        return self.service.handle(dict(version=1, id=str(uuid.uuid4()), method='project.add',
                                        params={'root': str(fresh)}, actor='test'))

    def completed(self, project, method, **params):
        accepted = self.call(method, project=project, **params)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            operation = self.service.catalog(project).get_operation(accepted['operation_id'])
            if operation['state'] not in ('queued', 'running'):
                self.assertEqual(operation['state'], 'succeeded', operation)
                return operation['result']
            time.sleep(.02)
        self.fail(f'{method} did not finish')

    def test_verified_retired_catalog_allows_registration_after_mount_id_change(self):
        response = self.add_after_remount()
        self.assertTrue(response['ok'], response)
        retained_store = Path(self.retired['store'])
        with self.assertRaises(CoreError) as caught:
            self.service.workspace_paths.assert_unowned(retained_store / 'candidate')
        self.assertEqual(caught.exception.code, 'UNSUPPORTED_STORAGE')

    def test_replaced_retired_catalog_still_blocks_registration(self):
        with sqlite3.connect(Path(self.retired['store']) / 'checkpoints.sqlite3') as db:
            db.execute("UPDATE core_metadata SET store_uuid='replacement'")
        response = self.add_after_remount()
        self.assertFalse(response['ok'], response)
        self.assertEqual(response['error']['code'], 'STORE_UNAVAILABLE')

    def test_registered_catalog_mount_change_still_blocks_registration(self):
        active = self.base / 'active'
        active.mkdir()
        self.call('project.add', root=str(active))
        response = self.add_after_remount()
        self.assertFalse(response['ok'], response)
        self.assertEqual(response['error']['code'], 'STORE_UNAVAILABLE')

    def test_new_project_can_prepare_result_after_retired_mount_change(self):
        response = self.add_after_remount()
        self.assertTrue(response['ok'], response)
        project = response['result']['id']
        task = self.call('task.create', project=project, title='work')['task_id']
        revision = self.call('task.show', project=project, task=task)['revision']
        workspace = Path(self.completed(project, 'task.start', task=task, expected_revision=revision)['path'])
        (workspace / 'candidate').write_text('ready')
        revision = self.call('task.show', project=project, task=task)['revision']
        checkpoint = self.completed(project, 'task.ready', task=task, expected_revision=revision)['checkpoint_id']
        revision = self.call('task.show', project=project, task=task)['revision']
        plan = self.completed(project, 'integration.prepare', task=task, result=checkpoint,
                              expected_revision=revision)
        self.assertIn('plan_id', plan)


if __name__ == '__main__':
    unittest.main()
