"""Schema-five creation and explicit upgrade compatibility."""
import unittest
from pathlib import Path
import test_catalog
import test_task_schema
from test_integration_schema import create_v2
from solace_core.catalog import ProjectCatalog, readonly_database
from solace_core.errors import CoreError
from solace_core.schema import apply_upgrade, preview_upgrade, schema_version
from solace_core.tasks import TaskCatalog

class PolicySchemaTests(unittest.TestCase):
    setUp = test_catalog.CatalogTests.setUp
    request = test_task_schema.SchemaTests.request

    def upgrade(self, catalog):
        preview = preview_upgrade(catalog)
        request = self.request()
        request['params']['plan_token'] = preview['plan_token']
        return apply_upgrade(catalog, preview['plan_token'], request)

    def test_new_store_defaults_manual_schema_five(self):
        catalog = ProjectCatalog.create(self.registration, self.policy)
        self.assertEqual(schema_version(catalog), 5)
        with catalog.connect() as db:
            row = db.execute('SELECT revision,mode,required_profile_id,required_profile_revision,required_profile_digest,allow_task_opt_in FROM project_integration_policy').fetchone()
            self.assertEqual(tuple(row), (1, 'manual', None, None, None, 0))
            self.assertEqual(db.execute('SELECT count(*) FROM task_integration_policy').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM integration_policy_intents').fetchone()[0], 0)

    def test_schema_four_upgrade_requires_preview_and_verified_backup(self):
        catalog = create_v2(self.registration, self.policy)
        self.upgrade(catalog)
        self.upgrade(catalog)
        preview = preview_upgrade(catalog)
        self.assertEqual((preview['required'], preview['from_schema'], preview['to_schema']), (True, 4, 5))
        with self.assertRaises(CoreError):
            apply_upgrade(catalog, 'bad', self.request())
        self.assertEqual(schema_version(catalog), 4)
        result = self.upgrade(catalog)
        with readonly_database(Path(result['backup'])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0], 4)
        self.assertEqual(schema_version(catalog), 5)

    def test_older_store_manual_operations_remain_readable(self):
        catalog = create_v2(self.registration, self.policy)
        request = self.request()
        request.update(method='task.create', params=dict(project='p', title='legacy'))
        task = TaskCatalog(catalog).create(request)['task_id']
        self.assertEqual(TaskCatalog(catalog).show(task)['state'], 'created')
        self.assertEqual(schema_version(catalog), 2)
