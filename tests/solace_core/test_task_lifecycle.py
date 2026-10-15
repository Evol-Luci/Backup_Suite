import shutil
from pathlib import Path
from task_fixture import TaskCase

class LifecycleTests(TaskCase):
    def test_transitions_preserve_ready_history_and_cancelled_workspace(self):
        (self.root/'file').write_text('source');task=self.create_task();started=self.start(task)
        self.mutate('task.block',task,reason='waiting');self.assertEqual(self.show(task)['state'],'blocked')
        ready=self.wait_operation(self.mutate('task.ready',task)['operation_id'])
        first=ready['result']['checkpoint_id']
        request=self.request('task.resume',task=task,expected_revision=self.show(task)['revision'])
        resumed=self.service.handle(request);self.assertTrue(resumed['ok'],resumed)
        self.mutate('task.block',task,reason='another decision')
        self.assertEqual(self.service.handle(request)['result'],resumed['result'])
        self.assertEqual(self.show(task)['state'],'blocked')
        again=self.wait_operation(self.mutate('task.ready',task)['operation_id'])['result']['checkpoint_id']
        self.assertEqual(self.show(task)['results'],[first,again])
        cancel=self.request('task.cancel',task=task,expected_revision=self.show(task)['revision'])
        cancelled=self.service.handle(cancel);self.assertTrue(cancelled['ok'],cancelled)
        self.assertTrue(Path(started['path']).exists())
        invalid=self.service.handle(self.request('task.resume',task=task,expected_revision=self.show(task)['revision']))
        self.assertEqual(invalid['error']['code'],'TASK_STATE_INVALID')
        new=self.create_task();reused=self.start(new,from_checkpoint=again)
        self.assertEqual(reused['base_checkpoint'],again)
        self.assertEqual(self.show(task)['state'],'cancelled')

    def test_parent_summary_ready_and_explicit_child_cancel(self):
        parent=self.create_task();self.start(parent);child=self.create_task(parent=parent)
        pending=self.service.handle(self.request('task.ready',task=parent,expected_revision=self.show(parent)['revision']))
        self.assertEqual(pending['error']['code'],'TASK_CHILDREN_PENDING')
        cannot_cancel=self.service.handle(self.request('task.cancel',task=parent,expected_revision=self.show(parent)['revision']))
        self.assertEqual(cannot_cancel['error']['code'],'TASK_CHILDREN_PENDING')
        self.assertEqual(self.show(parent)['aggregate'],'waiting_for_children')
        self.start(child);self.mutate('task.block',child,reason='question')
        self.assertEqual(self.show(parent)['aggregate'],'blocked')
        self.wait_operation(self.mutate('task.ready',child)['operation_id'])
        self.wait_operation(self.mutate('task.ready',parent)['operation_id'])
        summary=self.show(parent)
        self.assertEqual(summary['state'],'ready');self.assertEqual(summary['aggregate'],'awaiting_integration')
        self.assertEqual(summary['children']['ready'],1)
        self.mutate('task.cancel',child);self.mutate('task.cancel',parent)
        self.assertEqual(self.show(parent)['aggregate'],'cancelled')

    def test_unregister_requires_cancelled_tasks_and_preserves_receipts(self):
        task=self.create_task();self.start(task)
        refused=self.service.handle(self.request('project.unregister',project=self.project))
        self.assertEqual(refused['error']['code'],'PROJECT_BUSY')
        request=self.request('task.cancel',task=task,expected_revision=self.show(task)['revision'])
        cancelled=self.service.handle(request);self.assertTrue(cancelled['ok'],cancelled)
        unregister=self.request('project.unregister',project=self.project)
        removed=self.service.handle(unregister);self.assertTrue(removed['ok'],removed)
        self.assertEqual(self.service.handle(unregister),removed)
        self.assertEqual(self.service.handle(request),cancelled)
        self.assertEqual(self.show(task)['state'],'cancelled');self.assertEqual(self.registry.list(),[])
        fresh=self.service.handle(self.request('task.create',title='new'))
        self.assertEqual(fresh['error']['code'],'PROJECT_NOT_FOUND')

    def test_relink_requires_pause_confirmation_and_preserves_history_with_dirty_content(self):
        (self.root/'file').write_text('source');task=self.create_task();started=self.start(task)
        self.mutate('task.block',task,reason='paused')
        old=self.show(task);head=old['workspace']['head_checkpoint']
        previous=Path(started['path'])
        for count in range(2):
            candidate=self.base/f'relinked-{count}';shutil.copytree(previous,candidate,symlinks=True)
            (candidate/'file').write_text('dirty external files')
            params=dict(project=self.project,task=task,expected_revision=self.show(task)['revision'],path=str(candidate),confirmed_path=str(candidate),checkpoint=head,writers_paused=True)
            bad=self.service.handle(self.request('workspace.relink',**dict(params,writers_paused=False)))
            self.assertFalse(bad['ok'])
            accepted=self.call('workspace.relink',**params);outcome=self.wait_operation(accepted['operation_id'])
            self.assertEqual(outcome['state'],'succeeded',outcome)
            self.assertFalse(outcome['result']['comparison']['clean'])
            view=self.show(task);self.assertEqual(view['workspace']['path'],str(candidate))
            self.assertEqual(view['workspace']['id'],old['workspace']['id']);self.assertEqual(view['workspace']['head_checkpoint'],head)
            self.assertTrue(previous.exists());previous=candidate
