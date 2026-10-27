import json
import threading
from pathlib import Path
from unittest.mock import patch
from task_fixture import TaskCase

class TaskOperationTests(TaskCase):
    def test_start_ready_status_and_result_history(self):
        (self.root/'shared.txt').write_text('source')
        task=self.create_task(prompt='PRIVATE OP CANARY')
        started=self.start(task);path=Path(started['path'])
        self.assertNotEqual(path,self.root);self.assertEqual(self.show(task)['state'],'active')
        (path/'shared.txt').write_text('child only')
        refreshed=self.call('workspace.status',project=self.project,task=task,refresh=True)
        report=self.wait_operation(refreshed['operation_id'])
        self.assertEqual(report['state'],'succeeded',report)
        self.assertEqual(report['result']['changed'],[{'path':'shared.txt','reasons':['content']}])
        old=self.show(task)
        ready=self.wait_operation(self.mutate('task.ready',task)['operation_id'])
        self.assertEqual(ready['state'],'succeeded',ready)
        current=self.show(task)
        self.assertEqual(current['state'],'ready')
        self.assertNotEqual(current['workspace']['head_checkpoint'],old['workspace']['head_checkpoint'])
        self.assertEqual(current['results'],[ready['result']['checkpoint_id']])
        self.assertEqual((self.root/'shared.txt').read_text(),'source')
        self.assertNotIn('PRIVATE OP CANARY',json.dumps(self.service.catalog(self.project).events_after(0)))

    def test_ready_hook_failure_rolls_back_head_checkpoint_and_state(self):
        from solace_core.task_operations import TaskOperations
        (self.root/'file').write_text('base');task=self.create_task();self.start(task)
        catalog=self.service.catalog(self.project);before=self.show(task)
        count=len(catalog.open_existing().list_checkpoints())
        original=TaskOperations.complete_capture
        def fail(owner,db,operation_id,checkpoint,**kwargs):
            original(owner,db,operation_id,checkpoint,**kwargs)
            raise RuntimeError('before commit')
        with patch.object(TaskOperations,'complete_capture',fail):
            result=self.wait_operation(self.mutate('task.ready',task)['operation_id'])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(self.show(task)['state'],'active')
        self.assertEqual(self.show(task)['workspace']['head_checkpoint'],before['workspace']['head_checkpoint'])
        self.assertEqual(self.show(task)['results'],[])
        self.assertEqual(len(catalog.open_existing().list_checkpoints()),count)

    def test_pending_path_hidden_claims_guard_and_replay_precedes_availability(self):
        from solace_core.workspace_trees import WorkspaceBuilder
        entered,release=threading.Event(),threading.Event();original=WorkspaceBuilder.prepare
        task=self.create_task();revision=self.show(task)['revision']
        request=self.request('task.start',task=task,expected_revision=revision)
        def delayed(owner,*args):
            entered.set();release.wait(5);return original(owner,*args)
        try:
            with patch.object(WorkspaceBuilder,'prepare',delayed):
                accepted=self.service.handle(request);self.assertTrue(accepted['ok'],accepted)
                self.assertTrue(entered.wait(3));self.assertIsNone(self.show(task)['workspace'])
                self.assertEqual(self.service.handle(request),accepted)
                conflict=self.service.handle(self.request('task.start',task=task,expected_revision=revision))
                self.assertEqual(conflict['error']['code'],'TASK_BUSY')
        finally: release.set()
        self.assertEqual(self.wait_operation(accepted['result']['operation_id'])['state'],'succeeded')
        path=Path(self.show(task)['workspace']['path']);path.rename(path.with_name('moved-workspace'))
        self.assertEqual(self.service.handle(request),accepted)
        self.assertEqual(self.show(task)['workspace']['availability'],'unavailable')
