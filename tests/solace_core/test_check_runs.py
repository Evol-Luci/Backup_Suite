"""Durable exact-candidate execution and restart boundaries."""
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch
from integration_fixture import IntegrationCase
from solace_core.check_runs import CheckRuns
from solace_core.check_profiles import CheckProfiles
from solace_core.check_selection import CheckSelection
from solace_core.check_staging import CheckStaging
from solace_core.check_process import CheckExecutor


class RunTests(IntegrationCase):
    def prepared_run(self, scripts=None):
        self.catalog = self.service.catalog(self.project)
        scripts = scripts or ['open("generated", "w").write("ok")', 'print(open("generated").read())']
        self.definition = dict(version=1, name='run checks', environment={}, steps=[
            dict(name=str(i), argv=[sys.executable, '-c', code], timeout_seconds=5, output_limit_bytes=1024)
            for i, code in enumerate(scripts)])
        profiles = CheckProfiles(self.catalog)
        params = dict(action='create', definition=self.definition)
        preview = profiles.preview(self.request('project.check-profile.preview', **params))
        self.profile = profiles.apply(self.request('project.check-profile.apply', **params,
            confirmed=True, plan_token=preview['plan_token']))['profile_id']
        task = self.create_task()
        CheckSelection(self.catalog).select(self.request('task.check-profile.select', task=task,
            profile=self.profile, expected_revision=self.show(task)['revision']))
        workspace = Path(self.start(task)['path'])
        (workspace/'candidate').write_text('M')
        done = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        self.plan = self.prepare_direct(task, done['result']['checkpoint_id'])
        self.runs = CheckRuns(self.service, self.catalog)
        self.req = self.request('integration.check.run', plan=self.plan.id,
            revision=self.plan.revision, profile=self.profile)
        return workspace

    def finish(self, accepted):
        self.wait_operation(accepted['operation_id'])
        # Cleanup follows the terminal transaction; wait for the coordinator too.
        import time
        for _ in range(500):
            if not self.service.coordinator(self.project).busy: break
            time.sleep(.01)
        return self.runs.show(accepted['run_id'])

    def test_missing_task_profile_returns_setup_error_without_reserving_run(self):
        from solace_core.errors import CoreError
        self.prepared_run()
        task, result, _ = self.ready_task({'candidate': b'M'})
        plan = self.prepare_direct(task, result)
        request = self.request('integration.check.run', plan=plan.id,
            revision=plan.revision, profile=self.profile)
        with self.assertRaises(CoreError) as caught:
            self.runs.start(request)
        self.assertEqual(caught.exception.code, 'CHECK_PROFILE_MISSING')
        self.assertIn('solace task check-profile select', caught.exception.next_step)
        self.assertEqual(self.runs.list(plan.id)['runs'], [])

    def test_pass_and_fail_are_durable_and_replayable(self):
        workspace = self.prepared_run()
        accepted = self.runs.start(self.req)
        result = self.finish(accepted)
        self.assertEqual(result['state'], 'passed', result)
        self.assertEqual([s['state'] for s in result['steps']], ['passed', 'passed'])
        self.assertEqual(self.runs.start(self.req), accepted)
        self.assertEqual(self.catalog.get_operation(accepted['operation_id'])['state'], 'succeeded')
        self.assertFalse((workspace/'generated').exists())
        self.assertFalse((self.root/'generated').exists())
        self.assertEqual(self.runs.list(self.plan.id)['runs'][0]['id'], accepted['run_id'])
        request = self.request('integration.check.run', **{k:v for k,v in self.req['params'].items() if k!='project'})
        original = CheckExecutor.execute
        def failure(executor, step, stage, cancel, on_start):
            return original(executor, dict(step, argv=[sys.executable,'-c','raise SystemExit(7)']), stage, cancel, on_start)
        with patch.object(CheckExecutor, 'execute', failure):
            failed = self.runs.start(request); result = self.finish(failed)
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['steps'][0]['exit_code'], 7)
        self.assertEqual(len(result['steps']), 1)
        self.assertEqual(self.catalog.get_operation(failed['operation_id'])['state'], 'succeeded')

    def test_candidate_profile_target_change_marks_stale(self):
        self.prepared_run()
        original = CheckExecutor.execute
        def change(executor, *args):
            result = original(executor, *args)
            (self.root/'changed').write_text('target changed')
            return result
        with patch.object(CheckExecutor, 'execute', change):
            result = self.finish(self.runs.start(self.req))
        self.assertEqual(result['state'], 'stale')
        binding = result['binding']
        self.assertEqual(set(binding), {'plan_id','revision','review_token','candidate_id','manifest_sha256',
            'capture_profile_sha256','expected_sha256','task_id','task_revision','profile_id','profile_revision','profile_digest'})

    def test_wait_disconnect_does_not_cancel(self):
        self.prepared_run(['import time; time.sleep(.1); print("done")'])
        accepted = self.runs.start(self.req)
        del self.runs
        self.runs = CheckRuns(self.service, self.catalog)
        self.assertEqual(self.finish(accepted)['state'], 'passed')

    def test_restart_interrupts_without_false_pass(self):
        self.prepared_run()
        with patch.object(self.service.coordinator(self.project), 'submit'):
            accepted = self.runs.start(self.req)
        with self.catalog.connect(write=True) as db:
            self.runs.recover(db)
        result = self.runs.show(accepted['run_id'])
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(self.catalog.get_operation(accepted['operation_id'])['state'], 'interrupted')
        self.assertEqual(self.runs.start(self.req), accepted)

    def test_cancel_then_replay_is_idempotent(self):
        self.prepared_run(['import time; time.sleep(4)'])
        accepted = self.runs.start(self.req)
        request = self.request('integration.check.cancel', run=accepted['run_id'])
        first = self.runs.cancel(request)
        self.assertEqual(first['state'], 'cancelling')
        self.assertEqual(self.runs.cancel(request), first)
        self.assertEqual(self.finish(accepted)['state'], 'cancelled')

    def test_stage_replacement_preserves_evidence_and_reports_cleanup_blocked(self):
        self.prepared_run(['print("keep")'])
        original = CheckExecutor.execute
        def replace(executor, step, stage, *args):
            result = original(executor, step, stage, *args)
            stage.root.rename(stage.root.with_name('moved'))
            stage.root.mkdir(); (stage.root/'keep').write_text('replacement')
            return result
        with patch.object(CheckExecutor, 'execute', replace):
            result = self.finish(self.runs.start(self.req))
        self.assertEqual(result['cleanup_state'], 'unsafe')
        self.assertEqual(Path(result['steps'][0]['stdout_path']).read_bytes(), b'keep\n')
        self.assertEqual((Path(result['staging_path'])/'candidate'/'keep').read_text(), 'replacement')

    def test_capped_evidence_files_are_private_and_separate(self):
        self.prepared_run(['import sys; print("o"*5000); print("e"*6000, file=sys.stderr)'])
        result = self.finish(self.runs.start(self.req))
        self.assertEqual(result['state'], 'passed', result)
        self.assertEqual(result['cleanup_state'], 'cleaned')
        self.assertFalse(Path(result['staging_path']).exists())
        step = result['steps'][0]
        for stream, total in [('stdout',5001),('stderr',6001)]:
            path = Path(step[stream+'_path'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(path.read_bytes()), 1024)
            self.assertEqual(step[stream+'_bytes'], total)
            self.assertTrue(step['details'][stream+'_truncated'])

    def test_profile_change_after_execution_is_stale(self):
        self.prepared_run(['print("ok")'])
        original = CheckExecutor.execute
        def update(executor, *args):
            result = original(executor,*args)
            profiles = CheckProfiles(self.catalog)
            params = dict(action='update',profile_id=self.profile,
                          definition=dict(self.definition,name='updated'))
            preview = profiles.preview(self.request('project.check-profile.preview',**params))
            profiles.apply(self.request('project.check-profile.apply',**params,
                confirmed=True,plan_token=preview['plan_token']))
            return result
        with patch.object(CheckExecutor,'execute',update):
            result = self.finish(self.runs.start(self.req))
        self.assertEqual(result['state'],'stale')
        self.assertEqual(result['profile_revision'],1)

    def test_process_identity_and_previous_evidence_are_durable_before_next_step(self):
        self.prepared_run()
        original = CheckExecutor.execute
        calls = []
        def inspect(executor, step, stage, cancel, on_start):
            ordinal = len(calls); calls.append(ordinal)
            def persisted(pid,identity):
                on_start(pid,identity)
                with self.catalog.connect() as db:
                    row = db.execute('SELECT result_json FROM check_runs WHERE id=?',(stage.run_id,)).fetchone()
                    process = json.loads(row[0])['process']
                    self.assertEqual(process,dict(pid=pid,start_time=identity,ordinal=ordinal))
                    if ordinal:
                        previous = db.execute('SELECT stdout_path FROM check_steps WHERE run_id=? AND ordinal=0',(stage.run_id,)).fetchone()
                        self.assertTrue(Path(previous[0]).exists())
            return original(executor,step,stage,cancel,persisted)
        with patch.object(CheckExecutor,'execute',inspect):
            self.assertEqual(self.finish(self.runs.start(self.req))['state'],'passed')
        self.assertEqual(calls,[0,1])

    def test_incomplete_output_and_launch_error_never_pass(self):
        from dataclasses import replace
        self.prepared_run(['print("ok")'])
        original = CheckExecutor.execute
        def incomplete(executor,*args): return replace(original(executor,*args),output_complete=False)
        with patch.object(CheckExecutor,'execute',incomplete):
            accepted = self.runs.start(self.req); result = self.finish(accepted)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['cleanup_state'],'unsafe')
        self.assertTrue(Path(result['staging_path']).exists())
        self.assertEqual(self.catalog.get_operation(accepted['operation_id'])['state'],'succeeded')
        request = self.request('integration.check.run',plan=self.plan.id,revision=1,profile=self.profile)
        def missing(executor,step,*args): return original(executor,dict(step,argv=['/missing-executable']),*args)
        with patch.object(CheckExecutor,'execute',missing):
            accepted = self.runs.start(request); result = self.finish(accepted)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['steps'][0]['state'],'launch_error')
        self.assertEqual(self.catalog.get_operation(accepted['operation_id'])['state'],'failed')

    def test_incomplete_output_capture_error_retains_stage(self):
        from dataclasses import replace
        self.prepared_run(['print("ok")'])
        original = CheckExecutor.execute
        def incomplete(executor, *args):
            return replace(original(executor, *args), output_complete=False)
        with patch.object(CheckExecutor, 'execute', incomplete), patch.object(self.runs, '_capture', side_effect=OSError('capture failed')):
            result = self.finish(self.runs.start(self.req))
        self.assertEqual(result['cleanup_state'], 'unsafe')
        self.assertTrue(Path(result['staging_path']).exists())

    def test_cancel_during_final_scan_returns_and_prevents_pass(self):
        self.prepared_run(['print("ok")'])
        entered, release = threading.Event(), threading.Event()
        original = self.runs.binding
        calls = []
        def binding(*args):
            calls.append(1)
            if len(calls) == 3:
                entered.set()
                release.wait(10)
            return original(*args)
        with patch.object(self.runs, 'binding', binding):
            accepted = self.runs.start(self.req)
            self.assertTrue(entered.wait(10))
            response = []
            thread = threading.Thread(target=lambda: response.append(self.runs.cancel(
                self.request('integration.check.cancel', run=accepted['run_id']))))
            thread.start()
            thread.join(2)
            try:
                self.assertFalse(thread.is_alive(), 'cancel waited for the final scan')
            finally:
                release.set()
                thread.join(10)
            self.assertEqual(response[0]['state'], 'cancelling')
            self.assertEqual(self.finish(accepted)['state'], 'cancelled')

    def test_recovery_retains_running_stage_without_recorded_pid(self):
        self.prepared_run()
        with patch.object(self.service.coordinator(self.project), 'submit'):
            accepted = self.runs.start(self.req)
        stage = self.runs.staging.stage(accepted['run_id'], self.plan)
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE check_runs SET state='running' WHERE id=?", (accepted['run_id'],))
        with patch('solace_core.check_runs.os.killpg') as kill:
            self.catalog.recover_interrupted()
        kill.assert_not_called()
        result = self.runs.show(accepted['run_id'])
        self.assertEqual(result['cleanup_state'], 'unsafe')
        self.assertEqual(result['recovery']['cleanup'], 'retained')
        self.assertEqual(result['recovery']['survivor'], 'identity_uncertain')
        self.assertTrue(stage.root.exists())

    def test_recovery_checks_identity_and_preserves_uncertain_stage(self):
        self.prepared_run()
        with patch.object(self.service.coordinator(self.project),'submit'):
            accepted = self.runs.start(self.req)
        stage = CheckStaging(self.service,self.catalog).stage(accepted['run_id'],self.plan)
        (stage.evidence/'keep').write_text('retained')
        with self.catalog.connect(write=True) as db:
            row = db.execute('SELECT result_json FROM check_runs WHERE id=?',(accepted['run_id'],)).fetchone()
            result = json.loads(row[0]); result['process'] = dict(pid=123456,start_time=100,ordinal=0)
            db.execute("UPDATE check_runs SET state='running',result_json=? WHERE id=?",(json.dumps(result),accepted['run_id']))
        with patch('solace_core.check_runs.process_start_time',return_value=101), patch('solace_core.check_runs.os.killpg') as kill:
            self.catalog.recover_interrupted()
        kill.assert_not_called()
        result = self.runs.show(accepted['run_id'])
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(result['cleanup_state'],'unsafe')
        self.assertTrue(stage.root.exists())
        self.assertEqual((stage.evidence/'keep').read_text(),'retained')
        self.assertEqual(self.catalog.get_operation(accepted['operation_id'])['state'],'interrupted')

    def test_recovery_signals_only_matching_process_group(self):
        self.prepared_run()
        with patch.object(self.service.coordinator(self.project),'submit'):
            accepted = self.runs.start(self.req)
        with self.catalog.connect(write=True) as db:
            row = db.execute('SELECT result_json FROM check_runs WHERE id=?',(accepted['run_id'],)).fetchone()
            result=json.loads(row[0]); result['process']=dict(pid=123456,start_time=100,ordinal=0)
            db.execute('UPDATE check_runs SET result_json=? WHERE id=?',(json.dumps(result),accepted['run_id']))
        with patch('solace_core.check_runs.process_start_time',return_value=100), patch('solace_core.check_runs.os.getpgid',return_value=123456), patch('solace_core.check_runs.os.killpg') as kill:
            self.catalog.recover_interrupted()
        import signal
        self.assertEqual(kill.call_args_list, [((123456,signal.SIGTERM),),((123456,signal.SIGKILL),)])
        self.assertEqual(self.runs.show(accepted['run_id'])['state'],'interrupted')

    def test_available_store_relink_and_unregister_refuse_active_run(self):
        from solace_core.errors import CoreError
        self.prepared_run()
        with patch.object(self.service.coordinator(self.project),'submit'):
            accepted = self.runs.start(self.req)
        for method, extra in [('project.unregister',{}), ('project.relink',dict(root=str(self.root),store=str(self.catalog.store),confirmed_root=str(self.root)))]:
            with self.assertRaises(CoreError) as caught:
                self.service._dispatch(self.request(method,**extra))
            self.assertEqual(caught.exception.code,'PROJECT_BUSY')
        with self.catalog.connect(write=True) as db: self.runs.recover(db)

    def test_retained_paths_remain_owned_after_physical_store_relink(self):
        import shutil
        from solace_core.errors import CoreError
        self.prepared_run(['print("keep")'])
        result = self.finish(self.runs.start(self.req))
        old = self.catalog.store
        moved = old.with_name(old.name+'-moved')
        shutil.move(old,moved)
        self.call('project.relink',project=self.project,root=str(self.root),store=str(moved),confirmed_root=str(self.root))
        claims = self.service.workspace_paths.active_claims()
        for path in (old/'check-evidence'/result['id'],moved/'check-evidence'/result['id'],moved/'check-runs'/result['id']):
            self.assertIn(str(path),[row['path'] for row in claims])
            with self.assertRaises(CoreError): self.service.workspace_paths.assert_access(path)

    def test_shutdown_requests_cancellation_before_waiting(self):
        import time
        self.prepared_run(['import time; time.sleep(4)'])
        accepted = self.runs.start(self.req)
        before = time.monotonic(); self.service.close()
        self.assertLess(time.monotonic()-before,2)
        self.assertEqual(self.runs.show(accepted['run_id'])['state'],'cancelled')

    def test_pass_transaction_rechecks_profile_after_final_validation(self):
        self.prepared_run(['print("ok")'])
        original = self.runs._finish
        def update_then_finish(*args):
            profiles = CheckProfiles(self.catalog)
            params=dict(action='update',profile_id=self.profile,definition=dict(self.definition,name='changed at commit'))
            preview=profiles.preview(self.request('project.check-profile.preview',**params))
            profiles.apply(self.request('project.check-profile.apply',**params,confirmed=True,plan_token=preview['plan_token']))
            return original(*args)
        with patch.object(self.runs,'_finish',update_then_finish):
            result = self.finish(self.runs.start(self.req))
        self.assertEqual(result['state'],'stale')

    def test_run_resolves_current_profile_revision_and_all_binding_fields(self):
        self.prepared_run(['print("ok")'])
        profiles=CheckProfiles(self.catalog)
        params=dict(action='update',profile_id=self.profile,definition=dict(self.definition,name='current'))
        preview=profiles.preview(self.request('project.check-profile.preview',**params))
        profiles.apply(self.request('project.check-profile.apply',**params,confirmed=True,plan_token=preview['plan_token']))
        result=self.finish(self.runs.start(self.req))
        self.assertEqual(result['state'],'passed')
        self.assertEqual(result['binding']['profile_revision'],2)
        from solace_core.integration_snapshots import canonical
        import hashlib
        with self.catalog.connect() as db:
            row=db.execute('SELECT * FROM integration_revisions WHERE plan_id=?',(self.plan.id,)).fetchone()
        binding=result['binding']
        self.assertEqual(binding['review_token'],row['review_token'])
        self.assertEqual(binding['candidate_id'],self.plan.candidate.checkpoint_id)
        self.assertEqual(binding['manifest_sha256'],self.plan.candidate.manifest_sha256)
        self.assertEqual(binding['capture_profile_sha256'],hashlib.sha256(canonical(json.loads(row['profile_json'])).encode()).hexdigest())
        self.assertEqual(binding['expected_sha256'],hashlib.sha256(canonical(json.loads(row['expected_json'])).encode()).hexdigest())
        self.assertEqual(binding['task_revision'],self.show(self.plan.task_id)['revision'])

    def test_subprocess_loss_at_reservation_launch_capture_and_pass_commit(self):
        import subprocess
        import shutil
        import textwrap
        from solace_core.registry import Registry
        from solace_core.service import CoreService
        from solace_core.workspace_paths import CoreConfig
        for edge in ('reserved','started','captured','before_commit'):
            with self.subTest(edge=edge):
                report = self.base/('crash-'+edge+'.json')
                script = textwrap.dedent('''
                    import json, os, sys, time
                    from pathlib import Path
                    from unittest.mock import patch
                    from test_check_runs import RunTests
                    from solace_core.check_staging import CheckStaging
                    from solace_core.check_process import CheckExecutor
                    case=RunTests(); case.setUp()
                    edge,report=sys.argv[1:]
                    scripts=['import time; time.sleep(30)'] if edge=='started' else ['print("first")','print("second")']
                    case.prepared_run(scripts)
                    def crash():
                        with case.catalog.connect() as db:
                            row=dict(db.execute('SELECT * FROM check_runs').fetchone())
                        Path(report).write_text(json.dumps(dict(base=str(case.base),project=case.project,
                            registry=str(case.registry.path),store=str(case.catalog.store),run=row['id'],request=case.req)))
                        os._exit(73)
                    original_execute=CheckExecutor.execute
                    original_capture=case.runs._capture
                    if edge=='reserved':
                        patcher=patch.object(case.runs.staging,'stage',side_effect=lambda *args:crash())
                    elif edge=='started':
                        def execute(executor,step,stage,cancel,on_start):
                            def started(pid,identity): on_start(pid,identity); crash()
                            return original_execute(executor,step,stage,cancel,started)
                        patcher=patch.object(CheckExecutor,'execute',execute)
                    elif edge=='captured':
                        def capture(*args): original_capture(*args); crash()
                        patcher=patch.object(case.runs,'_capture',capture)
                    else:
                        patcher=patch.object(case.runs,'_finish',side_effect=lambda *args:crash())
                    with patcher:
                        case.runs.start(case.req)
                        time.sleep(30)
                    raise RuntimeError('crash hook not reached')
                ''')
                result=subprocess.run([sys.executable,'-c',script,edge,str(report)],
                    env=dict(os.environ,SOLACE_TEST_TMPDIR='/tmp',PYTHONDONTWRITEBYTECODE='1',
                             PYTHONPATH='tests/solace_core:.'),capture_output=True,timeout=15)
                self.assertEqual(result.returncode,73,result.stderr.decode())
                info=json.loads(report.read_text()); crashed_base=Path(info['base'])
                self.addCleanup(shutil.rmtree,crashed_base,ignore_errors=True)
                registry=Registry(Path(info['registry']),self.policy)
                restarted=CoreService(registry,config=CoreConfig(sync_roots=(),workspace_root=crashed_base/'workspaces'))
                try:
                    catalog=restarted.catalog(info['project'])
                    runs=CheckRuns(restarted,catalog)
                    run=runs.show(info['run'])
                    self.assertEqual(run['state'],'interrupted')
                    self.assertEqual(catalog.get_operation(run['operation_id'])['state'],'interrupted')
                    self.assertEqual(runs.start(info['request']),dict(operation_id=run['operation_id'],run_id=info['run']))
                    self.assertEqual(len(run['steps']),2 if edge=='before_commit' else 0)
                    if edge!='reserved': self.assertTrue(Path(run['staging_path']).is_dir())
                    if edge=='captured': self.assertEqual((Path(info['store'])/'check-evidence'/info['run']/'00.stdout').read_bytes(),b'first\n')
                    if edge=='started': self.assertEqual(run['recovery']['survivor'],'signalled_retained')
                finally: restarted.close()

    def test_profile_edit_while_queued_is_stale_before_launch(self):
        self.prepared_run(['print("not launched")'])
        with patch.object(self.service.coordinator(self.project),'submit'):
            accepted=self.runs.start(self.req)
        profiles=CheckProfiles(self.catalog)
        params=dict(action='update',profile_id=self.profile,definition=dict(self.definition,name='queued change'))
        preview=profiles.preview(self.request('project.check-profile.preview',**params))
        profiles.apply(self.request('project.check-profile.apply',**params,confirmed=True,plan_token=preview['plan_token']))
        with patch.object(CheckExecutor,'execute') as execute:
            self.runs.run(accepted['operation_id'],self.req)
        execute.assert_not_called()
        self.assertEqual(self.runs.show(accepted['run_id'])['state'],'stale')

    def test_approved_plan_binding_is_reusable_but_cannot_start_checks(self):
        from solace_core.errors import CoreError
        self.prepared_run()
        with self.catalog.connect() as db:
            profile=CheckProfiles.active(db,self.profile)
        before=self.runs.binding(self.plan,profile)
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_plans SET state='approved' WHERE id=?",(self.plan.id,))
        approved=self.plans.load(self.plan.id,1)
        self.assertEqual(self.runs.binding(approved,profile),before)
        with self.assertRaises(CoreError): self.runs.start(self.req)

    def test_candidate_or_plan_revision_changed_during_execution_is_stale(self):
        from dataclasses import replace
        self.prepared_run(['print("ok")'])
        original=CheckExecutor.execute
        for changed in ('candidate','revision'):
            with self.subTest(changed=changed):
                def change(executor,*args):
                    result=original(executor,*args)
                    if changed=='revision':
                        with self.catalog.connect(write=True) as db:
                            db.execute('UPDATE integration_plans SET current_revision=2 WHERE id=?',(self.plan.id,))
                    else:
                        load=self.runs.plans.load
                        def altered(*args):
                            plan=load(*args)
                            return replace(plan,candidate=replace(plan.candidate,manifest_sha256='0'*64))
                        self.runs.plans.load=altered
                    return result
                req=self.request('integration.check.run',plan=self.plan.id,revision=1,profile=self.profile)
                with patch.object(CheckExecutor,'execute',change):
                    result=self.finish(self.runs.start(req))
                self.assertEqual(result['state'],'stale')
                self.runs=CheckRuns(self.service,self.catalog)
                with self.catalog.connect(write=True) as db:
                    db.execute('UPDATE integration_plans SET current_revision=1 WHERE id=?',(self.plan.id,))
