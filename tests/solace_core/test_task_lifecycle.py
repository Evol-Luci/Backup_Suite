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

    def test_relink_private_validation_releases_locks_and_retains_candidate_ownership(self):
        import threading
        from unittest.mock import patch
        from solace_core.workspace_trees import validate_private_files
        (self.root/'file').write_text('source');task=self.create_task();started=self.start(task)
        self.mutate('task.block',task,reason='paused')
        candidate=self.base/'candidate';shutil.copytree(started['path'],candidate)
        request=self.request('workspace.relink',task=task,expected_revision=self.show(task)['revision'],
            path=str(candidate),confirmed_path=str(candidate),checkpoint=started['base_checkpoint'],writers_paused=True)
        entered=threading.Event();release=threading.Event();responses=[]
        def paused(path):
            entered.set()
            if not release.wait(5):raise RuntimeError('test release absent')
            return validate_private_files(path)
        def relink():responses.append(self.service.handle(request))
        with patch('solace_core.workspace_trees.validate_private_files',side_effect=paused):
            worker=threading.Thread(target=relink);worker.start()
            try:
                self.assertTrue(entered.wait(5))
                done=threading.Event();other=self.base/'unrelated';other.mkdir();registered=[]
                def register():
                    registered.append(self.service.handle(self.request('project.add',root=str(other))));done.set()
                placement=threading.Thread(target=register);placement.start()
                try:self.assertTrue(done.wait(2),'placement lock held during private-file validation')
                finally:
                    if not done.is_set():release.set()
                    placement.join(5)
                self.assertTrue(registered[0]['ok'],registered)
                catalog=self.service.catalog(self.project)
                with self.service.coordinator(self.project).lock,catalog.connect(write=True) as db:
                    intent=db.execute("SELECT phase,final_path FROM workspace_intents WHERE kind='relink'").fetchone()
                    self.assertEqual(tuple(intent),('reserved',str(candidate)))
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM task_claims WHERE task_id=?',(task,)).fetchone()[0],1)
                denied=self.service.handle(self.request('project.add',root=str(candidate)))
                self.assertFalse(denied['ok'],denied)
                self.assertEqual(denied['error']['code'],'INTEGRATION_TARGET_BUSY')
            finally:release.set();worker.join(5)
            self.assertTrue(responses[0]['ok'],responses)
            outcome=self.wait_operation(responses[0]['result']['operation_id'])
        self.assertEqual(outcome['state'],'succeeded',outcome)
        self.assertEqual(self.show(task)['workspace']['path'],str(candidate))

    def test_relink_rechecks_candidate_identity_after_private_validation(self):
        from unittest.mock import patch
        from solace_core.workspace_trees import validate_private_files
        (self.root/'file').write_text('source');task=self.create_task();started=self.start(task)
        self.mutate('task.block',task,reason='paused')
        candidate=self.base/'candidate';shutil.copytree(started['path'],candidate)
        preserved=self.base/'preserved-candidate'
        def replace_after_validation(path):
            validate_private_files(path)
            candidate.rename(preserved);candidate.mkdir();(candidate/'file').write_text('replacement')
        with patch('solace_core.workspace_trees.validate_private_files',side_effect=replace_after_validation):
            accepted=self.mutate('workspace.relink',task,path=str(candidate),confirmed_path=str(candidate),
                checkpoint=started['base_checkpoint'],writers_paused=True)
            outcome=self.wait_operation(accepted['operation_id'])
        self.assertEqual(outcome['state'],'failed',outcome)
        self.assertEqual(outcome['error']['code'],'WORKSPACE_IDENTITY_MISMATCH')
        self.assertEqual(self.show(task)['workspace']['path'],started['path'])
        self.assertEqual((candidate/'file').read_text(),'replacement')
        self.assertEqual((preserved/'file').read_text(),'source')

    def test_relink_can_adopt_retired_source_but_not_retained_store(self):
        (self.root/'file').write_text('source');task=self.create_task();started=self.start(task)
        self.mutate('task.block',task,reason='paused')
        candidate=self.base/'retired-source';shutil.copytree(started['path'],candidate)
        retired=self.call('project.add',root=str(candidate));self.call('project.unregister',project=retired['id'])
        denied=self.service.handle(self.request('workspace.relink',task=task,expected_revision=self.show(task)['revision'],
            path=retired['store'],confirmed_path=retired['store'],checkpoint=started['base_checkpoint'],writers_paused=True))
        self.assertFalse(denied['ok'],denied)
        accepted=self.mutate('workspace.relink',task,path=str(candidate),confirmed_path=str(candidate),
            checkpoint=started['base_checkpoint'],writers_paused=True)
        self.assertEqual(self.wait_operation(accepted['operation_id'])['state'],'succeeded')
        self.assertEqual(self.show(task)['workspace']['path'],str(candidate))

    def test_relink_rechecks_task_context_after_private_validation(self):
        from unittest.mock import patch
        from solace_core.workspace_trees import validate_private_files
        (self.root/'file').write_text('source');task=self.create_task();started=self.start(task)
        self.mutate('task.block',task,reason='paused')
        candidate=self.base/'candidate';shutil.copytree(started['path'],candidate)
        catalog=self.service.catalog(self.project)
        def change_context(path):
            validate_private_files(path)
            with catalog.connect(write=True) as db:
                db.execute('UPDATE tasks SET revision=revision+1 WHERE id=?',(task,))
        with patch('solace_core.workspace_trees.validate_private_files',side_effect=change_context):
            accepted=self.mutate('workspace.relink',task,path=str(candidate),confirmed_path=str(candidate),
                checkpoint=started['base_checkpoint'],writers_paused=True)
            outcome=self.wait_operation(accepted['operation_id'])
        self.assertEqual(outcome['state'],'failed',outcome)
        self.assertEqual(outcome['error']['code'],'TASK_REVISION_CONFLICT')
        self.assertEqual(self.show(task)['workspace']['path'],started['path'])
        self.assertEqual((candidate/'file').read_text(),'source')
