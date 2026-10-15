import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from solace_core.service import CoreService
from solace_core.registry import Registry
from solace_core.storage import StoragePolicy
from solace_engine.repository import ProjectRepository


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base/'source'; self.root.mkdir()
        self.policy = StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\n', sync_roots=[])
        self.registry = Registry(self.base/'data/registry.sqlite3', self.policy)
        self.service = CoreService(self.registry)
        self.addCleanup(self.service.close)
        self.project = self.call('project.add', root=str(self.root))['id']

    def call(self, method, request_id=None, **params):
        result = self.service.handle(dict(version=1,id=request_id or str(uuid.uuid4()),method=method,params=params,actor='test'))
        self.assertTrue(result['ok'], result)
        return result['result']

    def wait_operation(self, operation, project=None):
        catalog = self.service.catalog(project or self.project)
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            result = catalog.get_operation(operation)
            if result['state'] not in ('queued','running'): return result
            with catalog.condition: catalog.condition.wait(.05)
        self.fail('operation did not finish')

    def test_checkpoint_list_verify_restore_and_preserved_unregister(self):
        (self.root/'a').write_text('saved')
        operation = self.call('checkpoint.create',project=self.project,message='first')['operation_id']
        completed = self.wait_operation(operation)
        self.assertEqual(completed['state'],'succeeded')
        checkpoint = completed['result']['checkpoint_id']
        self.assertEqual(self.call('checkpoint.list',project=self.project)['checkpoints'][0]['id'],checkpoint)
        verification = self.call('checkpoint.verify',project=self.project,checkpoint=checkpoint)['operation_id']
        self.assertTrue(self.wait_operation(verification)['result']['ok'])
        destination = self.base/'restored'
        restore = self.call('checkpoint.restore',project=self.project,checkpoint=checkpoint,destination=str(destination))['operation_id']
        self.assertEqual(self.wait_operation(restore)['state'],'succeeded')
        self.assertEqual((destination/'a').read_text(),'saved')
        self.assertGreater(self.call('checkpoint.usage',project=self.project)['logical_bytes'],0)
        record = self.registry.get(self.project)
        self.call('project.unregister',project=self.project)
        self.assertTrue(Path(record['store']).exists())

    def test_committed_result_is_readable_before_worker_cleanup(self):
        catalog=self.service.catalog(self.project)
        committed,release=threading.Event(),threading.Event()
        original=catalog.notify
        def notify():
            with catalog.connect() as db:
                succeeded=db.execute("SELECT 1 FROM operations WHERE state='succeeded'").fetchone()
            if succeeded:
                committed.set();release.wait(5)
            original()
        try:
            with patch.object(catalog,'notify',side_effect=notify):
                operation=self.call('checkpoint.create',project=self.project,message='published')['operation_id']
                self.assertTrue(committed.wait(3))
                self.assertEqual(len(self.call('checkpoint.list',project=self.project)['checkpoints']),1)
        finally: release.set()

    def test_completed_restore_retry_returns_original_operation(self):
        capture=self.call('checkpoint.create',project=self.project,message='base')['operation_id']
        checkpoint=self.wait_operation(capture)['result']['checkpoint_id']
        reqid=str(uuid.uuid4())
        params=dict(project=self.project,checkpoint=checkpoint,destination=str(self.base/'retry-destination'))
        accepted=self.call('checkpoint.restore',reqid,**params)
        self.assertEqual(self.wait_operation(accepted['operation_id'])['state'],'succeeded')
        self.assertEqual(self.call('checkpoint.restore',reqid,**params),accepted)

    def test_blocked_project_does_not_block_another_and_busy_relink_refused(self):
        other_root = self.base/'other'; other_root.mkdir()
        other = self.call('project.add',root=str(other_root))['id']
        entered, release = threading.Event(), threading.Event()
        original = ProjectRepository._scan
        def scan(repo, exclusions):
            if repo.project_root == self.root:
                entered.set(); release.wait(5)
            return original(repo,exclusions)
        try:
            with patch.object(ProjectRepository,'_scan',scan):
                first = self.call('checkpoint.create',project=self.project,message='one')['operation_id']
                self.assertTrue(entered.wait(3))
                req = dict(version=1,id=str(uuid.uuid4()),method='project.unregister',params={'project':self.project},actor='test')
                self.assertEqual(self.service.handle(req)['error']['code'],'PROJECT_BUSY')
                second = self.call('checkpoint.create',project=other,message='two')['operation_id']
                self.assertEqual(self.wait_operation(second,other)['state'],'succeeded')
        finally: release.set()
        self.assertEqual(self.wait_operation(first)['state'],'succeeded')

    def test_same_project_retries_do_not_duplicate_checkpoints(self):
        entered, release = threading.Event(), threading.Event()
        original = ProjectRepository._scan
        def scan(repo, exclusions):
            entered.set(); release.wait(5); return original(repo, exclusions)
        request_id = str(uuid.uuid4())
        try:
            with patch.object(ProjectRepository,'_scan',scan):
                first = self.call('checkpoint.create',request_id,project=self.project,message='one')
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.call('checkpoint.create',request_id,project=self.project,message='one'),first)
        finally: release.set()
        self.wait_operation(first['operation_id'])
        self.assertEqual(len(self.call('checkpoint.list',project=self.project)['checkpoints']),1)

    def test_missing_store_and_invalid_method_are_structured(self):
        record = self.registry.get(self.project); shutil.rmtree(record['store'])
        req = dict(version=1,id=str(uuid.uuid4()),method='checkpoint.create',params={'project':self.project,'message':'x'},actor='test')
        self.assertEqual(self.service.handle(req)['error']['code'],'STORE_UNAVAILABLE')
        self.assertFalse(Path(record['store']).exists())
        req['method']='bad'
        self.assertEqual(self.service.handle(req)['error']['code'],'INVALID_REQUEST')

    def test_single_core_lock_and_restart_interruption(self):
        with self.assertRaises(Exception): CoreService(self.registry)
        catalog=self.service.catalog(self.project)
        request=dict(version=1,id=str(uuid.uuid4()),method='checkpoint.restore',params={'project':self.project,'checkpoint':'none','destination':str(self.base/'evidence')},actor='test')
        operation=catalog.accept_operation(request)['operation_id']
        catalog.transition(operation,'running')
        destination=self.base/'evidence';destination.mkdir();(destination/'keep').write_text('keep')
        self.service.close()
        self.service=CoreService(self.registry);self.addCleanup(self.service.close)
        result=self.service.catalog(self.project).get_operation(operation)
        self.assertEqual(result['state'],'interrupted')
        self.assertEqual(result['params']['destination'],str(destination))
        self.assertEqual((destination/'keep').read_text(),'keep')

    def test_subprocess_crash_before_and_after_checkpoint_commit(self):
        self.service.close()
        for phase in ['before','after']:
            req_id=str(uuid.uuid4())
            script='''
import os,sys,uuid
from solace_core.registry import Registry
from solace_core.storage import StoragePolicy
from solace_core.service import CoreService
from solace_engine.repository import ProjectRepository
policy=StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\\n',sync_roots=[])
s=CoreService(Registry(sys.argv[1],policy))
original=ProjectRepository.create_checkpoint
phase=sys.argv[3]
def capture(repo,message,exclusions=(),*,publication_hook=None,**kwargs):
    def hook(db,cp):
        publication_hook(db,cp)
        if phase=='before': os._exit(61)
    result=original(repo,message,exclusions,publication_hook=hook,**kwargs)
    os._exit(62)
ProjectRepository.create_checkpoint=capture
s.handle(dict(version=1,id=sys.argv[4],method='checkpoint.create',params={'project':sys.argv[2],'message':'crash'},actor='test'))
import threading
threading.Event().wait(10)
'''
            proc=subprocess.run([sys.executable,'-c',script,str(self.registry.path),self.project,phase,req_id],timeout=15,capture_output=True)
            self.assertEqual(proc.returncode,61 if phase=='before' else 62,proc.stderr.decode())
            service=CoreService(self.registry)
            try:
                catalog=service.catalog(self.project)
                with catalog.connect() as db: row=db.execute('SELECT id FROM operations WHERE request_id=?',(req_id,)).fetchone()
                result=catalog.get_operation(row[0])
                self.assertEqual(result['state'],'interrupted' if phase=='before' else 'succeeded')
                self.assertEqual(len(catalog.open_existing().list_checkpoints()),0 if phase=='before' else 1)
            finally: service.close()
