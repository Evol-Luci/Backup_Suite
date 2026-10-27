import json
import unittest
from unittest.mock import patch
from task_fixture import CatalogCase
from solace_core.errors import CoreError

class RecordTests(CatalogCase):
    def test_create_no_scan_private_view_and_retry_after_redaction(self):
        from solace_core.prompts import PromptStore
        canary='UNIQUE PRIVATE\nnot in receipts'
        request=self.request('task.create',title='Improve validation',prompt=canary)
        with patch('solace_engine.repository.ProjectRepository._scan',side_effect=AssertionError('scan')):
            first=self.tasks.create(request)
        view=self.tasks.show(first['task_id'])
        self.assertEqual(view['state'],'created');self.assertEqual(view['revision'],1)
        self.assertIsNone(view['workspace']);self.assertNotIn(canary.splitlines()[0],json.dumps(view))
        prompts=PromptStore(self.catalog)
        self.assertEqual(prompts.read(first['task_id'])['body'],canary)
        prompts.delete(self.request('task.prompt.delete',task=first['task_id'],expected_revision=1))
        self.assertEqual(self.tasks.create(request),first)
        self.assertEqual(prompts.read(first['task_id'])['status'],'deleted')
        with self.catalog.connect() as db:
            for table in ['operations','events','task_requests']:
                self.assertNotIn(canary.splitlines()[0],str([tuple(r) for r in db.execute('SELECT * FROM '+table)]))
        self.assertEqual(len(self.tasks.list()),1)

    def test_children_profile_revision_and_depth(self):
        parent=self.tasks.create(self.request('task.create',title='parent',exclusions=['*.tmp']))['task_id']
        child=self.tasks.create(self.request('task.create',title='child',parent=parent,expected_parent_revision=1))['task_id']
        self.assertEqual(self.tasks.show(parent)['revision'],2)
        self.assertEqual(self.tasks.show(child)['profile'],{'version':1,'protected':['.git','.solace_vault','solace_owned_roots'],'exclusions':['*.tmp']})
        for params,code in [
            (dict(parent=parent,expected_parent_revision=1),'TASK_REVISION_CONFLICT'),
            (dict(parent=child,expected_parent_revision=1),'TASK_DEPTH_UNSUPPORTED'),
            (dict(parent=parent,expected_parent_revision=2,exclusions=[]),'INVALID_REQUEST')]:
            with self.subTest(params=params),self.assertRaises(CoreError) as caught:
                self.tasks.create(self.request('task.create',title='invalid',**params))
            self.assertEqual(caught.exception.code,code)
        self.assertEqual(len(self.tasks.list()),2)

    def test_cross_namespace_request_id_and_fingerprint_conflicts(self):
        req=self.request('task.create',title='a');self.tasks.create(req)
        changed=dict(req,params=dict(req['params'],title='b'))
        with self.assertRaises(CoreError) as caught: self.tasks.create(changed)
        self.assertEqual(caught.exception.code,'REQUEST_ID_REUSED')
        operation=self.request('checkpoint.create',message='source')
        self.catalog.accept_operation(operation)
        other=dict(self.request('task.create',title='b'),id=operation['id'])
        with self.assertRaises(CoreError) as caught: self.tasks.create(other)
        self.assertEqual(caught.exception.code,'REQUEST_ID_REUSED')

    def test_creation_rollback_leaves_no_task_prompt_receipt_or_event(self):
        before=self.catalog.events_after(0)
        with patch.object(self.catalog,'_event',side_effect=RuntimeError('fail')):
            with self.assertRaises(RuntimeError): self.tasks.create(self.request('task.create',title='fail',prompt='private'))
        self.assertEqual(self.tasks.list(),[])
        self.assertEqual(self.catalog.events_after(0),before)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_prompts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_requests').fetchone()[0],0)

    def test_checkpoint_operation_cannot_reuse_a_task_request_id(self):
        req=self.request('task.create',title='a');self.tasks.create(req)
        operation=dict(self.request('checkpoint.create',message='capture'),id=req['id'])
        with self.assertRaises(CoreError) as caught:
            self.catalog.accept_operation(operation)
        self.assertEqual(caught.exception.code,'REQUEST_ID_REUSED')
