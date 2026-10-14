import json
import os
import subprocess
import sys
import unittest
import uuid
import test_socket
from solace_core.diagnostics import inspect, inspect_registry


class CLITests(unittest.TestCase):
    setUp = test_socket.SocketTests.setUp
    setUpService = test_socket.SocketTests.setUpService
    stop_server = test_socket.SocketTests.stop_server
    call = test_socket.SocketTests.call
    wait_operation = test_socket.SocketTests.wait_operation

    def run_cli(self,*args,legacy=False):
        env=dict(os.environ,XDG_RUNTIME_DIR=str(self.base),XDG_DATA_HOME=str(self.base/'xdg'))
        target=['solace_backup_cli.py'] if legacy else ['-m','solace_core.cli']
        return subprocess.run([sys.executable,*target,'--socket',str(self.path),*args],env=env,capture_output=True,text=True,timeout=10)

    def test_cli_json_flags_routes_and_async_result(self):
        for args in [('--json','core','status'),('core','status','--json')]:
            result=self.run_cli(*args)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue(json.loads(result.stdout)['ok'])
        result=self.run_cli('checkpoint','create',self.project,'message','--json')
        self.assertEqual(result.returncode,0,result.stderr)
        operation=json.loads(result.stdout)['result']['operation_id']
        self.wait_operation(operation)
        result=self.run_cli('operation','show',self.project,operation,'--json')
        self.assertEqual(json.loads(result.stdout)['result']['state'],'succeeded')
        result=self.run_cli('project','list','--json',legacy=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['result']['projects']),1)

    def test_noninteractive_relink_requires_exact_confirmation(self):
        record=self.registry.get(self.project)
        result=self.run_cli('project','relink',self.project,'--root',str(self.root),'--store',record['store'],'--json')
        self.assertEqual(result.returncode,2)
        self.assertEqual(json.loads(result.stdout)['error']['code'],'CONFIRMATION_REQUIRED')
        result=self.run_cli('project','relink',self.project,'--root',str(self.root),'--store',record['store'],
                            '--confirm-root',str(self.root),'--json')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_unavailable_core_and_readonly_offline_doctor(self):
        self.stop_server()
        self.service.close()
        result=self.run_cli('core','status','--json')
        self.assertEqual(result.returncode,3)
        self.assertEqual(json.loads(result.stdout)['error']['code'],'CORE_UNAVAILABLE')
        result=self.run_cli('doctor','--json')
        self.assertEqual(result.returncode,3)
        output=json.loads(result.stdout)
        self.assertFalse(output['result']['core']['ok'])
        self.assertFalse((self.base/'xdg').exists())

    def test_diagnostics_do_not_change_corrupt_or_newer_store(self):
        import sqlite3
        from contextlib import closing
        record=self.registry.get(self.project)
        self.service.close()
        store=self.base/'data/projects'/self.project
        def contents(): return {p.relative_to(store):p.read_bytes() for p in store.rglob('*') if p.is_file()}
        with closing(sqlite3.connect(store/'checkpoints.sqlite3')) as db,db:
            db.execute('PRAGMA user_version=2')
        before=contents();report=inspect(record,self.policy)
        self.assertFalse(report['ok'])
        self.assertEqual(report['error']['code'],'SCHEMA_VERSION_UNSUPPORTED')
        self.assertEqual(contents(),before)
        (store/'checkpoints.sqlite3').write_bytes(b'corrupt')
        before=contents();report=inspect(record,self.policy)
        self.assertEqual(report['error']['code'],'DATABASE_CORRUPT')
        self.assertEqual(contents(),before)

    def test_unregister_retry_by_id_replays_receipt(self):
        request_id=str(uuid.uuid4())
        first=self.run_cli('project','unregister',self.project,'--request-id',request_id,'--json')
        second=self.run_cli('project','unregister',self.project,'--request-id',request_id,'--json')
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertEqual(json.loads(first.stdout)['result'],json.loads(second.stdout)['result'])
