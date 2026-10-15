import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from task_fixture import TaskCase

class ConcurrencyTests(TaskCase):
    def test_same_revision_clients_cannot_overwrite_each_other(self):
        task=self.create_task();self.start(task);revision=self.show(task)['revision'];barrier=threading.Barrier(2)
        def block(reason):
            barrier.wait(3)
            return self.service.handle(self.request('task.block',task=task,expected_revision=revision,reason=reason))
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(block,['one','two']))
        self.assertEqual(sum(result['ok'] for result in results),1)
        self.assertEqual([result['error']['code'] for result in results if not result['ok']],['TASK_REVISION_CONFLICT'])
        self.assertEqual(self.show(task)['revision'],revision+1)

    def test_child_preparation_claims_parent_against_new_children_ready_and_cancel(self):
        from solace_core.workspace_trees import WorkspaceBuilder
        parent=self.create_task();self.start(parent);child=self.create_task(parent=parent)
        entered,release=threading.Event(),threading.Event();original=WorkspaceBuilder.prepare
        def delayed(owner,*args): entered.set();release.wait(5);return original(owner,*args)
        try:
            with patch.object(WorkspaceBuilder,'prepare',delayed):
                accepted=self.mutate('task.start',child,expected_parent_revision=self.show(parent)['revision'])
                self.assertTrue(entered.wait(3))
                for method in ['task.ready','task.cancel']:
                    response=self.service.handle(self.request(method,task=parent,expected_revision=self.show(parent)['revision']))
                    self.assertEqual(response['error']['code'],'TASK_BUSY')
                create=self.service.handle(self.request('task.create',title='race',parent=parent,expected_parent_revision=self.show(parent)['revision']))
                self.assertEqual(create['error']['code'],'TASK_BUSY')
        finally: release.set()
        self.assertEqual(self.wait_operation(accepted['operation_id'])['state'],'succeeded')
