import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from task_fixture import TaskCase
from solace_core.service import CoreService

class RecoveryTests(TaskCase):
    def test_project_relink_crash_reconciles_durably_before_recovery_and_replay(self):
        (self.root/'file').write_text('baseline')
        task=self.create_task();start_request=self.crash(task,'after_stage')
        original=Path(self.registry.get(self.project)['store']);moved=original.with_name('moved-store');original.rename(moved)
        request_id=str(uuid.uuid4())
        worker=Path(__file__).with_name('relink_crash_worker.py')
        crashed=subprocess.run([sys.executable,str(worker),str(self.registry.path),self.project,
            str(self.root),str(moved),str(self.config.workspace_root),request_id],capture_output=True,timeout=18)
        self.assertEqual(crashed.returncode,88,crashed.stderr.decode())
        self.reopen();operation=self.operation(start_request)
        resumed=self.recover(operation,task)
        outcome=self.wait_operation(resumed['operation_id']);self.assertEqual(outcome['state'],'succeeded',outcome)
        args=dict(project=self.project,root=str(self.root),store=str(moved),confirmed_root=str(self.root))
        first=self.call('project.relink',request_id,**args);self.assertEqual(self.call('project.relink',request_id,**args),first)
        catalog=self.service.catalog(self.project)
        with catalog.connect() as db:
            caches=[r[0] for r in db.execute('SELECT path FROM workspace_caches')]
        self.assertTrue(caches);self.assertTrue(all(Path(p).is_relative_to(moved) and Path(p).exists() for p in caches))
        self.assertEqual((Path(self.show(task)['workspace']['path'])/'file').read_text(),'baseline')

    def crash(self,task,phase):
        request_id=str(uuid.uuid4());self.service.close()
        process=subprocess.run([sys.executable,str(Path(__file__).with_name('task_crash_worker.py')),str(self.registry.path),self.project,task,request_id,phase,str(self.config.workspace_root)],capture_output=True,timeout=18)
        expected={'before_stage':71,'partial_stage':76,'after_stage':72,'after_rename':73,'before_commit':74,'after_commit':75,'ready_before_commit':74,'ready_after_commit':75}[phase]
        self.assertEqual(process.returncode,expected,process.stderr.decode())
        return request_id

    def reopen(self):
        self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)

    def operation(self,request):
        with self.service.catalog(self.project).connect() as db:
            return db.execute('SELECT id FROM operations WHERE request_id=?',(request,)).fetchone()[0]

    def recover(self,operation,task,action='resume',request_id=None):
        return self.call('workspace.recovery.'+action,request_id,project=self.project,operation=operation,task=task,expected_revision=self.show(task)['revision'])

    def test_real_crashes_on_both_sides_of_filesystem_and_database_publication(self):
        (self.root/'file').write_text('baseline')
        for phase in ['before_stage','after_stage','after_rename','before_commit','after_commit']:
            with self.subTest(phase=phase):
                task=self.create_task();request=self.crash(task,phase);self.reopen();operation=self.operation(request)
                outcome=self.wait_operation(operation)
                self.assertEqual(outcome['state'],'succeeded' if phase=='after_commit' else 'interrupted')
                replay=self.service.handle(dict(version=1,id=request,method='task.start',params=dict(project=self.project,task=task,expected_revision=1),actor='test'))
                self.assertEqual(replay['result'],{'operation_id':operation})
                if phase=='after_commit':
                    self.assertEqual(self.show(task)['state'],'active');continue
                self.assertEqual(self.show(task)['state'],'created');self.assertIsNone(self.show(task)['workspace'])
                inspection=self.call('workspace.recovery.show',project=self.project,operation=operation)
                self.assertFalse(inspection['workspace_advertised'])
                if phase=='before_stage':
                    self.recover(operation,task,'abandon');self.start(task)
                else:
                    accepted=self.recover(operation,task)
                    resumed=self.wait_operation(accepted['operation_id']);self.assertEqual(resumed['state'],'succeeded',resumed)
                    self.assertEqual(self.wait_operation(operation)['state'],'interrupted')
                    self.assertEqual((Path(self.show(task)['workspace']['path'])/'file').read_text(),'baseline')

    def test_unavailable_store_defers_recovery_and_dirty_final_is_not_adopted(self):
        (self.root/'file').write_text('baseline');task=self.create_task();request=self.crash(task,'after_rename')
        record=self.registry.get(self.project);store=Path(record['store']);offline=store.with_name('offline')
        store.rename(offline)
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.reopen()
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        self.assertTrue(any(r.get('error') for r in self.service.recovery))
        offline.rename(store);operation=self.operation(request)
        inspection=self.call('workspace.recovery.show',project=self.project,operation=operation)
        final=Path(inspection['final_path']);(final/'file').write_text('external edits')
        resumed=self.wait_operation(self.recover(operation,task)['operation_id'])
        self.assertEqual(resumed['state'],'failed');self.assertIsNone(self.show(task)['workspace'])
        self.recover(operation,task,'abandon')
        self.assertEqual((final/'file').read_text(),'external edits')

    def test_partial_staging_preview_is_invalidated_by_replacement_then_cleanup_replays(self):
        task=self.create_task();request=self.crash(task,'partial_stage');self.reopen();operation=self.operation(request)
        self.recover(operation,task,'abandon')
        preview=self.call('workspace.staging.cleanup',project=self.project,operation=operation,preview=True)
        staging=Path(preview['staging_path']);(staging/'extra').symlink_to('/outside')
        changed=self.service.handle(self.request('workspace.staging.cleanup',operation=operation,preview=False,plan_token=preview['plan_token']))
        self.assertEqual(changed['error']['code'],'RECOVERY_PREVIEW_STALE')
        self.assertTrue((staging/'extra').is_symlink())
        fresh=self.call('workspace.staging.cleanup',project=self.project,operation=operation,preview=True)
        req=self.request('workspace.staging.cleanup',operation=operation,preview=False,plan_token=fresh['plan_token'])
        cleaned=self.service.handle(req);self.assertTrue(cleaned['ok'],cleaned)
        self.assertFalse(staging.exists());self.assertEqual(self.service.handle(req),cleaned)

    def test_ready_crash_preserves_external_state_and_releases_capture_claim(self):
        (self.root/'file').write_text('baseline')
        for phase in ['ready_before_commit','ready_after_commit']:
            with self.subTest(phase=phase):
                task=self.create_task();self.start(task);old=self.show(task)['workspace']['head_checkpoint']
                request=self.crash(task,phase);self.reopen();operation=self.operation(request)
                outcome=self.wait_operation(operation);view=self.show(task)
                self.assertEqual(outcome['state'],'interrupted' if phase=='ready_before_commit' else 'succeeded')
                self.assertEqual(view['state'],'active' if phase=='ready_before_commit' else 'ready')
                self.assertIsNone(view['pending_operation'])
                if phase=='ready_before_commit': self.assertEqual(view['workspace']['head_checkpoint'],old)
                else: self.assertEqual(view['results'],[outcome['result']['checkpoint_id']])

    def test_recovery_rejects_stale_revision_missing_source_and_replaced_final(self):
        (self.root/'file').write_text('baseline');task=self.create_task();request=self.crash(task,'after_rename');self.reopen()
        operation=self.operation(request)
        stale=self.service.handle(self.request('workspace.recovery.resume',operation=operation,task=task,expected_revision=2))
        self.assertEqual(stale['error']['code'],'TASK_REVISION_CONFLICT')
        moved=self.root.with_name('source-away');self.root.rename(moved)
        missing=self.service.handle(self.request('workspace.recovery.resume',operation=operation,task=task,expected_revision=1))
        self.assertEqual(missing['error']['code'],'WORKSPACE_UNAVAILABLE');moved.rename(self.root)
        inspection=self.call('workspace.recovery.show',project=self.project,operation=operation)
        final=Path(inspection['final_path']);original=final.with_name('original-final');final.rename(original)
        final.mkdir();(final/'file').write_text('baseline')
        failed=self.wait_operation(self.recover(operation,task)['operation_id'])
        self.assertEqual(failed['error']['code'],'WORKSPACE_IDENTITY_MISMATCH')
        (final/'file').unlink();final.rmdir();original.rename(final)
        # Complete recorded trees are verified from immutable objects even when
        # an optional base cache is offline; recovery never rebuilds that cache.
        with self.service.catalog(self.project).connect() as db:
            cache=Path(db.execute('SELECT path FROM workspace_caches LIMIT 1').fetchone()[0])
        cache.parent.rename(cache.parent.with_name('offline-cache'))
        req=self.request('workspace.recovery.resume',operation=operation,task=task,expected_revision=1)
        accepted=self.service.handle(req);self.assertTrue(accepted['ok'],accepted)
        self.assertEqual(self.wait_operation(accepted['result']['operation_id'])['state'],'succeeded')
        self.assertEqual(self.service.handle(req),accepted)

    def test_changed_mount_defers_recovery_without_creating_directories(self):
        task=self.create_task();request=self.crash(task,'after_stage');self.reopen();operation=self.operation(request)
        inspection=self.call('workspace.recovery.show',project=self.project,operation=operation)
        staging=Path(inspection['staging_path']);identity=staging.stat()
        old=self.policy.mountinfo;self.policy.mountinfo='2 0 0:1 / / rw - ext4 x rw\n'
        result=self.service.handle(self.request('workspace.recovery.resume',operation=operation,task=task,expected_revision=1))
        self.assertEqual(result['error']['code'],'STORE_UNAVAILABLE')
        self.policy.mountinfo=old
        self.assertEqual(staging.stat().st_ino,identity.st_ino)
        req=self.request('workspace.recovery.abandon',operation=operation,task=task,expected_revision=1)
        abandoned=self.service.handle(req);self.assertTrue(abandoned['ok'],abandoned)
        self.assertEqual(self.service.handle(req),abandoned);self.assertTrue(staging.exists())

    def test_readonly_catalog_tolerates_a_wal_disappearing_between_sidecar_checks(self):
        from unittest.mock import patch
        from solace_core.catalog import readonly_database
        catalog=self.service.catalog(self.project)
        before={p.name:p.read_bytes() for p in catalog.store.iterdir() if p.is_file()}
        wal=Path(str(catalog.database)+'-wal');original_exists=Path.exists;observed=[]
        def exists(path):
            if path==wal and not observed:
                observed.append(True);return True
            return original_exists(path)
        with patch.object(Path,'exists',exists),readonly_database(catalog.database) as db:
            self.assertEqual(db.execute('SELECT schema_version FROM core_metadata').fetchone()[0],5)
        self.assertEqual(before,{p.name:p.read_bytes() for p in catalog.store.iterdir() if p.is_file()})

    def test_explicit_store_relink_preserves_recovery_and_relocates_verified_base_cache(self):
        (self.root/'file').write_text('baseline');parent=self.create_task();request=self.crash(parent,'after_stage')
        original=Path(self.registry.get(self.project)['store']);moved=original.with_name('relocated-store');original.rename(moved)
        with self.assertLogs('solace_core.maintenance',level='WARNING') as diagnostics:
            self.reopen()
        self.assertEqual([record.getMessage() for record in diagnostics.records],
                         ['prompt maintenance project='+self.project+' exception=CoreError'])
        self.call('project.relink',project=self.project,root=str(self.root),store=str(moved),confirmed_root=str(self.root))
        operation=self.operation(request)
        result=self.wait_operation(self.recover(operation,parent)['operation_id']);self.assertEqual(result['state'],'succeeded',result)
        baseline=self.show(parent)['workspace']['base_checkpoint'];child=self.create_task(parent=parent)
        started=self.start(child,from_checkpoint=baseline)
        self.assertEqual((Path(started['path'])/'file').read_text(),'baseline')
