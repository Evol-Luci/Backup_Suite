"""Disposable configured checks through the real CLI and Unix socket."""
import json
import sys
from pathlib import Path
import test_integration_acceptance as integration_fixture
from integration_fixture import IntegrationCase


class CheckAcceptanceTests(IntegrationCase):
    def setUp(self):
        import os, threading
        from solace_core.server import SocketServer, bind_private_socket
        super().setUp()
        self.path = self.base / 'core.sock'
        self.listener = bind_private_socket(self.path, self.base)
        self.server = SocketServer(self.listener, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.base), XDG_DATA_HOME=str(self.base/'xdg'), HOME=str(self.base/'home'))
    result = integration_fixture.IntegrationAcceptanceTests.result
    run_cli = integration_fixture.IntegrationAcceptanceTests.run_cli
    stop_server = integration_fixture.IntegrationAcceptanceTests.stop_server
    completed = integration_fixture.IntegrationAcceptanceTests.completed
    plan = integration_fixture.IntegrationAcceptanceTests.plan
    apply = integration_fixture.IntegrationAcceptanceTests.apply
    approval = integration_fixture.IntegrationAcceptanceTests.approval
    failed = integration_fixture.IntegrationAcceptanceTests.failed
    def profile(self, code, timeout=5):
        definition = {'version': 1, 'name': 'acceptance-' + str(timeout) + '-' + str(len(code)), 'environment': {}, 'steps': [
            {'name': 'check', 'argv': [sys.executable, '-c', code],
             'timeout_seconds': timeout, 'output_limit_bytes': 1024}]}
        path = self.base / 'profile.json'
        path.write_text(json.dumps(definition))
        args = ('project', 'check-profile', 'preview', self.project, 'create', '--definition', str(path))
        preview = self.result(*args)
        return self.result('project', 'check-profile', 'apply', self.project, 'create',
                           '--definition', str(path), '--plan-token', preview['plan_token'], '--confirmed')['profile_id']

    def selected_task(self, profile, parent=None):
        args = ('--parent', parent) if parent else ()
        task = self.result('task', 'create', self.project, 'checks', *args)['task_id']
        self.result('task', 'check-profile', 'select', self.project, task, profile)
        path = Path(self.completed('task', 'start', self.project, task)['path'])
        (path / 'candidate').write_text('exact candidate')
        result = self.completed('task', 'ready', self.project, task)['checkpoint_id']
        return task, result, path

    def run_check(self, plan, profile):
        response = self.run_cli('integration', 'check', 'run', self.project, plan['plan_id'],
                                '--revision', str(plan['revision']), '--profile', profile, '--wait', '--json', success=False)
        operation = json.loads(response.stdout)['result']
        self.assertEqual(operation['state'], 'succeeded', operation)
        return self.result('integration', 'check', 'show', self.project, operation['result']['run_id'])

    def checked_apply(self, plan, run):
        approval = self.result('integration', 'approve', self.project, plan['plan_id'], '--revision',
            str(plan['revision']), '--token', plan['review_token'], '--check-run', run['id'])['approval']
        return self.apply(plan, approval)

    def test_socket_checked_child_parent_and_source_flow(self):
        profile = self.profile('assert open("candidate").read() == "exact candidate"')
        parent = self.result('task', 'create', self.project, 'parent')['task_id']
        self.result('task', 'check-profile', 'select', self.project, parent, profile)
        parent_path = Path(self.completed('task', 'start', self.project, parent)['path'])
        child, saved, _ = self.selected_task(profile, parent)
        plan = self.plan(child, saved)
        run = self.run_check(plan, profile)
        self.assertEqual(run['state'], 'passed')
        receipt = self.checked_apply(plan, run)
        self.assertEqual(self.result('integration', 'receipt', self.project, receipt['receipt_id'])['checks']['run_id'], run['id'])
        saved = self.completed('task', 'ready', self.project, parent)['checkpoint_id']
        plan = self.plan(parent, saved)
        run = self.run_check(plan, profile)
        self.assertEqual(run['state'], 'passed')
        receipt = self.checked_apply(plan, run)
        self.assertEqual(self.result('integration', 'receipt', self.project, receipt['receipt_id'])['checks']['run_id'], run['id'])
        self.assertEqual((parent_path / 'candidate').read_text(), 'exact candidate')
        self.assertEqual((self.root / 'candidate').read_text(), 'exact candidate')

    def test_failed_timeout_and_unchanged_live_trees(self):
        for code, timeout, expected in [('raise SystemExit(7)', 5, 'failed'), ('import time; time.sleep(3)', 1, 'timed_out')]:
            with self.subTest(expected=expected):
                profile = self.profile(code, timeout)
                task, saved, workspace = self.selected_task(profile)
                plan = self.plan(task, saved)
                before = {str(p): p.read_bytes() for root in (self.root, workspace) for p in root.rglob('*') if p.is_file()}
                run = self.run_check(plan, profile)
                after = {str(p): p.read_bytes() for root in (self.root, workspace) for p in root.rglob('*') if p.is_file()}
                self.assertEqual(after, before)
                self.assertEqual(run['state'], expected)
                denied = self.failed('integration', 'approve', self.project, plan['plan_id'], '--revision', str(plan['revision']), '--token', plan['review_token'], '--check-run', run['id'])
                self.assertFalse(denied['ok'])
                self.assertEqual((workspace / 'candidate').read_text(), 'exact candidate')
                self.assertFalse((self.root / 'candidate').exists())

    def test_artifact_from_check_never_applies(self):
        profile = self.profile('open("artifact", "w").write("private")')
        task, saved, workspace = self.selected_task(profile)
        plan = self.plan(task, saved)
        run = self.run_check(plan, profile)
        self.assertEqual(run['state'], 'passed')
        self.checked_apply(plan, run)
        self.assertFalse((workspace / 'artifact').exists())
        self.assertFalse((self.root / 'artifact').exists())

    def test_restarted_core_preserves_interrupted_evidence(self):
        from unittest.mock import patch
        from solace_core.service import CoreService
        from solace_core.server import SocketServer, bind_private_socket
        import threading
        profile = self.profile('print("never started")')
        task, saved, _ = self.selected_task(profile)
        plan = self.plan(task, saved)
        with patch.object(self.service.coordinator(self.project), 'submit'):
            accepted = self.result('integration', 'check', 'run', self.project, plan['plan_id'],
                                   '--revision', str(plan['revision']), '--profile', profile)
        self.stop_server()
        self.service.close()
        self.path.unlink(missing_ok=True)
        self.service = CoreService(self.registry, config=self.config)
        self.addCleanup(self.service.close)
        self.listener = bind_private_socket(self.path, self.base)
        self.server = SocketServer(self.listener, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        run = self.result('integration', 'check', 'show', self.project, accepted['run_id'])
        self.assertEqual(run['state'], 'interrupted')
        self.assertEqual(self.result('operation', 'show', self.project, accepted['operation_id'])['state'], 'interrupted')
        self.assertFalse(self.failed('integration', 'approve', self.project, plan['plan_id'], '--revision',
            str(plan['revision']), '--token', plan['review_token'], '--check-run', run['id'])['ok'])

    def test_disposable_schema_three_upgrade_over_socket(self):
        import shutil
        from test_integration_schema import create_v2
        from solace_core.schema import preview_upgrade, apply_upgrade
        from solace_core.service import CoreService
        from solace_core.server import SocketServer, bind_private_socket
        import threading
        from solace_core.catalog import readonly_database
        self.stop_server()
        self.service.close()
        self.path.unlink(missing_ok=True)
        registration = self.registry.get(self.project)
        store = Path(registration['store'])
        self.assertTrue(store.is_relative_to(self.base))
        shutil.rmtree(store)
        catalog = create_v2(registration, self.policy)
        preview = preview_upgrade(catalog)
        request = self.request('project.upgrade', plan_token=preview['plan_token'])
        apply_upgrade(catalog, preview['plan_token'], request)
        self.service = CoreService(self.registry, config=self.config)
        self.addCleanup(self.service.close)
        self.listener = bind_private_socket(self.path, self.base)
        self.server = SocketServer(self.listener, self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        preview = self.result('project', 'upgrade', self.project, '--preview')
        self.assertEqual((preview['from_schema'], preview['to_schema']), (3, 4))
        result = self.result('project', 'upgrade', self.project, '--apply', '--plan-token', preview['plan_token'])
        self.assertEqual(result['schema_version'], 4)
        with readonly_database(Path(result['backup'])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0], 3)
        profile = self.profile('print("upgraded")')
        task, saved, _ = self.selected_task(profile)
        self.assertEqual(self.run_check(self.plan(task, saved), profile)['state'], 'passed')
