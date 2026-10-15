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
        self.assertEqual(preview['core_schema_supported'],[1,2]);self.assertEqual(preview['core_schema_created'],2)
        self.assertIn('does not downgrade',preview['database_rollback'])
        self.assertFalse((self.base/'home/.local/bin/solace').exists())

    def test_invalid_prompt_file_does_not_appear_in_output(self):
        prompt=self.base/'invalid.txt';prompt.write_bytes(b'PRIVATE BAD\0CONTENT')
        result=self.run_cli('task','create',self.project,'work','--prompt-file',str(prompt),'--json',success=False)
        self.assertNotEqual(result.returncode,0);self.assertNotIn(b'PRIVATE BAD',result.stdout+result.stderr)

    def test_doctor_and_storage_report_schema_and_shared_extent_limit(self):
        task=self.create_task();self.start(task)
        report=self.result('doctor',self.project)
        self.assertEqual(report['projects'][0]['core_schema'],2)
        self.assertEqual(report['projects'][0]['tasks']['active'],1)
        self.assertIn('does not downgrade',report['projects'][0]['database_rollback'])
        usage=self.result('checkpoint','usage',self.project)
        self.assertIsNone(usage['unique_physical_bytes'])
        self.assertEqual(usage['shared_extent_accounting'],'unavailable')
        self.assertIn('workspace_logical_bytes',usage)
        self.assertEqual(self.result('project','show',self.project)['core_schema'],2)

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
        self.assertEqual(self.result('project','show',project)['core_schema'],2)
        self.assertTrue(self.result('task','create',project,'upgraded')['task_id'])
