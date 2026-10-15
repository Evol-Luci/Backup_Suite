from pathlib import Path
from task_fixture import TaskCase

class TaskLineageTests(TaskCase):
    def test_source_parent_and_sibling_heads_never_follow_global_latest(self):
        (self.root/'shared').write_text('original')
        (self.root/'.git').mkdir();(self.root/'.git/config').write_text('original Git')
        source=self.wait_operation(self.call('checkpoint.create',project=self.project,message='manual')['operation_id'])['result']['checkpoint_id']
        parent=self.create_task();p=self.start(parent);parent_path=Path(p['path'])
        self.assertFalse((parent_path/'.git').exists())
        a=self.create_task(parent=parent);first=self.start(a)
        (Path(first['path'])/'shared').write_text('sibling A result')
        saved=self.wait_operation(self.mutate('task.ready',a)['operation_id'])['result']['checkpoint_id']
        (parent_path/'shared').write_text('parent later')
        b=self.create_task(parent=parent);second=self.start(b)
        self.assertEqual((Path(second['path'])/'shared').read_text(),'parent later')
        self.assertNotEqual(second['base_checkpoint'],saved)
        own=self.wait_operation(self.mutate('workspace.checkpoint',b,message='B capture')['operation_id'])
        self.assertEqual(own['state'],'succeeded',own)
        allcp=self.call('checkpoint.list',project=self.project,all_workspaces=True)['checkpoints']
        by_id={cp['id']:cp for cp in allcp}
        self.assertEqual(by_id[own['result']['checkpoint_id']]['parent_id'],second['head_checkpoint'])
        self.assertEqual(by_id[p['base_checkpoint']]['parent_id'],source)
        self.assertEqual(self.call('checkpoint.usage',project=self.project)['source_head'],source)
        self.assertEqual([cp['id'] for cp in self.call('checkpoint.list',project=self.project)['checkpoints']],[source])
        self.assertEqual((self.root/'.git/config').read_text(),'original Git')
        c=self.create_task(parent=parent)
        invalid=self.service.handle(self.request('task.start',task=c,expected_revision=self.show(c)['revision'],expected_parent_revision=self.show(parent)['revision'],from_checkpoint=saved))
        self.assertEqual(invalid['error']['code'],'BASELINE_INELIGIBLE')
        third=self.start(c,from_checkpoint=second['base_checkpoint'])
        self.assertEqual(third['base_checkpoint'],second['base_checkpoint'])
