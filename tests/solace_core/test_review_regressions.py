"""Independent-review failures: lifecycle races, receipts, and offline diagnosis."""
import json
import os
import subprocess
import sys
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import test_service
from solace_core.registry import Registry
from solace_core.diagnostics import inspect_registry
from solace_core.service import CoreService
from solace_engine.repository import ProjectRepository


class ReviewRegressionTests(unittest.TestCase):
    setUp = test_service.ServiceTests.setUp
    call = test_service.ServiceTests.call
    wait_operation = test_service.ServiceTests.wait_operation

    def request(self, method, **params):
        return dict(version=1,id=str(uuid.uuid4()),method=method,params=params,actor='review')

    def test_relink_cannot_accept_checkpoint_against_stale_catalog(self):
        (self.root/'content').write_text('OLD ROOT')
        new = self.base/'new-root';new.mkdir();(new/'content').write_text('NEW ROOT')
        registration=self.registry.get(self.project)
        selected, release, scanning, finish_scan, relink_done = (threading.Event() for _ in range(5))
        original_catalog, original_scan = self.service.catalog, ProjectRepository._scan
        observed={}
        def delayed_catalog(project):
            result=original_catalog(project)
            if threading.current_thread().name=='delayed-capture':
                selected.set();release.wait(5)
            return result
        def delayed_scan(repo, exclusions):
            observed['root']=str(repo.project_root);scanning.set();finish_scan.wait(5)
            return original_scan(repo,exclusions)
        def capture():
            observed['capture']=self.service.handle(self.request('checkpoint.create',project=self.project,message='race'))
        def relink():
            observed['relink']=self.service.handle(self.request('project.relink',project=self.project,
                root=str(new),store=registration['store'],confirmed_root=str(new)))
            relink_done.set()
        first=threading.Thread(target=capture,name='delayed-capture')
        second=threading.Thread(target=relink,name='relink')
        try:
            with patch.object(self.service,'catalog',side_effect=delayed_catalog), patch.object(ProjectRepository,'_scan',delayed_scan):
                first.start();self.assertTrue(selected.wait(3));second.start()
                # Original code commits relink here; correctly locked lookup makes it wait.
                relink_done.wait(.2);release.set()
                self.assertTrue(scanning.wait(3));self.assertTrue(relink_done.wait(3))
                if observed['relink']['ok']:
                    self.assertEqual(observed['root'],str(new))
                else:
                    self.assertEqual(observed['relink']['error']['code'],'PROJECT_BUSY')
                    self.assertEqual(observed['root'],str(self.root))
        finally:
            release.set();finish_scan.set();first.join(5);second.join(5)

    def test_offline_registry_doctor_changes_no_files(self):
        path=self.base/'offline/registry.sqlite3'
        Registry(path,self.policy)
        before={p.relative_to(path.parent):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
        readonly=Registry(path,self.policy,read_only=True)
        report=inspect_registry(readonly)
        self.assertTrue(report['ok'],report)
        after={p.relative_to(path.parent):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
        self.assertEqual(set(after),set(before))
        self.assertEqual(after,before)

    def test_checkpoint_receipt_replays_after_source_move(self):
        request=self.request('checkpoint.create',project=self.project,message='saved')
        accepted=self.service.handle(request);self.assertTrue(accepted['ok'])
        self.wait_operation(accepted['result']['operation_id'])
        self.root.rename(self.base/'moved-source')
        replay=self.service.handle(request)
        self.assertTrue(replay['ok'],replay)
        self.assertEqual(replay['result'],accepted['result'])

    def test_checkpoint_receipt_replays_after_unregister_without_resurrection(self):
        request=self.request('checkpoint.create',project=self.project,message='saved')
        accepted=self.service.handle(request);self.assertTrue(accepted['ok'])
        self.wait_operation(accepted['result']['operation_id'])
        self.call('project.unregister',project=self.project)
        replay=self.service.handle(request)
        self.assertTrue(replay['ok'],replay)
        self.assertEqual(replay['result'],accepted['result'])
        self.assertEqual(self.registry.list(),[])
        fresh=self.service.handle(self.request('checkpoint.create',project=self.project,message='new'))
        self.assertEqual(fresh['error']['code'],'PROJECT_NOT_FOUND')

    def test_deferred_recovery_runs_when_original_store_returns(self):
        catalog=self.service.catalog(self.project)
        operation=catalog.accept_operation(self.request('checkpoint.create',project=self.project,message='crash'))['operation_id']
        catalog.transition(operation,'running')
        store=Path(self.registry.get(self.project)['store'])
        self.service.close();store.rename(self.base/'offline-store')
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.service=CoreService(self.registry)
        self.addCleanup(self.service.close)
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        (self.base/'offline-store').rename(store)
        outcome=self.call('operation.show',project=self.project,operation=operation)
        self.assertEqual(outcome['state'],'interrupted')
        self.assertEqual(outcome['error']['code'],'OPERATION_INTERRUPTED')
        self.call('project.unregister',project=self.project)

    def test_doctor_inspects_registry_without_runtime_environment(self):
        data=self.base/'diagnostic-data'
        Registry(data/'solace/registry.sqlite3',self.policy)
        env=dict(os.environ,XDG_DATA_HOME=str(data));env.pop('XDG_RUNTIME_DIR',None)
        result=subprocess.run([sys.executable,'-m','solace_core.cli','doctor','--json'],env=env,
                              capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,3,result.stderr)
        output=json.loads(result.stdout)
        self.assertIn('result',output)
        self.assertTrue(output['result']['registry']['ok'],output)
        self.assertEqual(output['result']['core']['error']['code'],'CORE_UNAVAILABLE')

    def test_unavailable_store_can_be_relinked_and_recovered(self):
        catalog=self.service.catalog(self.project)
        operation=catalog.accept_operation(self.request('checkpoint.create',project=self.project,message='crash'))['operation_id']
        catalog.transition(operation,'running')
        original=Path(self.registry.get(self.project)['store'])
        self.service.close();moved=self.base/'moved-store';original.rename(moved)
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.service=CoreService(self.registry)
        self.addCleanup(self.service.close)
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        result=self.service.handle(self.request('project.relink',project=self.project,
            root=str(self.root),store=str(moved),confirmed_root=str(self.root)))
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.call('operation.show',project=self.project,operation=operation)['state'],'interrupted')

    def test_unavailable_store_unregister_refused_when_task_ownership_is_unknown(self):
        original=Path(self.registry.get(self.project)['store'])
        self.service.close();moved=self.base/'moved-store';original.rename(moved)
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.service=CoreService(self.registry)
        self.addCleanup(self.service.close)
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        result=self.service.handle(self.request('project.unregister',project=self.project))
        self.assertFalse(result['ok'],result)
        self.assertEqual(result['error']['code'],'STORE_UNAVAILABLE')
        self.assertTrue(moved.exists())
        self.assertEqual(len(self.registry.list()),1)
