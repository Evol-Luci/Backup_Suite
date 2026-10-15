"""Candidate acceptance through a real same-user Unix socket and CLI."""
import json
import logging
import os
import threading
from pathlib import Path
from unittest.mock import patch
from task_fixture import TaskCase
import test_task_cli
from solace_core.server import SocketServer,bind_private_socket

class AcceptanceTests(TaskCase):
    run_cli=test_task_cli.TaskCLITests.run_cli
    result=test_task_cli.TaskCLITests.result
    stop_server=test_task_cli.TaskCLITests.stop_server

    def setUp(self):
        super().setUp();self.path=self.base/'core.sock'
        self.listener=bind_private_socket(self.path,self.base);self.server=SocketServer(self.listener,self.service)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop_server)
        self.env=dict(os.environ,XDG_RUNTIME_DIR=str(self.base),XDG_DATA_HOME=str(self.base/'xdg'),HOME=str(self.base/'home'))

    def completed(self,*args):
        result=self.result(*args,'--wait')
        self.assertEqual(result['state'],'succeeded',result)
        return result['result']

    def test_parent_and_two_children_preserve_source_and_independent_results(self):
        (self.root/'same.txt').write_text('source unchanged')
        (self.root/'.git').write_text('gitdir: /must/not/be/copied')
        (self.root/'.solace_vault').mkdir();(self.root/'.solace_vault/history').write_bytes(b'legacy state')
        (self.root/'.hidden').write_bytes(b'\0hidden')
        (self.root/'binary').write_bytes(bytes(range(256))*5000)
        (self.root/'run').write_bytes(b'#!/bin/sh\n');(self.root/'run').chmod(0o755)
        (self.root/'link').symlink_to('same.txt')
        before={str(p.relative_to(self.root)):(os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in self.root.rglob('*') if p.is_file() or p.is_symlink()}
        source=self.wait_operation(self.result('checkpoint','create',self.project,'source')['operation_id'])['result']['checkpoint_id']
        prompt=self.base/'request.txt';prompt.write_text('ACCEPTANCE PRIVATE CANARY\nsecond line')
        parent=self.result('task','create',self.project,'parent','--prompt-file',str(prompt))['task_id']
        p=self.completed('task','start',self.project,parent)
        self.assertFalse((Path(p['path'])/'.git').exists());self.assertFalse((Path(p['path'])/'.solace_vault').exists())
        a=self.result('task','create',self.project,'A','--parent',parent)['task_id']
        first=self.completed('task','start',self.project,a)
        b=self.result('task','create',self.project,'B','--parent',parent)['task_id']
        second=self.completed('task','start',self.project,b)
        left,right=Path(first['path']),Path(second['path'])
        self.assertNotEqual((left/'same.txt').stat().st_ino,(right/'same.txt').stat().st_ino)
        self.assertNotEqual((left/'same.txt').stat().st_ino,(self.root/'same.txt').stat().st_ino)
        (left/'same.txt').write_text('A only');(right/'same.txt').write_text('B only')
        a_result=self.completed('task','ready',self.project,a)['checkpoint_id']
        b_result=self.completed('task','ready',self.project,b)['checkpoint_id']
        self.completed('task','ready',self.project,parent)
        summary=self.result('task','show',self.project,parent)
        self.assertEqual(summary['aggregate'],'awaiting_integration');self.assertEqual(summary['children']['ready'],2)
        self.result('task','resume',self.project,a);(left/'same.txt').write_text('A again')
        again=self.completed('task','ready',self.project,a)['checkpoint_id']
        self.assertEqual(self.result('task','show',self.project,a)['results'],[a_result,again])
        self.assertEqual((right/'same.txt').read_text(),'B only')
        self.assertEqual(before,{str(p.relative_to(self.root)):(os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in self.root.rglob('*') if p.is_file() or p.is_symlink()})
        self.assertEqual([cp['id'] for cp in self.result('checkpoint','list',self.project)['checkpoints']],[source])
        allcp=self.result('checkpoint','list',self.project,'--all-workspaces')['checkpoints']
        by_id={cp['id']:cp for cp in allcp}
        self.assertEqual(by_id[again]['parent_id'],a_result)
        self.assertEqual(by_id[b_result]['parent_id'],second['base_checkpoint'])
        self.assertEqual(self.result('checkpoint','usage',self.project)['source_head'],source)
        catalog=self.service.catalog(self.project)
        with catalog.connect() as db:
            for table in ('operations','events','task_requests'):
                self.assertNotIn('ACCEPTANCE PRIVATE CANARY',str([tuple(row) for row in db.execute('SELECT * FROM '+table)]))
        # Restore saved results proves exact immutable file content, not only IDs.
        for checkpoint,expected in [(a_result,'A only'),(b_result,'B only'),(again,'A again')]:
            destination=self.base/('restore-'+checkpoint)
            catalog.open_existing().restore_checkpoint(checkpoint,destination)
            self.assertEqual((destination/'same.txt').read_text(),expected)

    def test_one_project_preparing_does_not_block_a_second_project(self):
        from solace_core.workspace_trees import WorkspaceBuilder
        task=self.create_task();other_root=self.base/'other';other_root.mkdir();(other_root/'file').write_text('other source')
        other=self.call('project.add',root=str(other_root))['id']
        entered,release=threading.Event(),threading.Event();original=WorkspaceBuilder.prepare
        def delayed(owner,*args):
            if owner.catalog.registration['id']==self.project:
                entered.set();release.wait(5)
            return original(owner,*args)
        try:
            with patch.object(WorkspaceBuilder,'prepare',delayed):
                first=self.mutate('task.start',task);self.assertTrue(entered.wait(3))
                second=self.call('task.create',project=other,title='independent')['task_id']
                accepted=self.call('task.start',project=other,task=second,expected_revision=1)
                self.assertEqual(self.wait_operation(accepted['operation_id'],other)['state'],'succeeded')
        finally:release.set()
        self.assertEqual(self.wait_operation(first['operation_id'])['state'],'succeeded')
