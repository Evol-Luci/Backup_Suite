"""Adversarial regressions from the independent task/workspace review."""
import json
import os
from pathlib import Path
from unittest.mock import patch
from task_fixture import TaskCase


class IsolationReviewTests(TaskCase):
    def test_upgrade_replays_after_unregister_without_another_backup(self):
        import uuid
        from test_task_schema import create_v1
        root=self.base/'legacy';root.mkdir();store=self.base/'legacy-store';project=uuid.uuid4().hex
        record=dict(id=project,root=str(root),store=str(store),store_uuid=uuid.uuid4().hex,mount_id=1,created_at='2026-01-01T00:00:00+00:00')
        create_v1(record,self.policy)
        with self.registry.connect(write=True) as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?)',tuple(record[k] for k in ('id','root','store','store_uuid','mount_id','created_at')))
            self.registry._save_receipt(db,str(uuid.uuid4()),'fixture',record)
        preview=self.call('project.upgrade',project=project,preview=True)
        request=self.request('project.upgrade',project=project,preview=False,plan_token=preview['plan_token'])
        first=self.service.handle(request);self.assertTrue(first['ok'],first)
        self.call('project.unregister',project=project)
        self.assertEqual(self.service.handle(request),first)
        self.assertEqual(len(list((store/'schema-backups').glob('*/checkpoints.sqlite3'))),1)
        fresh=self.service.handle(self.request('project.upgrade',project=project,preview=False,plan_token=preview['plan_token']))
        self.assertEqual(fresh['error']['code'],'PROJECT_NOT_FOUND')

    def test_substituted_intermediate_directory_never_receives_source_bytes(self):
        import solace_core.workspace_trees as trees
        (self.root/'sub').mkdir();(self.root/'sub/file').write_text('private source')
        outside=self.base/'outside';outside.mkdir()
        original=os.open;swapped=[]
        def open_with_swap(path,flags,*args,**kwargs):
            # Substitute immediately before creation, after all parent checks.
            if flags & os.O_CREAT and Path(path).name=='file' and not swapped:
                parent=(Path(os.readlink('/proc/self/fd/'+str(kwargs['dir_fd'])))
                        if kwargs.get('dir_fd') is not None else Path(path).parent)
                if '.staging-' in str(parent):
                    parent.rename(parent.with_name('original-sub'))
                    parent.symlink_to(outside,target_is_directory=True);swapped.append(True)
            return original(path,flags,*args,**kwargs)
        task=self.create_task()
        with patch.object(trees.os,'open',side_effect=open_with_swap):
            result=self.wait_operation(self.mutate('task.start',task)['operation_id'])
        self.assertTrue(swapped)
        self.assertFalse((outside/'file').exists(),'source bytes escaped into an unrelated directory')
        self.assertEqual(result['state'],'failed');self.assertIsNone(self.show(task)['workspace'])

    def test_workspace_relink_refuses_other_workspace_and_hardlinks(self):
        (self.root/'sub').mkdir();(self.root/'sub/file').write_text('baseline')
        right=self.create_task();sibling=Path(self.start(right)['path'])
        hardlinked=self.base/'hardlinked';hardlinked.mkdir()
        os.link(sibling/'sub/file',hardlinked/'file')
        for candidate in [sibling,sibling/'sub',hardlinked]:
            with self.subTest(candidate=candidate):
                left=self.create_task();baseline=self.start(left)
                self.mutate('task.block',left,reason='paused')
                response=self.service.handle(self.request('workspace.relink',task=left,
                    expected_revision=self.show(left)['revision'],path=str(candidate),confirmed_path=str(candidate),
                    checkpoint=baseline['base_checkpoint'],writers_paused=True))
                if response['ok']:
                    result=self.wait_operation(response['result']['operation_id'])
                    self.assertEqual(result['state'],'failed',result)
                    self.assertEqual(result['error']['code'],'UNSUPPORTED_STORAGE',result)
                else:self.assertEqual(response['error']['code'],'UNSUPPORTED_STORAGE',response)
                self.assertEqual(self.show(left)['workspace']['path'],baseline['path'])
        self.assertEqual((sibling/'sub/file').read_text(),'baseline')

    def test_store_placement_protects_retained_workspaces_after_config_change(self):
        from solace_core.service import CoreService
        from solace_core.workspace_paths import CoreConfig
        (self.root/'file').write_text('baseline')
        task=self.create_task();workspace=Path(self.start(task)['path'])
        other=self.base/'other-source';other.mkdir()
        response=self.service.handle(self.request('project.add',root=str(other),store=str(workspace/'store')))
        self.assertFalse(response['ok'],response)
        self.mutate('task.cancel',task);self.call('project.unregister',project=self.project)
        self.service.close()
        self.service=CoreService(self.registry,config=CoreConfig((),self.base/'new-workspaces'))
        self.addCleanup(self.service.close)
        for root,store in [(other,workspace/'store'),(workspace/'new-source',self.base/'separate-store')]:
            root.mkdir(exist_ok=True)
            response=self.service.handle(self.request('project.add',root=str(root),store=str(store)))
            self.assertFalse(response['ok'],response)
            self.assertEqual(response['error']['code'],'UNSUPPORTED_STORAGE')

    def test_empty_start_syncs_every_new_workspace_ancestor(self):
        from solace_core.service import CoreService
        from solace_core.workspace_paths import CoreConfig
        self.service.close()
        root=self.base/'new'/'nested'/'workspaces'
        self.service=CoreService(self.registry,config=CoreConfig((),root));self.addCleanup(self.service.close)
        original=os.fsync;synced=set()
        def fsync(fd):
            synced.add(os.readlink('/proc/self/fd/'+str(fd)));return original(fd)
        task=self.create_task()
        with patch('os.fsync',side_effect=fsync):self.start(task)
        for path in [self.base,self.base/'new',self.base/'new/nested',root,root/self.project]:
            self.assertIn(str(path),synced)
