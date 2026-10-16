import errno
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch
from task_fixture import TaskCase
from solace_core.errors import CoreError
from solace_core.tasks import TaskCatalog
from solace_engine.repository import ProjectRepository

class MaterializationTests(TaskCase):
    def test_cache_creation_and_reads_never_follow_substituted_parents(self):
        import solace_core.workspace_trees as trees
        (self.root/'sub').mkdir();(self.root/'sub/file').write_text('private source')
        cp=self.fixture();builder=trees.WorkspaceBuilder(self.catalog,self.service.workspace_paths)
        outside=self.base/'outside';outside.mkdir();(outside/'sentinel').write_text('unrelated bytes')
        outside_mode=(outside/'sentinel').stat().st_mode
        original=os.open;swapped=[]
        def replace_cache_parent(path,flags,*args,**kwargs):
            if flags & os.O_CREAT and Path(path).name=='file' and not swapped:
                parent=(Path(os.readlink('/proc/self/fd/'+str(kwargs['dir_fd'])))
                        if kwargs.get('dir_fd') is not None else Path(path).parent)
                if '.stage-' in str(parent):
                    parent.rename(parent.with_name('original-sub'));parent.symlink_to(outside,target_is_directory=True);swapped.append(True)
            return original(path,flags,*args,**kwargs)
        from solace_engine.errors import CorruptObject
        with patch.object(trees.os,'open',side_effect=replace_cache_parent),self.assertRaises((CorruptObject,CoreError,OSError)):
            trees.BaseCache(self.catalog,self.service.workspace_paths).ensure(self.repo,cp.id)
        self.assertTrue(swapped);self.assertFalse((outside/'file').exists())
        self.assertEqual((outside/'sentinel').stat().st_mode,outside_mode)
        (outside/'file').write_text('unrelated bytes')
        cache=trees.BaseCache(self.catalog,self.service.workspace_paths).ensure(self.repo,cp.id)
        intent=self.intent(cp);swapped.clear();cloned=[]
        original_clone=trees.clone_regular
        def read_with_swap(path,flags,*args,**kwargs):
            if Path(path).name=='file' and not flags & os.O_CREAT and not swapped:
                parent=(Path(os.readlink('/proc/self/fd/'+str(kwargs['dir_fd'])))
                        if kwargs.get('dir_fd') is not None else Path(path).parent)
                if str(parent)==str(Path(cache['path'])/'sub'):
                    Path(cache['path']).chmod(0o700)
                    parent.rename(parent.with_name('original-sub'));parent.symlink_to(outside,target_is_directory=True);swapped.append(True)
            return original(path,flags,*args,**kwargs)
        def clone(source,destination):
            cloned.append(os.fstat(source).st_ino)
            self.assertNotEqual(os.fstat(source).st_ino,(outside/'file').stat().st_ino)
            return original_clone(source,destination)
        # Arm only after cache verification, immediately before reading its files.
        original_ensure=trees.BaseCache.ensure
        def ensure(*args):
            result=original_ensure(*args)
            patcher=patch.object(trees.os,'open',side_effect=read_with_swap);patcher.start();self.addCleanup(patcher.stop)
            return result
        with patch.object(trees.BaseCache,'ensure',side_effect=lambda *args:ensure(trees.BaseCache(self.catalog,self.service.workspace_paths),*args)),patch.object(trees,'clone_regular',side_effect=clone):
            builder.prepare(self.repo,cp.id,intent)
        self.assertTrue(swapped);self.assertTrue(cloned)
        self.assertEqual((Path(intent['staging_path'])/'sub/file').read_text(),'private source')

    def fixture(self):
        (self.root/'big').write_bytes(b'chunked\0'*160000)
        (self.root/'run').write_bytes(b'#!/bin/sh\n');(self.root/'run').chmod(0o755)
        (self.root/'.hidden').write_bytes(b'keep');(self.root/'link').symlink_to('run')
        self.catalog=self.service.catalog(self.project);self.repo=self.catalog.open_existing()
        return self.repo.create_checkpoint('base',parent_id=None)

    def intent(self,checkpoint):
        tasks=TaskCatalog(self.catalog)
        task=tasks.create(self.request('task.create',title='copy'))['task_id']
        request=self.request('task.start',task=task,expected_revision=1)
        operation=self.catalog.accept_operation(request)['operation_id']
        paths=self.service.workspace_paths
        selected=paths.reserve(self.project,task,operation)
        parent=paths.ensure_parent(Path(selected['final_path']))
        intent=dict(operation_id=operation,task_id=task,workspace_id=uuid.uuid4().hex,kind='start',phase='reserved',
                    base_checkpoint=checkpoint.id,expected_json=json.dumps({'destination_parent':parent}),**selected)
        with self.catalog.connect(write=True) as db:
            db.execute('INSERT INTO workspace_intents(operation_id,task_id,workspace_id,kind,phase,staging_path,final_path,base_checkpoint,expected_json) VALUES (?,?,?,?,?,?,?,?,?)',
                tuple(intent[k] for k in ['operation_id','task_id','workspace_id','kind','phase','staging_path','final_path','base_checkpoint','expected_json']))
        return intent

    def test_clones_are_verified_and_mutable_inodes_are_separate(self):
        from solace_core.workspace_trees import WorkspaceBuilder
        cp=self.fixture();builder=WorkspaceBuilder(self.catalog,self.service.workspace_paths)
        results=[]
        for unused in range(2):
            intent=self.intent(cp);staged=builder.prepare(self.repo,cp.id,intent)
            results.append(builder.publish(intent,staged))
        left,right=[Path(r['path']) for r in results]
        self.assertEqual((left/'big').read_bytes(),(self.root/'big').read_bytes())
        self.assertEqual((left/'run').stat().st_mode & 0o777,0o755)
        self.assertEqual(os.readlink(left/'link'),'run')
        self.assertNotEqual((left/'big').stat().st_ino,(right/'big').stat().st_ino)
        (left/'big').write_bytes(b'changed')
        self.assertEqual((right/'big').read_bytes(),(self.root/'big').read_bytes())
        with self.catalog.connect() as db: cache=Path(db.execute('SELECT path FROM workspace_caches').fetchone()[0])
        self.assertEqual((cache/'big').read_bytes(),(self.root/'big').read_bytes())
        self.assertNotEqual((cache/'big').stat().st_ino,(right/'big').stat().st_ino)

    def test_unsupported_clone_fallback_and_io_errors(self):
        from solace_core.workspace_trees import clone_regular
        for error in [errno.EOPNOTSUPP,errno.ENOTTY,errno.EXDEV,errno.EINVAL,errno.ENOSYS,errno.EIO,errno.ENOSPC,errno.EDQUOT,errno.EACCES]:
            with self.subTest(error=error):
                source=self.base/'copy-source';source.write_bytes(b'copy exact')
                target=self.base/'copy-target'
                with source.open('rb') as src,target.open('w+b') as dst,patch('fcntl.ioctl',side_effect=OSError(error,'injected')):
                    if error in [errno.EIO,errno.ENOSPC,errno.EDQUOT,errno.EACCES]:
                        with self.assertRaises(OSError): clone_regular(src.fileno(),dst.fileno())
                    else:
                        self.assertEqual(clone_regular(src.fileno(),dst.fileno()),'copy')
                        dst.seek(0);self.assertEqual(dst.read(),b'copy exact')

    def test_cache_corruption_fails_without_publishing(self):
        from solace_core.workspace_trees import BaseCache,WorkspaceBuilder
        cp=self.fixture();cache=BaseCache(self.catalog,self.service.workspace_paths).ensure(self.repo,cp.id)
        file=Path(cache['path'])/'run';file.chmod(0o600);file.write_bytes(b'corrupt')
        intent=self.intent(cp)
        with self.assertRaises(CoreError) as caught: WorkspaceBuilder(self.catalog,self.service.workspace_paths).prepare(self.repo,cp.id,intent)
        self.assertEqual(caught.exception.code,'CACHE_CORRUPT')
        self.assertFalse(Path(intent['final_path']).exists())

    def test_existing_destination_and_parent_replacement_are_preserved(self):
        from solace_core.workspace_trees import WorkspaceBuilder
        cp=self.fixture();builder=WorkspaceBuilder(self.catalog,self.service.workspace_paths)
        intent=self.intent(cp);staged=builder.prepare(self.repo,cp.id,intent)
        final=Path(intent['final_path']);final.mkdir();(final/'sentinel').write_text('keep')
        with self.assertRaises(FileExistsError): builder.publish(intent,staged)
        self.assertEqual((final/'sentinel').read_text(),'keep')
        other=self.intent(cp);staged=builder.prepare(self.repo,cp.id,other)
        parent=Path(other['final_path']).parent;parent.rename(parent.with_name('moved-parent'));parent.mkdir()
        (parent/'sentinel').write_text('other')
        with self.assertRaises(CoreError): builder.publish(other,staged)
        self.assertEqual((parent/'sentinel').read_text(),'other')

    def test_unsafe_link_graph_refused_before_materialization(self):
        from solace_core.workspace_trees import validate_workspace_links
        from solace_engine.models import FileEntry
        def entries(targets):
            return [FileEntry(p,'symlink',0o777,len(t),'unused',()) for p,t in targets.items()]
        for targets in [{'a':b'/outside'},{'a':b'../outside'},{'a':b'bridge/file','bridge':b'../outside'},{'a':b'b','b':b'a'},{'a':b'../inside/a'}]:
            with self.subTest(targets=targets),self.assertRaises(CoreError) as caught:
                validate_workspace_links(entries(targets),targets)
            self.assertEqual(caught.exception.code,'WORKSPACE_SYMLINK_UNSAFE')
        validate_workspace_links(entries({'a':b'inside/missing','inside/b':b'../safe'}),{'a':b'inside/missing','inside/b':b'../safe'})
