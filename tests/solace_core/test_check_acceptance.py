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
        import os
        import signal
        import subprocess
        import time
        from solace_core.check_process import process_start_time

        definition = {'version': 1, 'name': 'crash-evidence', 'environment': {}, 'steps': [
            {'name': 'evidence', 'argv': [sys.executable, '-c', 'print("retained evidence")'],
             'timeout_seconds': 5, 'output_limit_bytes': 1024},
            {'name': 'running', 'argv': [sys.executable, '-c', 'import time; time.sleep(60)'],
             'timeout_seconds': 120, 'output_limit_bytes': 1024}]}
        definition_path = self.base / 'crash-profile.json'
        definition_path.write_text(json.dumps(definition))
        preview = self.result('project', 'check-profile', 'preview', self.project, 'create', '--definition', str(definition_path))
        profile = self.result('project', 'check-profile', 'apply', self.project, 'create', '--definition', str(definition_path),
                              '--plan-token', preview['plan_token'], '--confirmed')['profile_id']
        task, saved, _ = self.selected_task(profile)
        plan = self.plan(task, saved)
        self.stop_server()
        self.service.close()
        self.path.unlink(missing_ok=True)

        # A separate core process owns the actual socket and worker. Killing it
        # avoids the cancellation and executor join performed by clean close().
        script = """
import sys
from pathlib import Path
from solace_core.registry import Registry
from solace_core.storage import StoragePolicy
from solace_core.service import CoreService
from solace_core.workspace_paths import CoreConfig
from solace_core.server import SocketServer, bind_private_socket
base = Path(sys.argv[1])
policy = StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\\n', sync_roots=[])
registry = Registry(base / 'data/registry.sqlite3', policy)
service = CoreService(registry, config=CoreConfig(sync_roots=(), workspace_root=base / 'workspaces'))
listener = bind_private_socket(base / 'core.sock', base)
SocketServer(listener, service).serve_forever()
"""
        processes = []
        def start_core():
            self.path.unlink(missing_ok=True)
            proc = subprocess.Popen([sys.executable, '-c', script, str(self.base)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            processes.append(proc)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    self.fail(f'core exited: {proc.stderr.read().decode()}')
                if self.path.exists():
                    return proc
                time.sleep(.02)
            self.fail('core socket did not appear')
        def stop_processes():
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
                proc.stderr.close()
        self.addCleanup(stop_processes)
        core = start_core()
        accepted = self.result('integration', 'check', 'run', self.project, plan['plan_id'],
                               '--revision', str(plan['revision']), '--profile', profile)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = self.result('integration', 'check', 'show', self.project, accepted['run_id'])
            process = run.get('process') or {}
            if process.get('ordinal') == 1 and len(run['steps']) == 1:
                break
            time.sleep(.03)
        else:
            self.fail(f'check did not launch second step: {run}')
        self.assertEqual(run['state'], 'running')
        self.assertEqual(process_start_time(process['pid']), process['start_time'])
        def reap_survivor():
            if process_start_time(process['pid']) == process['start_time']:
                try:
                    if os.getpgid(process['pid']) == process['pid']:
                        os.killpg(process['pid'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(reap_survivor)
        evidence = Path(run['steps'][0]['stdout_path'])
        self.assertEqual(evidence.read_bytes(), b'retained evidence\n')
        stage = Path(run['staging_path'])
        self.assertTrue(stage.is_dir())
        self.assertEqual((stage / 'candidate' / 'candidate').read_text(), 'exact candidate')
        os.kill(core.pid, signal.SIGKILL)
        self.assertEqual(core.wait(timeout=10), -signal.SIGKILL)
        start_core()
        run = self.result('integration', 'check', 'show', self.project, accepted['run_id'])
        self.assertEqual(run['state'], 'interrupted')
        self.assertEqual(run['recovery']['survivor'], 'signalled_retained')
        self.assertEqual(run['recovery']['cleanup'], 'retained')
        self.assertEqual(run['steps'][0]['state'], 'passed')
        self.assertEqual(evidence.read_bytes(), b'retained evidence\n')
        self.assertTrue(stage.is_dir())
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
