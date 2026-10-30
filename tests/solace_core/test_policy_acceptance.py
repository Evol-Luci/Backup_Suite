"""Policy acceptance via subprocess CLI and a real disposable Unix socket."""
import time
from pathlib import Path
import test_check_acceptance as fixture
from integration_fixture import IntegrationCase

class PolicyAcceptanceTests(IntegrationCase):
    def setUp(self):
        import os, threading
        from solace_core.server import SocketServer, bind_private_socket
        super().setUp()
        self.path=self.base/'core.sock'
        self.listener=bind_private_socket(self.path,self.base)
        self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.env=dict(os.environ,XDG_RUNTIME_DIR=str(self.base),XDG_DATA_HOME=str(self.base/'xdg'),HOME=str(self.base/'home'))
    stop_server=fixture.CheckAcceptanceTests.stop_server
    result=fixture.CheckAcceptanceTests.result
    run_cli=fixture.CheckAcceptanceTests.run_cli
    completed=fixture.CheckAcceptanceTests.completed
    failed=fixture.CheckAcceptanceTests.failed
    profile=fixture.CheckAcceptanceTests.profile
    selected_task=fixture.CheckAcceptanceTests.selected_task
    apply=fixture.CheckAcceptanceTests.apply
    checked_apply=fixture.CheckAcceptanceTests.checked_apply
    def set_policy(self,mode='manual',profile=None,opt=False):
        fields=['--mode',mode,'--allow-task-opt-in' if opt else '--no-allow-task-opt-in']
        if profile:fields+=['--required-profile',profile]
        preview=self.result('project','policy','preview',self.project,*fields)
        self.assertIn('authority_warning',preview)
        return self.result('project','policy','apply',self.project,*fields,'--plan-token',preview['plan_token'],'--confirmed')
    def queue(self,task):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            rows=self.result('integration','queue','list',self.project)['intents']
            matches=[r for r in rows if r['task_id']==task]
            if matches and matches[0]['stage'] in ('pending','pending_apply','done'):return matches[0]
            time.sleep(.05)
        self.fail(str(rows))
    def test_checked_child_and_root_wait_for_human_apply(self):
        self.assertEqual(self.result('project','policy','show',self.project)['mode'],'manual')
        profile=self.profile('assert open("candidate").read()=="exact candidate"')
        self.set_policy('checked_queue',profile)
        parent=self.result('task','create',self.project,'parent')['task_id']
        self.result('task','check-profile','select',self.project,parent,profile)
        parent_path=Path(self.completed('task','start',self.project,parent)['path'])
        child,_,_=self.selected_task(profile,parent)
        for task,target in ((child,parent_path),(parent,self.root)):
            if task==parent:self.completed('task','ready',self.project,parent)
            intent=self.queue(task)
            self.assertEqual(intent['stage'],'pending_apply',intent)
            self.assertFalse((target/'candidate').exists())
            plan=self.result('integration','show',self.project,intent['plan_id'])
            run=self.result('integration','check','show',self.project,intent['run_id'])
            self.assertEqual(run['state'],'passed')
            denied=self.failed('integration','approve',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--token',plan['review_token'],'--accept-unchecked')
            self.assertFalse(denied['ok'])
            self.checked_apply(plan,run)
            self.assertEqual((target/'candidate').read_text(),'exact candidate')
        self.set_policy()
        self.assertEqual(self.result('project','policy','show',self.project)['mode'],'manual')
    def test_failed_check_and_different_selection_remain_pending(self):
        bad=self.profile('raise SystemExit(9)')
        other=self.profile('pass')
        self.set_policy('checked_queue',bad)
        for selected,reason in ((bad,'check_failed'),(other,'profile')):
            task,_,_=self.selected_task(selected)
            intent=self.queue(task)
            self.assertEqual(intent['stage'],'pending',intent)
            self.assertIn(reason,intent['reason'])
            self.assertFalse((self.root/'candidate').exists())
    def test_task_override_confirmation_and_default_manual(self):
        profile=self.profile('pass')
        task,_,_=self.selected_task(profile)
        self.assertEqual(self.result('integration','queue','list',self.project)['intents'],[])
        self.set_policy('manual',profile,True)
        task=self.result('task','create',self.project,'override')['task_id']
        for mode in ('checked_queue','manual','inherit'):
            revision=self.result('task','show',self.project,task)['revision']
            fields=['--mode',mode,'--revision',str(revision)]
            preview=self.result('task','policy','preview',self.project,task,*fields)
            result=self.result('task','policy','apply',self.project,task,*fields,'--plan-token',preview['plan_token'],'--confirmed')
            self.assertEqual(result['integration_policy']['override'],mode)
        self.set_policy()
    def test_schema_four_upgrade_verified_backup(self):
        import shutil,threading
        from test_integration_schema import create_v2
        from solace_core.schema import preview_upgrade,apply_upgrade
        from solace_core.service import CoreService
        from solace_core.server import SocketServer,bind_private_socket
        from solace_core.catalog import readonly_database
        self.stop_server();self.service.close();self.path.unlink(missing_ok=True)
        registration=self.registry.get(self.project)
        store=Path(registration['store'])
        self.assertTrue(store.is_relative_to(self.base));shutil.rmtree(store)
        catalog=create_v2(registration,self.policy)
        for _ in range(2):
            preview=preview_upgrade(catalog)
            apply_upgrade(catalog,preview['plan_token'],self.request('project.upgrade',plan_token=preview['plan_token']))
        self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)
        self.listener=bind_private_socket(self.path,self.base);self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        task,_,_=self.selected_task(self.profile('pass'))
        self.assertEqual(self.result('task','policy','show',self.project,task)['mode'],'manual')
        preview=self.result('project','upgrade',self.project,'--preview')
        self.assertEqual((preview['from_schema'],preview['to_schema']),(4,5))
        result=self.result('project','upgrade',self.project,'--apply','--plan-token',preview['plan_token'])
        self.assertEqual(result['schema_version'],5)
        with readonly_database(Path(result['backup'])) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0],4)
        self.assertEqual(self.result('project','policy','show',self.project)['mode'],'manual')
    def test_restart_retains_single_run_and_target_staleness(self):
        import threading
        from solace_core.service import CoreService
        from solace_core.server import SocketServer,bind_private_socket
        counter=self.base/'executions'
        profile=self.profile('from pathlib import Path; p=Path('+repr(str(counter))+'); p.write_text(p.read_text()+"x" if p.exists() else "x")')
        self.set_policy('checked_queue',profile)
        task,_,_=self.selected_task(profile)
        first=self.queue(task)
        self.assertEqual(first['stage'],'pending_apply')
        self.assertEqual(counter.read_text(),'x')
        self.stop_server();self.service.close();self.path.unlink(missing_ok=True)
        self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)
        self.listener=bind_private_socket(self.path,self.base);self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        again=self.queue(task)
        self.assertEqual(again['run_id'],first['run_id'])
        self.assertEqual(counter.read_text(),'x')
        (self.root/'candidate').write_text('changed externally')
        plan=self.result('integration','show',self.project,first['plan_id'])
        denied=self.failed('integration','approve',self.project,plan['plan_id'],'--revision',str(plan['revision']),'--token',plan['review_token'],'--check-run',first['run_id'])
        self.assertFalse(denied['ok'])
        self.assertEqual(counter.read_text(),'x')
    def test_missing_selection_and_conflict_pending(self):
        profile=self.profile('pass')
        self.set_policy('checked_queue',profile)
        task,_,_=self.ready_cli_task({'candidate':b'missing selection'})
        intent=self.queue(task)
        self.assertEqual(intent['stage'],'pending')
        self.assertIsNone(intent['run_id'])
        (self.root/'binary').write_bytes(b'\0base')
        task=self.result('task','create',self.project,'conflict')['task_id']
        self.result('task','check-profile','select',self.project,task,profile)
        path=Path(self.completed('task','start',self.project,task)['path'])
        (path/'binary').write_bytes(b'\0incoming')
        (self.root/'binary').write_bytes(b'\0current')
        self.completed('task','ready',self.project,task)
        intent=self.queue(task)
        self.assertEqual(intent['stage'],'pending')
        self.assertIn('conflict',intent['reason'])
        self.assertIsNone(intent['run_id'])
        self.assertEqual((self.root/'binary').read_bytes(),b'\0current')
    def test_policy_and_profile_staleness_and_ready_replay(self):
        import uuid
        profile=self.profile('pass')
        self.set_policy('checked_queue',profile)
        task=self.result('task','create',self.project,'replay')['task_id']
        self.result('task','check-profile','select',self.project,task,profile)
        self.completed('task','start',self.project,task)
        revision=self.result('task','show',self.project,task)['revision']
        args=('task','ready',self.project,task,'--revision',str(revision),'--request-id',str(uuid.uuid4()))
        first=self.result(*args)
        self.assertEqual(self.result(*args),first)
        intent=self.queue(task)
        self.assertEqual(intent['stage'],'pending_apply')
        self.assertEqual(len(self.result('integration','check','list',self.project,intent['plan_id'])['runs']),1)
        self.set_policy('manual',profile)
        stale=self.result('integration','queue','show',self.project,intent['id'])
        self.assertTrue(stale['stale'])
        self.set_policy('checked_queue',profile)
        task,_,_=self.selected_task(profile)
        intent=self.queue(task)
        fields=('retire','--profile-id',profile)
        preview=self.result('project','check-profile','preview',self.project,*fields)
        self.result('project','check-profile','apply',self.project,*fields,'--plan-token',preview['plan_token'],'--confirmed')
        stale=self.result('integration','queue','show',self.project,intent['id'])
        self.assertTrue(stale['stale'])
        self.assertFalse((self.root/'candidate').exists())
