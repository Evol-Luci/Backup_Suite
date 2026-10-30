import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from unittest.mock import patch
from task_fixture import TaskCase
from solace_core.server import SocketServer,bind_private_socket

class TaskCLITests(TaskCase):
    def test_task_listing_pages_large_cached_views(self):
        from solace_core.client import CoreClient
        client=CoreClient(self.path);tasks=[]
        for index in range(30):
            task=self.create_task('aggregate '+str(index));started=self.start(task);tasks.append(task)
            report=dict(checkpoint_id=started['base_checkpoint'],observed_at='2026-10-15T00:00:00+00:00',
                        clean=False,added=[str(i)+'x'*3990 for i in range(32)],removed=[],changed=[],warnings=[],logical_bytes=0)
            with self.service.catalog(self.project).connect(write=True) as db:
                db.execute('UPDATE workspaces SET observed_json=? WHERE task_id=?',(json.dumps(report),task))
        first=client.request('task.list',dict(project=self.project))
        self.assertTrue(first['ok'],first)
        seen=[];page=first['result']
        while True:
            seen += [task['id'] for task in page['tasks']]
            if page['next_after'] is None:break
            value=client.request('task.list',dict(project=self.project,after=page['next_after'],limit=5))
            self.assertTrue(value['ok'],value);page=value['result']
        self.assertEqual(seen,tasks)
        self.assertEqual(len(self.result('task','list',self.project,'--limit','5')['tasks']),5)

    def test_oversized_response_preserves_request_correlation(self):
        from solace_core.client import CoreClient
        from solace_core.protocol import MAX_FRAME
        with patch.object(self.service,'_dispatch',return_value={'oversized':'x'*MAX_FRAME}):
            value=CoreClient(self.path).request('core.status',{})
        self.assertEqual(value['error']['code'],'RESPONSE_TOO_LARGE',value)

    def test_large_observation_stays_readable_and_all_changes_can_be_paged(self):
        from solace_core.client import CoreClient
        client=CoreClient(self.path)
        task=self.create_task();workspace=Path(self.start(task)['path'])
        names=[str(i).zfill(5)+'x'*180 for i in range(6000)]
        for name in names:(workspace/name).touch()
        operation=self.call('workspace.status',project=self.project,task=task,refresh=True)['operation_id']
        self.assertEqual(self.wait_operation(operation)['state'],'succeeded')
        for method,params in [('operation.show',dict(operation=operation)),('task.show',dict(task=task)),('workspace.status',dict(task=task))]:
            value=client.request(method,dict(project=self.project,**params))
            self.assertTrue(value['ok'],value)
        view=client.request('task.show',dict(project=self.project,task=task))['result']
        self.assertEqual(view['workspace']['last_observed']['counts']['added'],6000)
        seen=[];cursor=0;token=None
        while True:
            params=dict(project=self.project,task=task,after=cursor,limit=128)
            if token:params['observation']=token
            result=client.request('workspace.status',params);self.assertTrue(result['ok'],result)
            page=result['result']['changes'];token=page['observation']
            seen += [row['path'] for row in page['entries'] if row['kind']=='added']
            cursor=page['next_after']
            if cursor is None:break
        self.assertEqual(seen,names)
        # Revision inference and a new operation must remain usable after refresh.
        self.result('task','block',self.project,task,'--reason','large tree still manageable')
        ready=self.result('task','ready',self.project,task,'--wait');self.assertEqual(ready['state'],'succeeded')
        workspace.joinpath('new').touch()
        self.wait_operation(self.call('workspace.status',project=self.project,task=task,refresh=True)['operation_id'])
        stale=client.request('workspace.status',dict(project=self.project,task=task,after=128,observation=token))
        self.assertEqual(stale['error']['code'],'OBSERVATION_CHANGED')

    def setUp(self):
        super().setUp();self.path=self.base/'core.sock'
        self.listener=bind_private_socket(self.path,self.base);self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        self.env=dict(os.environ,XDG_RUNTIME_DIR=str(self.base),XDG_DATA_HOME=str(self.base/'xdg'),HOME=str(self.base/'home'))

    def stop_server(self):
        self.server.close();self.thread.join(3);self.listener.close()

    def run_cli(self,*args,success=True):
        result=subprocess.run([sys.executable,'-m','solace_core.cli','--socket',str(self.path),*args],env=self.env,capture_output=True,timeout=15)
        if success: self.assertEqual(result.returncode,0,result.stderr.decode())
        return result

    def result(self,*args):
        return json.loads(self.run_cli(*args,'--json').stdout)['result']

    def test_cli_exact_prompt_and_lifecycle_round_trip(self):
        prompt=self.base/'prompt.txt';prompt.write_bytes('é\r\nno final newline'.encode())
        task=self.result('task','create',self.project,'work','--prompt-file',str(prompt))['task_id']
        self.assertEqual(self.run_cli('task','prompt','show',self.project,task).stdout,prompt.read_bytes())
        start=self.result('task','start',self.project,task,'--wait')
        self.assertEqual(start['state'],'succeeded');path=Path(start['result']['path']);(path/'change').write_text('edited')
        status=self.result('workspace','status',self.project,task,'--refresh','--wait')
        self.assertEqual(status['result']['added'],['change'])
        self.result('task','block',self.project,task,'--reason','question')
        ready=self.result('task','ready',self.project,task,'--wait');self.assertEqual(ready['state'],'succeeded')
        self.result('task','resume',self.project,task)
        self.result('task','prompt','delete',self.project,task)
        metadata=self.result('task','prompt','show',self.project,task)
        self.assertEqual(metadata['status'],'deleted');self.assertNotIn('body',metadata)
        self.result('project','prompt-policy','set',self.project,'none')
        self.assertEqual(self.result('project','prompt-policy','show',self.project)['policy'],'none')
        self.assertEqual(len(self.result('checkpoint','list',self.project,'--all-workspaces')['checkpoints']),2)

    def test_cli_revision_retry_and_old_core_capabilities(self):
        task=self.create_task();self.start(task);revision=self.show(task)['revision'];request=str(uuid.uuid4())
        args=('task','block',self.project,task,'--reason','decision','--revision',str(revision),'--request-id',request)
        first=self.result(*args);self.assertEqual(self.result(*args),first)
        failed=self.run_cli('task','resume',self.project,task,'--revision',str(revision),'--json',success=False)
        self.assertEqual(json.loads(failed.stdout)['error']['code'],'TASK_REVISION_CONFLICT')
        original=self.service._dispatch
        def old(request):
            result=original(request)
            if request['method']=='core.status':result.pop('capabilities',None)
            return result
        with patch.object(self.service,'_dispatch',side_effect=old):
            unsupported=self.run_cli('task','show',self.project,task,'--json',success=False)
            self.assertEqual(json.loads(unsupported.stdout)['error']['code'],'CORE_UPGRADE_REQUIRED')
            self.assertTrue(self.result('checkpoint','list',self.project)['checkpoints']==[])

    def test_install_preview_has_schema_boundary_and_does_not_install(self):
        result=subprocess.run([sys.executable,'packaging/install-core.py','--preview'],env=self.env,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr.decode());preview=json.loads(result.stdout)
        self.assertEqual(preview['core_schema_supported'],[1,2,3,4,5]);self.assertEqual(preview['core_schema_created'],5)
        self.assertIn('does not downgrade',preview['database_rollback'])
        self.assertFalse((self.base/'home/.local/bin/solace').exists())

    def test_invalid_prompt_file_does_not_appear_in_output(self):
        prompt=self.base/'invalid.txt';prompt.write_bytes(b'PRIVATE BAD\0CONTENT')
        result=self.run_cli('task','create',self.project,'work','--prompt-file',str(prompt),'--json',success=False)
        self.assertNotEqual(result.returncode,0);self.assertNotIn(b'PRIVATE BAD',result.stdout+result.stderr)

    def test_doctor_and_storage_report_schema_and_shared_extent_limit(self):
        task=self.create_task();self.start(task)
        report=self.result('doctor',self.project)
        self.assertEqual(report['projects'][0]['core_schema'],5)
        self.assertEqual(report['projects'][0]['tasks']['active'],1)
        self.assertIn('does not downgrade',report['projects'][0]['database_rollback'])
        usage=self.result('checkpoint','usage',self.project)
        self.assertIsNone(usage['unique_physical_bytes'])
        self.assertEqual(usage['shared_extent_accounting'],'unavailable')
        self.assertIn('workspace_logical_bytes',usage)
        self.assertEqual(self.result('project','show',self.project)['core_schema'],5)

    def test_wait_timeout_keeps_operation_id(self):
        from solace_core.task_cli import wait_for_operation
        accepted={'version':1,'id':str(uuid.uuid4()),'ok':True,'result':{'operation_id':'e'*32}}
        result=wait_for_operation(None,self.project,accepted,timeout=0)
        self.assertTrue(result['wait_timed_out']);self.assertEqual(result['result'],accepted['result'])

    def test_task_watch_refetches_body_free_state_after_durable_events(self):
        import select,time
        task=self.create_task(prompt='WATCH PRIVATE CANARY');self.start(task)
        process=subprocess.Popen([sys.executable,'-m','solace_core.cli','--socket',str(self.path),'task','show',self.project,task,'--watch','--json'],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        output=b''
        try:
            self.assertTrue(select.select([process.stdout],[],[],3)[0])
            output+=os.read(process.stdout.fileno(),65536)
            self.mutate('task.block',task,reason='watch update')
            deadline=time.monotonic()+3
            while b'"state": "blocked"' not in output and time.monotonic()<deadline:
                if select.select([process.stdout],[],[],0.1)[0]:output+=os.read(process.stdout.fileno(),65536)
            self.assertIn(b'"state": "blocked"',output)
            self.assertNotIn(b'WATCH PRIVATE CANARY',output)
        finally:
            process.terminate();process.communicate(timeout=3)

    def test_explicit_cli_migration_replays_without_a_second_backup(self):
        from test_task_schema import create_v1
        root=self.base/'v1-source';root.mkdir();store=self.base/'v1-store'
        record=dict(id=uuid.uuid4().hex,root=str(root),store=str(store),store_uuid=uuid.uuid4().hex,mount_id=1,created_at='2026-01-01T00:00:00+00:00')
        create_v1(record,self.policy)
        with self.registry.connect(write=True) as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)',tuple(record[k] for k in ('id','root','store','store_uuid','mount_id','created_at')))
        project=record['id'];preview=self.result('project','upgrade',project,'--preview')
        self.assertTrue(preview['required'])
        request=str(uuid.uuid4());args=('project','upgrade',project,'--apply','--plan-token',preview['plan_token'],'--request-id',request)
        applied=self.result(*args);self.assertEqual(self.result(*args),applied)
        self.assertEqual(len(list((store/'schema-backups').glob('*/checkpoints.sqlite3'))),1)
        self.assertEqual(self.result('project','show',project)['core_schema'],3)
        self.assertTrue(self.result('task','create',project,'upgraded')['task_id'])
