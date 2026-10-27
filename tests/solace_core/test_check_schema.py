"""Explicit schema-four upgrade and manual compatibility."""
import sqlite3
from pathlib import Path
import test_catalog
import test_task_schema
from test_integration_schema import create_v2
import test_integration_schema
from solace_core.catalog import ProjectCatalog, readonly_database
from solace_core.errors import CoreError
from solace_core.schema import apply_upgrade, preview_upgrade, schema_version
from solace_core.tasks import TaskCatalog
import unittest

class CheckSchemaTests(unittest.TestCase):
    request = test_task_schema.SchemaTests.request

    def setUp(self):
        test_catalog.CatalogTests.setUp(self)
        self.catalog = create_v2(self.registration, self.policy)
        self.upgrade()

    def upgrade(self):
        preview = preview_upgrade(self.catalog)
        request = self.request()
        request['params']['plan_token'] = preview['plan_token']
        return apply_upgrade(self.catalog, preview['plan_token'], request)

    def test_v3_preview_targets_v4_and_requires_fresh_token(self):
        preview = preview_upgrade(self.catalog)
        self.assertEqual((preview['from_schema'], preview['to_schema']), (3, 4))
        self.assertTrue(preview['required'])
        with self.catalog.connect(write=True) as db:
            self.catalog._event(db, dict(id=None, actor='test'), 'changed', 'succeeded', {})
        with self.assertRaises(CoreError) as caught:
            apply_upgrade(self.catalog, preview['plan_token'], self.request())
        self.assertEqual(caught.exception.code, 'UPGRADE_PREVIEW_STALE')
        result = self.upgrade()
        with readonly_database(Path(result['backup'])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0], 3)
        self.assertEqual(schema_version(self.catalog), 4)

    def test_v1_v2_still_upgrade_to_v3_first(self):
        for version, factory in ((1, test_task_schema.create_v1), (2, create_v2)):
            self.registration['store'] = str(self.base / ('legacy-' + str(version)))
            self.catalog = factory(self.registration, self.policy)
            self.assertEqual(self.upgrade()['schema_version'], 3)
            self.assertEqual(preview_upgrade(self.catalog)['to_schema'], 4)

    def test_v4_manual_integration_and_task_paths(self):
        self.upgrade()
        from solace_core.schema import require_integrations, require_tasks
        require_integrations(self.catalog)
        require_tasks(self.catalog)
        request = self.request()
        request.update(method='task.create', params=dict(project='p', title='compatible'))
        result = TaskCatalog(self.catalog).create(request)
        self.assertEqual(TaskCatalog(self.catalog).show(result['task_id'])['state'], 'created')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def seed_plan(self):
        source, task = test_integration_schema.IntegrationSchemaTests.seed_source_and_task(self)
        operation = self.catalog.accept_operation(dict(id=__import__('uuid').uuid4().hex, method='checkpoint.create', params={}, actor='test'))['operation_id']
        self.catalog.transition(operation, 'running')
        self.catalog.transition(operation, 'succeeded', result={})
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO integration_plans VALUES ('plan',?,?,'source',NULL,'prepared',1,'now','now')",(task,source))
            db.execute("INSERT INTO integration_revisions VALUES ('plan',1,?,?,?,?,NULL,'{}','{}',?,?,'now','now')",(source,source,source,source,'a'*64,'b'*64))
        return source, task, operation

    def test_v3_unchecked_rows_remain_unchanged(self):
        source,task,operation = self.seed_plan()
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO integration_approvals VALUES ('approval','plan',1,? ,?,'owner',1,'now')", ('c'*64, '{ "checks": "not_run_by_solace" }'))
            db.execute("INSERT INTO integration_receipts VALUES ('receipt','plan',1,?,?,'source',?,?,?,?,'now')",(task,source,operation,source,source,'{ "checks": "not_run_by_solace", "accepted_unchecked": true }'))
        with self.catalog.connect() as db:
            before = {table: db.execute('SELECT * FROM ' + table).fetchall()
                      for table in ('integration_approvals', 'integration_receipts')}
            approval_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='integration_approvals'").fetchone()[0]
        self.upgrade()
        with self.catalog.connect() as db:
            self.assertEqual(before, {table: db.execute('SELECT * FROM ' + table).fetchall() for table in before})
            self.assertEqual(approval_sql, db.execute("SELECT sql FROM sqlite_master WHERE name='integration_approvals'").fetchone()[0])

    def test_fresh_store_is_schema_five(self):
        self.registration['store'] = str(self.base / 'fresh')
        self.catalog = ProjectCatalog.create(self.registration, self.policy)
        self.assertEqual(schema_version(self.catalog), 5)
        self.assertFalse(preview_upgrade(self.catalog)['required'])

    def test_checks_require_four_and_ddl_failure_rolls_back(self):
        from solace_core.schema import require_checks
        from solace_core import schema
        from unittest.mock import patch
        with self.assertRaises(CoreError): require_checks(self.catalog)
        preview = preview_upgrade(self.catalog)
        def fail(db):
            schema.create_schema_v4_original(db)
            raise RuntimeError('injected DDL failure')
        with patch.object(schema, 'create_schema_v4_original', schema.create_schema_v4, create=True):
            with patch.object(schema, 'create_schema_v4', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError,'injected'):
                    apply_upgrade(self.catalog,preview['plan_token'],self.request())
        self.assertEqual(schema_version(self.catalog),3)
        with self.catalog.connect() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='check_runs'").fetchone())
        self.upgrade()
        require_checks(self.catalog)

    def test_profile_revisions_are_immutable_and_digests_validated(self):
        self.upgrade()
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO check_profiles VALUES ('profile','tests',1,0,'now','now')")
            db.execute("INSERT INTO check_profile_revisions VALUES ('profile',1,?,'{}','owner','now')",('a'*64,))
        for query in ("UPDATE check_profile_revisions SET definition_json='[]'",'DELETE FROM check_profile_revisions'):
            with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                with self.catalog.connect(write=True) as db: db.execute(query)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.catalog.connect(write=True) as db:
                db.execute("INSERT INTO check_profile_revisions VALUES ('profile',2,?,'{}','owner','now')",('z'*64,))

    def test_terminal_run_and_steps_are_immutable_except_cleanup(self):
        source,task,_ = self.seed_plan()
        self.upgrade()
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO check_profiles VALUES ('profile','tests',1,0,'now','now')")
            db.execute("INSERT INTO check_profile_revisions VALUES ('profile',1,?,'{}','owner','now')",('a'*64,))
            db.execute("INSERT INTO check_runs VALUES ('run','plan',1,?,?,?,'{}','{}','profile',1,?,'running','/private/check',NULL,'pending',NULL,'now','now')",(task,source,'b'*64,'a'*64))
            db.execute("INSERT INTO check_steps(run_id,ordinal,state,argv_json,timeout_seconds,output_cap) VALUES ('run',0,'passed','[\"true\"]',1,10)")
            db.execute("UPDATE check_runs SET state='passed',result_json='{}'")
        for query in ("UPDATE check_runs SET state='running'", "UPDATE check_runs SET manifest_sha256='"+'c'*64+"'", "DELETE FROM check_runs", "UPDATE check_steps SET exit_code=1", "DELETE FROM check_steps", "INSERT INTO check_steps(run_id,ordinal,state,argv_json,timeout_seconds,output_cap) VALUES ('run',1,'passed','[]',1,10)"):
            with self.subTest(query=query), self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):
                with self.catalog.connect(write=True) as db: db.execute(query)
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE check_runs SET cleanup_state='cleaned'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.catalog.connect(write=True) as db:
                db.execute("INSERT INTO task_check_profiles VALUES (?, 'missing',1,'now')",(task,))

    def test_running_step_cannot_be_reparented_into_terminal_run(self):
        source, task, _ = self.seed_plan()
        self.upgrade()
        with self.catalog.connect(write=True) as db:
            db.execute("INSERT INTO check_profiles VALUES ('profile','tests',1,0,'now','now')")
            db.execute("INSERT INTO check_profile_revisions VALUES ('profile',1,?,'{}','owner','now')", ('a'*64,))
            for run, state in (('running-run', 'running'), ('terminal-run', 'passed')):
                db.execute("INSERT INTO check_runs VALUES (?, 'plan',1,?,?,?,'{}','{}','profile',1,?,?,?,NULL,'pending',NULL,'now','now')",
                           (run, task, source, 'b'*64, 'a'*64, state, '/private/'+run))
            db.execute("INSERT INTO check_steps(run_id,ordinal,state,argv_json,timeout_seconds,output_cap) "
                       "VALUES ('running-run',0,'passed','[\"true\"]',1,10)")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
            with self.catalog.connect(write=True) as db:
                db.execute("UPDATE check_steps SET run_id='terminal-run' WHERE run_id='running-run'")

    def test_v4_upgrade_replays_without_second_backup(self):
        from unittest.mock import patch
        from solace_core import schema
        preview = preview_upgrade(self.catalog)
        request = self.request()
        request['params']['plan_token'] = preview['plan_token']
        result = apply_upgrade(self.catalog,preview['plan_token'],request)
        with patch.object(schema,'preview_upgrade',side_effect=AssertionError('replay first')):
            self.assertEqual(apply_upgrade(self.catalog,preview['plan_token'],request),result)
        self.assertEqual(len(list((self.store/'schema-backups').glob('*/checkpoints.sqlite3'))),2)
