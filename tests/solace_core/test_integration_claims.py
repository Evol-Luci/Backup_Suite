"""Approval authentication and persisted cross-project live-tree ownership."""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from unittest.mock import patch

from integration_fixture import IntegrationCase
from solace_core.errors import CoreError
from solace_core.integration_review import IntegrationReview
from solace_core.integration_snapshots import canonical
from solace_core.workspace_paths import directory_identity


class ClaimsTests(IntegrationCase):
    def test_export_and_unrelated_workspace_preserve_exact_review_token_and_revision(self):
        plan=self.simple_plan()
        request=self.approval_request(plan,accept_unchecked=True)
        with self.catalog.connect() as db:
            original=dict(db.execute('SELECT * FROM integration_revisions WHERE plan_id=?',(plan.id,)).fetchone())
            original.pop('validated_at')
        self.export_direct(plan,self.base/'review-copy')
        self.start(self.create_task('independent workspace'))
        other=self.base/'unrelated';other.mkdir();self.call('project.add',root=str(other))
        approved=self.claims().approve(request)
        self.assertEqual(approved['revision'],plan.revision)
        with self.catalog.connect() as db:
            revised=dict(db.execute('SELECT * FROM integration_revisions WHERE plan_id=?',(plan.id,)).fetchone())
            revised.pop('validated_at')  # Approval refreshes this documented observation time.
            self.assertEqual(revised,original)
            self.assertEqual(revised['review_token'],request['params']['token'])
            binding=json.loads(db.execute('SELECT binding_json FROM integration_approvals').fetchone()[0])
        self.assertEqual(binding['review_token'],request['params']['token'])
        self.assertIn(str(self.base/'review-copy'),self.plans._boundaries())
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_unowned(self.base/'review-copy')

    def claims(self):
        from solace_core.integration_claims import IntegrationClaims
        return IntegrationClaims(self.service, self.catalog)

    def simple_plan(self):
        (self.root/'a').write_bytes(b'base')
        task, result, _ = self.ready_task({'a': b'result'})
        return self.prepare_direct(task, result)

    def approval_request(self, plan, **extra):
        shown = IntegrationReview(self.service, self.catalog).show(plan.id)
        return self.request('integration.approve', plan=plan.id, revision=plan.revision,
                            token=shown['review_token'], **extra)

    def reserve(self, plan):
        request = self.request('integration.apply', plan=plan.id, revision=plan.revision)
        operation = self.catalog.accept_operation(request)['operation_id']
        with self.registry.lock, self.service.coordinator(self.project).lock, self.catalog.connect(write=True) as db:
            journal = self.claims().reserve(db, operation, plan)
        return journal

    def test_exact_approval_replay_and_unchecked(self):
        plan = self.simple_plan()
        claims = self.claims()
        for value in (None, False, 1):
            request = self.approval_request(plan, **({} if value is None else {'accept_unchecked': value}))
            with self.assertRaises(CoreError) as raised: claims.approve(request)
            self.assertEqual(raised.exception.code, 'INTEGRATION_UNCHECKED')
        request = self.approval_request(plan, accept_unchecked=True)
        response = claims.approve(request)
        self.assertRegex(response['approval'], r'^[0-9a-f]{64}$')
        with self.catalog.connect() as db:
            row = db.execute('SELECT * FROM integration_approvals').fetchone()
            self.assertEqual(row['token_sha256'], hashlib.sha256(response['approval'].encode()).hexdigest())
            binding = json.loads(row['binding_json'])
            self.assertEqual(binding['candidate']['manifest_sha256'], plan.candidate.manifest_sha256)
            self.assertEqual(binding['checks'], 'not_run_by_solace')
        self.root.rename(self.base/'moved-source')
        self.assertEqual(claims.approve(request), response)

    def test_resolution_invalidates_old_approval(self):
        plan = self.make_binary_conflict()
        resolved = self.review.resolve(self.request('integration.resolve', plan=plan.id, revision=1, path='data.bin', take='incoming'))
        plan = self.plans.load(plan.id)
        request = self.approval_request(plan, accept_unchecked=True)
        approved = self.claims().approve(request)
        self.review.resolve(self.request('integration.resolve', plan=plan.id, revision=2, path='data.bin', take='current'))
        with self.assertRaises(CoreError) as raised: self.claims().validate(plan, scan=True)
        self.assertEqual(raised.exception.code, 'INTEGRATION_PLAN_STALE')
        self.assertEqual(self.claims().approve(request), approved)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM integration_plans').fetchone()[0], 'prepared')

    def test_changed_bytes_mode_head_revision_profile_or_latest_result_are_stale(self):
        plan = self.simple_plan()
        claims = self.claims()
        self.assertEqual(claims.validate(plan, scan=True)['revision'], 1)
        original = (self.root/'a').read_bytes(); original_mode = (self.root/'a').stat().st_mode & 0o777
        for mutate, restore in (
            (lambda: (self.root/'a').write_bytes(b'edit'), lambda: (self.root/'a').write_bytes(original)),
            (lambda: (self.root/'a').chmod(0o700), lambda: (self.root/'a').chmod(original_mode)),
        ):
            mutate()
            with self.assertRaises(CoreError) as raised: claims.validate(plan, scan=True)
            self.assertEqual(raised.exception.code, 'INTEGRATION_PLAN_STALE'); restore()
        for statement, args, undo, undo_args in (
            ('UPDATE tasks SET revision=revision+1 WHERE id=?', (plan.task_id,), 'UPDATE tasks SET revision=revision-1 WHERE id=?', (plan.task_id,)),
            ('UPDATE tasks SET profile_json=? WHERE id=?', ('{}', plan.task_id), 'UPDATE tasks SET profile_json=? WHERE id=?', (plan.incoming.profile_json, plan.task_id)),
            ("UPDATE workspaces SET head_checkpoint=? WHERE task_id=?", (plan.base.checkpoint_id,plan.task_id), "UPDATE workspaces SET head_checkpoint=? WHERE task_id=?", (plan.result_id,plan.task_id)),
        ):
            with self.catalog.connect(write=True) as db: db.execute(statement,args)
            with self.assertRaises(CoreError) as raised: claims.validate(plan, scan=False)
            self.assertEqual(raised.exception.code, 'INTEGRATION_PLAN_STALE')
            with self.catalog.connect(write=True) as db: db.execute(undo,undo_args)
        accepted=self.call('checkpoint.create',project=self.project,message='changed head')
        self.assertEqual(self.wait_operation(accepted['operation_id'])['state'],'succeeded')
        with self.assertRaises(CoreError) as raised: claims.validate(plan, scan=False)
        self.assertEqual(raised.exception.code, 'INTEGRATION_PLAN_STALE')

    def test_reservation_is_durable_before_creation_and_blocks_source_access(self):
        pending = self.create_task()
        plan = self.simple_plan(); journal = self.reserve(plan)
        self.assertIsNone(journal.identity); self.assertFalse(journal.path.exists())
        self.assertEqual(journal.path, self.root.parent/('.solace-integration-'+journal.operation_id))
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM integration_journals').fetchone()[0], 'reserved')
        for path in (self.root,self.root/'a',self.root.parent,journal.path):
            with self.assertRaises(CoreError) as raised: self.claims().assert_access(path)
            self.assertEqual(raised.exception.code, 'INTEGRATION_TARGET_BUSY')
        self.claims().assert_access(self.root,operation_id=journal.operation_id)
        for request in (self.request('checkpoint.create',message='blocked'),
                        self.request('task.start',task=pending,expected_revision=1),
                        self.request('project.unregister')):
            response=self.service.handle(request)
            self.assertFalse(response['ok'], response)
            self.assertIn(response['error']['code'], ('INTEGRATION_TARGET_BUSY','TASK_BUSY','PROJECT_BUSY'))
        response=self.service.handle(self.request('project.add',root=str(journal.path.parent)))
        self.assertFalse(response['ok'],response)
        with self.catalog.connect(write=True) as db: self.claims().release(db,journal.operation_id)
        # Removing physical rows cannot release an unresolved logical owner.
        with self.assertRaises(CoreError) as raised: self.claims().assert_access(self.root)
        self.assertEqual(raised.exception.code, 'INTEGRATION_TARGET_BUSY')
        self.plans.abandon(self.request('integration.abandon',plan=plan.id,revision=plan.revision))
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM integration_journals WHERE operation_id=?',(journal.operation_id,)).fetchone()[0], 'abandoned')
        self.claims().assert_access(self.root)
        # Historical journal ownership survives verified abandonment.
        with self.assertRaises(CoreError): self.service.workspace_paths.assert_unowned(journal.path)

    def test_overlapping_sources_and_journal_inside_other_source_are_refused(self):
        plan = self.simple_plan()
        nested=self.root/'nested'; nested.mkdir()
        self.call('project.add',root=str(nested))
        with self.assertRaises(CoreError): self.reserve(plan)
        with self.catalog.connect() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)

    def test_unavailable_retained_catalog_fails_closed(self):
        plan=self.simple_plan()
        other=self.base/'other';other.mkdir()
        record=self.call('project.add',root=str(other))
        self.call('project.unregister',project=record['id'])
        Path(record['store']).rename(self.base/'offline')
        with self.assertRaises(CoreError) as raised: self.reserve(plan)
        self.assertEqual(raised.exception.code,'STORE_UNAVAILABLE')

    def test_completed_export_and_claims_protect_registration(self):
        plan=self.simple_plan();destination=self.base/'export'
        self.export_direct(plan,destination)
        for root in (destination,destination/'candidate'):
            response=self.service.handle(self.request('project.add',root=str(root)))
            self.assertFalse(response['ok'],response)
        with self.catalog.connect(write=True) as db: db.execute('DELETE FROM integration_claims WHERE operation_id=?',(self.export_operation,))
        with self.assertRaises(CoreError): self.service.workspace_paths.assert_unowned(destination)

    def test_export_does_not_hold_global_lock_across_materialization(self):
        plan=self.simple_plan(); entered=threading.Event(); release=threading.Event(); outcomes=[]
        original=IntegrationReview._materialize
        def paused(review,entries,fd):
            entered.set()
            if not release.wait(5): raise RuntimeError('test release absent')
            return original(review,entries,fd)
        def export():
            try: outcomes.append(self.export_direct(plan,self.base/'export'))
            except Exception as error: outcomes.append(error)
        with patch.object(IntegrationReview,'_materialize',paused):
            thread=threading.Thread(target=export);thread.start()
            try:
                self.assertTrue(entered.wait(5))
                # A concrete unrelated registration must finish while export is paused.
                other=self.base/'unrelated';other.mkdir();done=threading.Event();results=[]
                def register():
                    results.append(self.service.handle(self.request('project.add',root=str(other))));done.set()
                worker=threading.Thread(target=register);worker.start()
                try: self.assertTrue(done.wait(2),'global placement lock held during bulk export')
                finally: release.set();worker.join(5)
                self.assertTrue(results[0]['ok'],results)
            finally: release.set();thread.join(5)
        self.assertFalse(isinstance(outcomes[0],Exception),outcomes)

    def test_preparation_rechecks_claim_before_live_scan(self):
        task,result,_=self.ready_task({'b':b'other'})
        plan=self.simple_plan()
        journal=self.reserve(plan)
        with patch('solace_engine.repository.ProjectRepository._scan',side_effect=AssertionError('claimed tree was scanned')):
            with self.assertRaises(CoreError) as raised: self.prepare_direct(task,result)
        self.assertIn(raised.exception.code,('INTEGRATION_TARGET_BUSY','TASK_BUSY'))

    def test_queued_checkpoint_rechecks_claim_at_worker_entry(self):
        plan=self.simple_plan(); coordinator=self.service.coordinator(self.project)
        with patch.object(coordinator,'submit'):
            accepted=self.call('checkpoint.create',project=self.project,message='queued before reservation')
        journal=self.reserve(plan)
        request=self.request('checkpoint.create',message='queued before reservation')
        with patch('solace_engine.repository.ProjectRepository.create_checkpoint',side_effect=AssertionError('claimed tree captured')):
            self.service._work(self.catalog,accepted['operation_id'],request)
        outcome=self.catalog.get_operation(accepted['operation_id'])
        self.assertEqual(outcome['error']['code'],'INTEGRATION_TARGET_BUSY')
        self.assertFalse(journal.path.exists())

    def test_child_claim_hides_parent_path_and_leaves_other_workspace_usable(self):
        parent=self.create_task();self.start(parent)
        other=self.create_task();self.start(other)
        child,result,_=self.ready_task({'a':b'child'},parent=parent)
        plan=self.prepare_direct(child,result);journal=self.reserve(plan)
        shown=self.show(parent)
        self.assertIsNone(shown['workspace']['path'])
        self.assertEqual(shown['workspace']['availability'],'applying')
        request=self.request('workspace.status',task=parent,refresh=True)
        self.assertFalse(self.service.handle(request)['ok'])
        other_op=self.call('workspace.status',project=self.project,task=other,refresh=True)['operation_id']
        self.assertEqual(self.wait_operation(other_op)['state'],'succeeded')
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_journals SET phase='recovery_required' WHERE operation_id=?",(journal.operation_id,))
        self.assertEqual(self.show(parent)['workspace']['availability'],'recovery_required')
        self.assertIsNone(self.show(parent)['workspace']['path'])

    def test_read_claim_does_not_advertise_publication_as_applying(self):
        task=self.create_task();started=self.start(task)
        self.catalog=self.service.catalog(self.project)
        op=self.catalog.accept_operation(self.request('workspace.status',task=task,refresh=True))['operation_id']
        with self.registry.lock,self.service.coordinator(self.project).lock,self.catalog.connect(write=True) as db:
            self.claims().claim_access(db,op,[started['path']])
        shown=self.show(task)
        self.assertNotEqual(shown['workspace']['availability'],'applying')
        self.assertIsNone(shown['workspace']['path'])

    def test_reservation_races_registration_and_exactly_one_wins(self):
        plan=self.simple_plan();barrier=threading.Barrier(2);outcomes=[]
        op=self.catalog.accept_operation(self.request('integration.apply',plan=plan.id,revision=1))['operation_id']
        journal=self.root.parent/('.solace-integration-'+op)
        other=self.base/'race-source';other.mkdir()
        # A competing store registration wants the exact journal pathname.
        def reserve():
            barrier.wait()
            try:
                with self.registry.lock,self.service.coordinator(self.project).lock,self.catalog.connect(write=True) as db:
                    self.claims().reserve(db,op,plan)
                outcomes.append('reservation')
            except CoreError: outcomes.append('reservation refused')
        def register():
            barrier.wait()
            response=self.service.handle(self.request('project.add',root=str(other),store=str(journal)))
            outcomes.append('registration' if response['ok'] else 'registration refused')
        threads=[threading.Thread(target=reserve),threading.Thread(target=register)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(5);self.assertFalse(thread.is_alive())
        self.assertEqual(sum(x in ('reservation','registration') for x in outcomes),1,outcomes)
        self.assertEqual(journal.exists(),'registration' in outcomes)

    def test_changed_ancestor_and_new_protected_root_make_approval_stale(self):
        plan=self.simple_plan()
        parent=self.root.parent
        expected=directory_identity(parent,self.policy)
        from solace_core import integration_plans
        original=integration_plans.directory_identity
        def changed(path,policy):
            identity=original(path,policy)
            if path==parent:identity['inode']+=1
            return identity
        with patch.object(integration_plans,'directory_identity',changed):
            with self.assertRaises(CoreError) as raised:self.claims().validate(plan,scan=False)
        self.assertEqual(raised.exception.code,'INTEGRATION_PLAN_STALE')
        other=self.base/'other';other.mkdir();self.call('project.add',root=str(other))
        self.claims().validate(plan,scan=False)
        from solace_core.workspace_paths import CoreConfig
        config=self.service.workspace_paths.config
        changed=CoreConfig(sync_roots=(*config.sync_roots,self.root/'new-protected'),workspace_root=config.workspace_root)
        with patch.object(self.service.workspace_paths,'config',changed), self.assertRaises(CoreError) as raised:
            self.claims().validate(plan,scan=False)
        self.assertEqual(raised.exception.code,'INTEGRATION_PLAN_STALE')

    def test_empty_home_git_allows_journal_but_real_marker_refuses(self):
        plan=self.simple_plan(); marker=self.base/'.git';marker.mkdir()
        journal=self.reserve(plan)
        self.plans.abandon(self.request('integration.abandon',plan=plan.id,revision=plan.revision))
        plan=self.prepare_direct(plan.task_id,plan.result_id)
        (marker/'HEAD').write_text('ref: refs/heads/main\n')
        with self.assertRaises(CoreError) as raised:self.reserve(plan)
        self.assertIn(raised.exception.code,('INTEGRATION_JOURNAL_PLACEMENT','INTEGRATION_PLAN_STALE'))

    def test_authentication_rejects_valid_same_size_replacements_for_approval_export_resolve(self):
        plan=self.make_binary_conflict()
        self.review.resolve(self.request('integration.resolve',plan=plan.id,revision=1,path='data.bin',take='incoming'))
        plan=self.plans.load(plan.id)
        approval=self.approval_request(plan,accept_unchecked=True)
        original_token=approval['params']['token']
        for name in ('base','incoming','current'):
            version=getattr(plan,name);entry=next(e for e in self.plans.snapshots.entries(version) if e.path=='data.bin')
            data=b'x'*entry.size;digest=self.plans.snapshots.objects.put_bytes(data)
            with self.catalog.connect(write=True) as db:
                db.execute('UPDATE file_entries SET file_digest=? WHERE checkpoint_id=? AND path=?',(digest,version.checkpoint_id,entry.path))
                db.execute('DELETE FROM chunk_refs WHERE checkpoint_id=? AND path=?',(version.checkpoint_id,entry.path))
                db.execute('INSERT INTO chunk_refs VALUES (?,?,?,?)',(version.checkpoint_id,entry.path,0,digest))
            try:
                calls=(lambda:self.claims().approve(approval),
                       lambda:self.export_direct(plan,self.base/('corrupt-export-'+name)),
                       lambda:self.review.resolve(self.request('integration.resolve',plan=plan.id,revision=plan.revision,path=entry.path,take='current')))
                for call in calls:
                    with self.subTest(version=name,consumer=call):
                        with self.assertRaises(CoreError) as raised:call()
                        self.assertEqual(raised.exception.code,'INTEGRATION_MANIFEST_INVALID')
                with self.catalog.connect() as db:
                    self.assertEqual(db.execute('SELECT review_token FROM integration_revisions WHERE plan_id=? AND revision=?',(plan.id,plan.revision)).fetchone()[0],original_token)
            finally:
                with self.catalog.connect(write=True) as db:
                    db.execute('UPDATE file_entries SET file_digest=? WHERE checkpoint_id=? AND path=?',(entry.file_digest,version.checkpoint_id,entry.path))
                    db.execute('DELETE FROM chunk_refs WHERE checkpoint_id=? AND path=?',(version.checkpoint_id,entry.path))
                    db.executemany('INSERT INTO chunk_refs VALUES (?,?,?,?)',[(version.checkpoint_id,entry.path,i,d) for i,d in enumerate(entry.chunk_digests)])

    def test_journal_path_occupied_by_disjoint_registered_source_is_refused(self):
        task,result,_=self.ready_task({'a':b'result'})
        operation=uuid.uuid4().hex
        sibling=self.root.parent/('.solace-integration-'+operation);sibling.mkdir()
        (sibling/'keep').write_text('other source')
        self.call('project.add',root=str(sibling))
        plan=self.prepare_direct(task,result)
        request=self.request('integration.apply',plan=plan.id,revision=1)
        accepted=self.catalog.accept_operation(request)['operation_id']
        with self.catalog.connect(write=True) as db:db.execute('UPDATE operations SET id=? WHERE id=?',(operation,accepted))
        with self.assertRaises(CoreError) as raised:
            with self.registry.lock,self.service.coordinator(self.project).lock,self.catalog.connect(write=True) as db:
                self.claims().reserve(db,operation,plan)
        self.assertEqual(raised.exception.code,'INTEGRATION_JOURNAL_PLACEMENT')
        self.assertEqual((sibling/'keep').read_text(),'other source')
        with self.catalog.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_journals').fetchone()[0],0)

    def test_restart_retains_reserved_journal_but_releases_finished_read_claim(self):
        parent=self.create_task();self.start(parent)
        child,result,_=self.ready_task({'a':b'child'},parent=parent)
        plan=self.prepare_direct(child,result);journal=self.reserve(plan)
        read=self.catalog.accept_operation(self.request('checkpoint.create',message='finished read'))['operation_id']
        read_path=self.base/'read-tree';read_path.mkdir()
        with self.catalog.connect(write=True) as db:
            self.catalog._transition(db,read,'running')
            self.catalog._transition(db,read,'succeeded',result={})
            # Simulate an orphan retained by an older process or failed cleanup.
            self.claims().claim_access(db,read,[read_path])
        self.service.close()
        from solace_core.service import CoreService
        self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)
        self.catalog=self.service.catalog(self.project)
        with self.assertRaises(CoreError):self.claims().assert_access(Path(plan.target.identity.path))
        self.claims().assert_access(read_path)
        shown=self.show(parent)
        self.assertIsNone(shown['workspace']['path'])
        self.assertEqual(shown['workspace']['availability'],'recovery_required')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase,identity_json FROM integration_journals').fetchone()[:],('reserved',None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_claims WHERE operation_id=?',(journal.operation_id,)).fetchone()[0],2)

    def test_restore_worker_rechecks_destination_after_queued_registration(self):
        plan=self.simple_plan();destination=self.base/'late-registered'
        coordinator=self.service.coordinator(self.project)
        request=self.request('checkpoint.restore',checkpoint=plan.result_id,destination=str(destination))
        with patch.object(coordinator,'submit'):
            response=self.service.handle(request);self.assertTrue(response['ok'],response)
        destination.mkdir();self.call('project.add',root=str(destination))
        with patch('solace_engine.repository.ProjectRepository.restore_checkpoint',side_effect=AssertionError('managed destination touched')):
            self.service._work(self.catalog,response['result']['operation_id'],request)
        outcome=self.catalog.get_operation(response['result']['operation_id'])
        self.assertEqual(outcome['error']['code'],'UNSUPPORTED_STORAGE')

    def test_final_prepare_verification_does_not_hold_placement_lock(self):
        task,result,_=self.ready_task({'a':b'result'})
        from solace_core.integration_snapshots import SnapshotStore
        entered=threading.Event();release=threading.Event();outcomes=[]
        original=SnapshotStore._verify
        # Two initial loads/entries verify B/R; the final insertion verifies C.
        count=[0]
        def paused(store,entries):
            count[0]+=1
            if count[0]==5:
                entered.set()
                if not release.wait(5):raise RuntimeError('test release absent')
            return original(store,entries)
        def prepare():
            try:outcomes.append(self.prepare_direct(task,result))
            except Exception as error:outcomes.append(error)
        with patch.object(SnapshotStore,'_verify',paused):
            thread=threading.Thread(target=prepare);thread.start()
            try:
                self.assertTrue(entered.wait(5))
                other=self.base/'progress';other.mkdir();done=threading.Event();results=[]
                def register():
                    results.append(self.service.handle(self.request('project.add',root=str(other))));done.set()
                worker=threading.Thread(target=register);worker.start()
                try:self.assertTrue(done.wait(2),'placement lock held during final CAS verification')
                finally:release.set();worker.join(5)
                self.assertTrue(results[0]['ok'],results)
            finally:release.set();thread.join(5)
        # Changed placement may correctly invalidate the draft before publication.
        if isinstance(outcomes[0],CoreError):self.assertEqual(outcomes[0].code,'INTEGRATION_STALE')

    def test_verified_snapshot_evidence_rejects_fabricated_foreign_and_changed_binding(self):
        from dataclasses import replace
        from solace_core.integration_snapshots import SnapshotStore
        plan=self.simple_plan();store=self.plans.snapshots
        entries=store.entries(plan.candidate);profile=json.loads(plan.candidate.profile_json)
        prepared=store.verify_snapshot(entries,parent_id=plan.current.checkpoint_id,profile=profile,task_id=plan.task_id,purpose='source')
        other=SnapshotStore(self.catalog)
        for owner,evidence in ((other,prepared),(store,replace(prepared,profile_json='{}')),
                               (store,replace(prepared,parent_id=plan.base.checkpoint_id)),
                               (store,replace(prepared,entries=())),(store,object())):
            with self.subTest(owner=owner,evidence=type(evidence).__name__):
                with self.assertRaises(CoreError),self.catalog.connect(write=True) as db:owner.insert_verified(db,evidence)
        with self.assertRaises(RuntimeError):
            with self.catalog.connect(write=True) as db:
                version=store.insert_verified(db,prepared)
                raise RuntimeError('transaction rollback')
        with self.catalog.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM checkpoints WHERE id=?',(prepared.checkpoint_id,)).fetchone())
        with self.catalog.connect(write=True) as db:version=store.insert_verified(db,prepared)
        self.assertEqual(version.manifest_sha256,plan.candidate.manifest_sha256)
        self.assertEqual(store.entries(version),entries)
        changed=replace(prepared,store_identity=replace(prepared.store_identity,inode=prepared.store_identity.inode+1))
        with self.assertRaises(CoreError),self.catalog.connect(write=True) as db:store.insert_verified(db,changed)
        # A new verification always reads CAS again; prior evidence is never a restart cache.
        path=store.objects._object_path(entries[0].chunk_digests[0]); original=path.read_bytes()
        path.write_bytes(b'bad')
        try:
            with self.assertRaises(CoreError):
                store.verify_snapshot(entries,parent_id=plan.current.checkpoint_id,profile=profile,task_id=plan.task_id,purpose='source')
        finally:path.write_bytes(original)

    def test_cache_preparation_keeps_store_and_cache_subtree_owned(self):
        from solace_core.workspace_trees import BaseCache
        entered=threading.Event();release=threading.Event();original=BaseCache.ensure;cache_paths=[]
        def paused(cache,*args,**kwargs):
            result=original(cache,*args,**kwargs);cache_paths.append(Path(result['path']));entered.set()
            if not release.wait(5):raise RuntimeError('test release absent')
            return result
        task=self.create_task()
        with patch.object(BaseCache,'ensure',paused):
            accepted=self.mutate('task.start',task)
            try:
                self.assertTrue(entered.wait(5))
                record=self.registry.get(self.project)
                self.assertTrue(cache_paths[0].is_relative_to(Path(record['store'])))
                response=self.service.handle(self.request('project.add',root=str(cache_paths[0])))
                self.assertFalse(response['ok'],response)
                response=self.service.handle(self.request('project.relink',root=record['root'],store=record['store'],confirmed_root=record['root']))
                self.assertFalse(response['ok'],response)
                self.assertIn(response['error']['code'],('PROJECT_BUSY','TASK_BUSY','INTEGRATION_TARGET_BUSY'))
                with self.assertRaises(CoreError):self.service.workspace_paths.assert_unowned(cache_paths[0])
            finally:release.set()
            self.assertEqual(self.wait_operation(accepted['operation_id'])['state'],'succeeded')

    def test_new_configured_sync_boundary_invalidates_review(self):
        from solace_core.workspace_paths import CoreConfig
        plan=self.simple_plan()
        original=self.service.workspace_paths.config
        self.service.workspace_paths.config=CoreConfig(sync_roots=(self.root/'a',),workspace_root=original.workspace_root)
        with self.assertRaises(CoreError) as raised:self.claims().validate(plan,scan=False)
        self.assertEqual(raised.exception.code,'INTEGRATION_PLAN_STALE')

    def test_project_relink_cache_verification_allows_unrelated_registration(self):
        self.start(self.create_task())
        drained=threading.Event();self.service.coordinator(self.project).submit(uuid.uuid4().hex,drained.set)
        self.assertTrue(drained.wait(5))
        record=self.registry.get(self.project)
        entered=threading.Event();release=threading.Event();outcomes=[]
        original=self.service._rebase_recovery
        def paused(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('test release absent')
            return original(*args)
        def relink():
            outcomes.append(self.service.handle(self.request('project.relink',root=record['root'],store=record['store'],confirmed_root=record['root'])))
        with patch.object(self.service,'_rebase_recovery',paused):
            thread=threading.Thread(target=relink);thread.start()
            try:
                self.assertTrue(entered.wait(5),outcomes)
                other=self.base/'relink-progress';other.mkdir();done=threading.Event();responses=[]
                def register():
                    responses.append(self.service.handle(self.request('project.add',root=str(other))));done.set()
                worker=threading.Thread(target=register);worker.start()
                try:self.assertTrue(done.wait(2),'global placement lock held while relink verifies cache')
                finally:release.set();worker.join(5)
                self.assertTrue(responses[0]['ok'],responses)
            finally:release.set();thread.join(5)
        self.assertTrue(outcomes[0]['ok'],outcomes)

    def test_reserved_unchanged_child_retains_exact_validation_binding(self):
        parent=self.create_task();self.start(parent)
        child,result,_=self.ready_task({'a':b'child'},parent=parent)
        plan=self.prepare_direct(child,result)
        before=self.claims().validate(plan,scan=True)
        self.reserve(plan)
        self.assertEqual(self.claims().validate(plan,scan=True),before)

    def test_success_releases_reader_claim_in_same_transaction_as_receipt(self):
        self.catalog=self.service.catalog(self.project);task=self.create_task()
        entered=threading.Event();release=threading.Event();original=self.catalog.notify
        def paused():
            with self.catalog.connect() as db:
                done=db.execute("SELECT 1 FROM operations WHERE kind='task.start' AND state='succeeded'").fetchone()
            if done:
                entered.set()
                if not release.wait(5):raise RuntimeError('test release absent')
            original()
        with patch.object(self.catalog,'notify',paused):
            operation=self.mutate('task.start',task)['operation_id']
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.catalog.get_operation(operation)['state'],'succeeded')
                self.claims().assert_access(self.root)
                self.assertEqual(self.show(task)['workspace']['availability'],'available')
            finally:release.set()

    def test_queued_task_start_rechecks_source_claim_and_immutable_verify_still_runs(self):
        pending=self.create_task();plan=self.simple_plan()
        request=self.request('task.start',task=pending,expected_revision=1)
        with patch.object(self.service.coordinator(self.project),'submit'):
            accepted=self.service.handle(request)
        self.assertTrue(accepted['ok'],accepted)
        # Prepare after acceptance so the queued workspace intent is part of
        # the exact protected-boundary snapshot.
        plan=self.prepare_direct(plan.task_id,plan.result_id)
        self.reserve(plan)
        from solace_core.task_operations import TaskOperations
        with patch('solace_engine.repository.ProjectRepository.create_checkpoint',side_effect=AssertionError('claimed source read')):
            TaskOperations(self.service,self.catalog).run(accepted['result']['operation_id'],request)
        outcome=self.catalog.get_operation(accepted['result']['operation_id'])
        self.assertEqual(outcome['error']['code'],'INTEGRATION_TARGET_BUSY')
        verified=self.call('checkpoint.verify',project=self.project,checkpoint=plan.result_id)
        self.assertEqual(self.wait_operation(verified['operation_id'])['state'],'succeeded')

    def test_parent_revision_change_invalidates_child_approval(self):
        parent=self.create_task();self.start(parent)
        child,result,_=self.ready_task({'a':b'child'},parent=parent)
        plan=self.prepare_direct(child,result)
        self.mutate('task.block',parent,reason='state changed')
        with self.assertRaises(CoreError) as raised:self.claims().approve(self.approval_request(plan,accept_unchecked=True))
        self.assertEqual(raised.exception.code,'INTEGRATION_PLAN_STALE')

    def test_resolution_does_not_revive_plan_abandoned_during_long_read(self):
        plan=self.make_binary_conflict();edited=self.base/'edited';edited.write_bytes(b'edited')
        entered=threading.Event();release=threading.Event();outcomes=[]
        original=self.review._import
        def paused(*args):
            entered.set()
            if not release.wait(5):raise RuntimeError('test release absent')
            return original(*args)
        def resolve():
            try:outcomes.append(self.review.resolve(self.request('integration.resolve',plan=plan.id,revision=1,path='data.bin',from_file=str(edited))))
            except CoreError as error:outcomes.append(error)
        with patch.object(self.review,'_import',paused):
            thread=threading.Thread(target=resolve);thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.plans.abandon(self.request('integration.abandon',plan=plan.id,revision=1))
            finally:release.set();thread.join(5)
        self.assertIsInstance(outcomes[0],CoreError)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT state,current_revision FROM integration_plans').fetchone()[:],('abandoned',1))

    def test_unavailable_source_and_store_relink_keeps_unresolved_logical_gate(self):
        from solace_core.service import CoreService
        from solace_core.catalog import ProjectCatalog
        plan=self.simple_plan();journal=self.reserve(plan)
        registration=self.registry.get(self.project)
        self.service.close()
        moved_root=self.base/'moved-source';self.root.rename(moved_root)
        moved_store=self.base/'moved-store';Path(registration['store']).rename(moved_store)
        self.service=CoreService(self.registry,config=self.config);self.addCleanup(self.service.close)
        request=self.request('project.relink',root=str(moved_root),store=str(moved_store),confirmed_root=str(moved_root))
        response=self.service.handle(request)
        self.assertTrue(response['ok'],response)
        self.assertEqual(self.registry.get(self.project)['root'],str(moved_root))
        with self.assertRaises(CoreError): self.service.workspace_paths.assert_access(moved_root)
        replacement=ProjectCatalog(dict(registration,root=str(moved_root),store=str(moved_store)),self.policy)
        with replacement.connect() as db:
            self.assertEqual(db.execute('SELECT phase,identity_json FROM integration_journals WHERE operation_id=?',(journal.operation_id,)).fetchone()[:],('reserved',None))
            self.assertEqual(db.execute("SELECT path FROM integration_claims WHERE operation_id=? AND kind='target'",(journal.operation_id,)).fetchone()[0],str(self.root))
        self.assertFalse(self.service.handle(self.request('checkpoint.create',message='must remain unavailable'))['ok'])
        self.assertFalse(journal.path.exists())
        self.assertEqual((moved_root/'a').read_bytes(),b'base')

    def test_retired_nested_source_no_longer_blocks_approval_but_store_is_retained(self):
        nested=self.root/'retired';nested.mkdir()
        retired=self.call('project.add',root=str(nested))
        self.call('project.unregister',project=retired['id'])
        plan=self.simple_plan()
        self.assertEqual(self.claims().validate(plan,scan=True)['revision'],1)
        self.assertRegex(self.claims().approve(self.approval_request(plan,accept_unchecked=True))['approval'],r'^[0-9a-f]{64}$')
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_unowned(Path(retired['store'])/'candidate')
        self.reserve(plan)

    def test_reregistered_source_does_not_overlap_its_retired_registration(self):
        previous=self.project
        self.call('project.unregister',project=previous)
        self.project=self.call('project.add',root=str(self.root))['id']
        self.assertNotEqual(previous,self.project)
        plan=self.simple_plan()
        self.assertEqual(self.claims().validate(plan,scan=True)['revision'],1)
        self.reserve(plan)
