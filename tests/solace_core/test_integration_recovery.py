"""Explicit recovery never turns a restart into permission to mutate files."""
import json
import os
from pathlib import Path
from unittest.mock import patch

from integration_fixture import IntegrationCase
import test_integration_publish as publishing
from solace_core.errors import CoreError
from solace_core.service import CoreService


class RecoveryTests(IntegrationCase):
    plan_with = publishing.PublishTests.plan_with
    reserve = publishing.PublishTests.reserve
    staged_replacement = publishing.PublishTests.staged_replacement
    actions = publishing.PublishTests.actions
    displaced_version_exists = publishing.PublishTests.displaced_version_exists

    def restart(self):
        self.service.close()
        self.service = CoreService(self.registry, config=self.config)
        self.addCleanup(self.service.close)
        self.catalog = self.service.catalog(self.project)
        from solace_core.integration_recovery import IntegrationRecovery
        self.recovery = IntegrationRecovery(self.service, self.catalog)

    def interrupt(self, journal, point):
        def crash(name):
            if name == point: raise SystemExit('disposable crash')
        self.publisher.failpoint = crash
        with self.assertRaises(SystemExit): self.publisher.publish(journal)
        self.restart()

    def recover(self, journal, mode='resume', **params):
        request = self.request('integration.recovery.'+mode, operation=journal.operation_id,
                               writers_paused=True, **params)
        accepted = self.recovery.accept(request)
        self.recovery.run(accepted['operation_id'], request)
        return request, accepted, self.catalog.get_operation(accepted['operation_id'])

    def test_restart_preserves_partial_tree_and_claims_then_explicit_resume(self):
        plan, journal = self.staged_replacement()
        self.interrupt(journal, 'old_moved:0')
        self.assertFalse((self.root/'file').exists())
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        original = self.catalog.get_operation(journal.operation_id)
        self.assertEqual(original['state'], 'interrupted')
        with self.assertRaises(CoreError): self.service.workspace_paths.assert_access(self.root)
        self.assertEqual(self.recovery.show(journal.operation_id)['state'], 'recovery_required')
        _, _, done = self.recover(journal)
        self.assertEqual(done['state'], 'succeeded', done)
        self.assertEqual((self.root/'file').read_bytes(), b'incoming')
        self.assertEqual(self.catalog.get_operation(journal.operation_id), original)
        self.service.workspace_paths.assert_access(self.root)

    def test_restore_before_retains_both_versions_and_original_outcome(self):
        plan, journal = self.staged_replacement()
        self.interrupt(journal, 'new_published:1')
        original = self.catalog.get_operation(journal.operation_id)
        _, _, done = self.recover(journal, 'restore-before')
        self.assertEqual(done['state'], 'succeeded', done)
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assertTrue(any(p.read_bytes() == b'incoming' for p in journal.path.iterdir() if p.is_file() and p.name.startswith('restore-')))
        self.assertEqual(self.catalog.get_operation(journal.operation_id), original)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM integration_plans WHERE id=?',(plan.id,)).fetchone()[0], 'restored')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_receipts').fetchone()[0], 0)
        self.service.workspace_paths.assert_access(self.root)

    def test_foreign_same_bytes_winner_blocks_restoration(self):
        _, journal = self.staged_replacement()
        self.interrupt(journal, 'new_published:1')
        os.rename(self.root/'file', self.base/'saved-published')
        (self.root/'file').write_bytes(b'incoming')
        with self.assertRaises(CoreError): self.recover(journal, 'restore-before')
        self.assertEqual((self.root/'file').read_bytes(), b'incoming')
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        with self.assertRaises(CoreError): self.service.workspace_paths.assert_access(self.root)

    def test_files_ready_recovery_replays_after_committed_reply_loss(self):
        _, journal = self.staged_replacement()
        self.publisher.publish(journal)
        self.publisher.verify_final(journal)
        self.restart()
        request, accepted, done = self.recover(journal)
        self.assertEqual(done['state'], 'succeeded', done)
        (self.root/'file').write_bytes(b'after successful recovery')
        self.assertEqual(self.recovery.accept(request), accepted)
        self.recovery.run(accepted['operation_id'], request)
        self.assertEqual((self.root/'file').read_bytes(), b'after successful recovery')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_receipts').fetchone()[0], 1)

    def test_unknown_created_directory_cannot_be_adopted(self):
        plan = self.plan_with(lambda root: ((root/'added').mkdir(), (root/'added/file').write_bytes(b'new')))
        reserved = self.reserve(plan)
        journal = self.publisher.stage(reserved.operation_id, plan)
        self.interrupt(journal, 'dir_changed:0')
        with self.assertRaises(CoreError): self.recover(journal)
        self.assertTrue((self.root/'added').is_dir())

    def test_missing_target_is_deferred_and_not_recreated(self):
        _, journal = self.staged_replacement()
        self.interrupt(journal, 'old_moved:0')
        self.root.rename(self.base/'offline')
        self.assertEqual(self.recovery.show(journal.operation_id)['validation'], 'deferred')
        with self.assertRaises(CoreError): self.recover(journal)
        self.assertFalse(self.root.exists())

    def test_restore_crashes_after_move_and_restarts_without_writes(self):
        _, journal = self.staged_replacement()
        self.interrupt(journal, 'new_published:1')
        def crash(name):
            if name == 'restore_changed:0': raise SystemExit('recovery crash')
        self.recovery.publisher.failpoint = crash
        with self.assertRaises(SystemExit): self.recover(journal, 'restore-before')
        self.assertFalse((self.root/'file').exists())
        self.restart()
        self.assertFalse((self.root/'file').exists())
        _, _, done = self.recover(journal, 'restore-before')
        self.assertEqual(done['state'], 'succeeded', done)
        self.assertEqual((self.root/'file').read_bytes(), b'original')

    def test_resume_intent_before_move_and_completed_prefix(self):
        for point in ('intent:0', 'progress:0', 'intent:1', 'progress:1'):
            with self.subTest(point=point):
                # Each publication has its own retained result and unchanged project history.
                _, journal = self.staged_replacement(name=point.replace(':','-'))
                self.interrupt(journal, point)
                _, _, done = self.recover(journal)
                self.assertEqual(done['state'], 'succeeded', done)

    def test_restore_directory_replacements_preserves_unaffected_inode(self):
        (self.root/'keep').write_bytes(b'keep')
        inode=(self.root/'keep').stat().st_ino
        (self.root/'directory').mkdir(); (self.root/'directory/file').write_bytes(b'old')
        (self.root/'leaf').write_bytes(b'old leaf')
        def edit(root):
            (root/'directory/file').unlink(); (root/'directory').rmdir(); (root/'directory').write_bytes(b'leaf')
            (root/'leaf').unlink(); (root/'leaf').mkdir(); (root/'leaf/new').write_bytes(b'new')
        plan=self.plan_with(edit); reserved=self.reserve(plan); journal=self.publisher.stage(reserved.operation_id,plan)
        self.publisher.publish(journal);self.restart()
        _, _, done=self.recover(journal,'restore-before')
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual((self.root/'directory/file').read_bytes(),b'old')
        self.assertEqual((self.root/'leaf').read_bytes(),b'old leaf')
        self.assertEqual((self.root/'keep').stat().st_ino,inode)

    def test_partial_relocation_group_refused_without_new_operation(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        with self.assertRaises(CoreError): self.recover(journal,target_path=str(self.root))

    def test_moved_project_store_missing_journal_keeps_logical_gate(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        old=self.registry.get(self.project)
        new_root=self.base/'relocated-source';new_store=self.base/'relocated-store'
        self.root.rename(new_root);Path(old['store']).rename(new_store)
        result=self.call('project.relink',project=self.project,root=str(new_root),store=str(new_store),confirmed_root=str(new_root))
        self.assertEqual(result['root'],str(new_root))
        self.restart()
        with self.assertRaises(CoreError): self.service.workspace_paths.assert_access(new_root)
        denied=self.service.handle(self.request('project.add',root=str(new_root),store=str(self.base/'other-store')))
        self.assertFalse(denied['ok'],denied)
        self.assertEqual(denied['error']['code'],'INTEGRATION_TARGET_BUSY')
        missing=self.base/'missing-journal'
        with self.assertRaises(CoreError): self.recover(journal,target_path=str(new_root),journal_path=str(missing),confirmed_target=str(new_root),confirmed_journal=str(missing))
        self.assertFalse(missing.exists())
        self.assertFalse((new_root/'file').exists())

    def test_explicit_moved_target_store_handoff_then_resume(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        old=self.registry.get(self.project)
        new_root=self.base/'relocated-source';new_store=self.base/'relocated-store';new_journal=self.base/('relocated-'+journal.path.name)
        self.root.rename(new_root);Path(old['store']).rename(new_store);journal.path.rename(new_journal)
        self.call('project.relink',project=self.project,root=str(new_root),store=str(new_store),confirmed_root=str(new_root))
        self.restart()
        request,accepted,done=self.recover(journal,target_path=str(new_root),journal_path=str(new_journal),confirmed_target=str(new_root),confirmed_journal=str(new_journal))
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual((new_root/'file').read_bytes(),b'incoming')
        self.assertEqual(self.recovery.accept(request),accepted)
        self.service.workspace_paths.assert_access(new_root)

    def test_handoff_crash_replays_only_on_explicit_recovery(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        old=self.registry.get(self.project)
        new_root=self.base/'moved-source';new_store=self.base/'moved-store';new_journal=self.base/('moved-'+journal.path.name)
        self.root.rename(new_root);Path(old['store']).rename(new_store);journal.path.rename(new_journal)
        self.call('project.relink',project=self.project,root=str(new_root),store=str(new_store),confirmed_root=str(new_root))
        self.restart()
        def crash(name):
            if name=='location_committed': raise SystemExit('handoff crash')
        self.recovery.publisher.failpoint=crash
        mapping=dict(target_path=str(new_root),journal_path=str(new_journal),confirmed_target=str(new_root),confirmed_journal=str(new_journal))
        with self.assertRaises(SystemExit): self.recover(journal,**mapping)
        self.restart()
        self.assertFalse((new_root/'file').exists())
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(new_root)
        _,_,done=self.recover(journal)
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual((new_root/'file').read_bytes(),b'incoming')

    def test_moved_workspace_retains_owner_and_can_finish(self):
        parent=self.create_task();workspace=Path(self.start(parent)['path'])
        child,result,_=self.ready_task({'file':b'incoming'},parent=parent)
        plan=self.prepare_direct(child,result);reserved=self.reserve(plan);journal=self.publisher.stage(reserved.operation_id,plan)
        self.publisher.publish(journal);self.restart()
        moved=workspace.parent/'relocated-parent';moved_journal=workspace.parent/('moved-'+journal.path.name)
        workspace.rename(moved);journal.path.rename(moved_journal)
        _,_,done=self.recover(journal,target_path=str(moved),journal_path=str(moved_journal),confirmed_target=str(moved),confirmed_journal=str(moved_journal))
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual(self.show(parent)['workspace']['path'],str(moved))
        self.assertEqual((moved/'file').read_bytes(),b'incoming')

    def test_mapping_tampering_refuses_completion(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        old=self.registry.get(self.project)
        new_root=self.base/'moved-source';new_store=self.base/'moved-store';new_journal=self.base/('moved-'+journal.path.name)
        self.root.rename(new_root);Path(old['store']).rename(new_store);journal.path.rename(new_journal)
        self.call('project.relink',project=self.project,root=str(new_root),store=str(new_store),confirmed_root=str(new_root))
        self.restart()
        def crash(name):
            if name=='location_committed': raise SystemExit('handoff crash')
        self.recovery.publisher.failpoint=crash
        with self.assertRaises(SystemExit): self.recover(journal,target_path=str(new_root),journal_path=str(new_journal),confirmed_target=str(new_root),confirmed_journal=str(new_journal))
        self.restart()
        with self.catalog.connect(write=True) as db:
            row=db.execute('SELECT operation_id,location_json FROM integration_recoveries ORDER BY rowid DESC LIMIT 1').fetchone()
            location=json.loads(row['location_json']);location['mapping']['new'].pop('store')
            db.execute('UPDATE integration_recoveries SET location_json=? WHERE operation_id=?',(json.dumps(location),row['operation_id']))
        with self.assertRaises(CoreError): self.recover(journal)
        self.assertFalse((new_root/'file').exists())

    def test_reserved_without_directory_can_resume_but_unknown_directory_cannot(self):
        plan=self.plan_with(lambda root:(root/'new').write_bytes(b'new'))
        journal=self.reserve(plan);self.restart()
        _,_,done=self.recover(journal)
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual((self.root/'new').read_bytes(),b'new')

    def test_reserved_unidentified_directory_stays_conflict(self):
        plan=self.plan_with(lambda root:(root/'new').write_bytes(b'new'))
        journal=self.reserve(plan);journal.path.mkdir(mode=0o700);self.restart()
        with self.assertRaises(CoreError):self.recover(journal)
        self.assertTrue(journal.path.exists());self.assertFalse((self.root/'new').exists())

    def test_pending_handoff_crash_reuses_confirmed_mapping_after_restart(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        old=self.registry.get(self.project)
        new_root=self.base/'moved-source';new_store=self.base/'moved-store';new_journal=self.base/('moved-'+journal.path.name)
        self.root.rename(new_root);Path(old['store']).rename(new_store);journal.path.rename(new_journal)
        self.call('project.relink',project=self.project,root=str(new_root),store=str(new_store),confirmed_root=str(new_root))
        self.restart()
        def crash(name):
            if name=='location_intent': raise SystemExit('pending handoff crash')
        self.recovery.publisher.failpoint=crash
        with self.assertRaises(SystemExit):self.recover(journal,target_path=str(new_root),journal_path=str(new_journal),confirmed_target=str(new_root),confirmed_journal=str(new_journal))
        self.restart()
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(new_journal)
        self.assertFalse((new_root/'file').exists())
        _,_,done=self.recover(journal)
        self.assertEqual(done['state'],'succeeded',done)

    def test_corrupted_restoration_step_cannot_escape_target(self):
        winner=self.root/'foreign';winner.write_bytes(b'original')
        _,journal=self.staged_replacement();self.interrupt(journal,'new_published:1')
        def crash(name):
            if name=='restore_progress:0':raise SystemExit('after first restore move')
        self.recovery.publisher.failpoint=crash
        with self.assertRaises(SystemExit):self.recover(journal,'restore-before')
        self.restart();meta=winner.stat()
        with self.catalog.connect(write=True) as db:
            row=db.execute('SELECT operation_id,location_json FROM integration_recoveries ORDER BY rowid DESC LIMIT 1').fetchone()
            location=json.loads(row['location_json']);step=location['restoration']['steps'][1]
            step['source'].update(space='target',name='foreign',identity=dict(device=meta.st_dev,inode=meta.st_ino,mode=meta.st_mode))
            step['destination']['identity']=step['source']['identity']
            db.execute('UPDATE integration_recoveries SET location_json=? WHERE operation_id=?',(json.dumps(location),row['operation_id']))
        with self.assertRaises(CoreError):self.recover(journal,'restore-before')
        self.assertTrue(winner.exists(),'Recovery moved an unaffected file')
        self.assertEqual(winner.read_bytes(),b'original')

    def test_reserved_restore_before_and_staged_restore_before_need_no_file_mutation(self):
        plan=self.plan_with(lambda root:(root/'new').write_bytes(b'new'))
        journal=self.reserve(plan);self.restart()
        _,_,done=self.recover(journal,'restore-before')
        self.assertEqual(done['state'],'succeeded',done)
        self.assertFalse((self.root/'new').exists());self.assertFalse(journal.path.exists())

    def test_same_location_resume_checks_foreign_slot_and_unchanged_leaf(self):
        (self.root/'unchanged').write_bytes(b'kept')
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        (self.root/'unchanged').rename(self.base/'saved-unchanged')
        (self.root/'unchanged').write_bytes(b'kept')
        with self.assertRaises(CoreError):self.recover(journal)
        self.assertFalse((self.root/'file').exists())

    def test_done_marker_before_sql_progress_is_reconciled(self):
        _,journal=self.staged_replacement();self.publisher.publish(journal)
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_actions SET phase='intent',observed_json=NULL WHERE operation_id=? AND ordinal=1",(journal.operation_id,))
        self.restart();_,_,done=self.recover(journal)
        self.assertEqual(done['state'],'succeeded',done)

    def test_missing_restoration_completion_marker_remains_conflict(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'new_published:1')
        def crash(name):
            if name=='restore_progress:0':raise SystemExit('restoration done before next step')
        self.recovery.publisher.failpoint=crash
        with self.assertRaises(SystemExit):self.recover(journal,'restore-before')
        for marker in journal.path.glob('restore-*.done.json'):marker.unlink()
        self.restart()
        with self.assertRaises(CoreError):self.recover(journal,'restore-before')
        self.assertFalse((self.root/'file').exists())
        self.assertTrue(self.displaced_version_exists(journal,b'original'))

    def test_restoration_transaction_failure_retains_claims_until_verified_retry(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'new_published:1')
        release=self.recovery.claims.release
        def fail(db,operation):
            release(db,operation)
            raise RuntimeError('after release before commit')
        with patch.object(self.recovery.claims,'release',side_effect=fail):
            with self.assertRaises(CoreError):self.recover(journal,'restore-before')
        self.assertEqual((self.root/'file').read_bytes(),b'original')
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(self.root)
        _,_,done=self.recover(journal,'restore-before')
        self.assertEqual(done['state'],'succeeded',done)
        self.service.workspace_paths.assert_access(self.root)

    def test_relocated_uncreated_reservation_keeps_logical_gate_without_marker(self):
        plan=self.plan_with(lambda root:(root/'new').write_bytes(b'new'))
        journal=self.reserve(plan);self.restart()
        old=self.registry.get(self.project)
        moved=self.base/'moved-source';store=self.base/'moved-store';missing=self.base/'unidentified-journal'
        self.root.rename(moved);Path(old['store']).rename(store)
        self.call('project.relink',project=self.project,root=str(moved),store=str(store),confirmed_root=str(moved))
        self.restart()
        with self.assertRaisesRegex(CoreError,'unidentified journal'):
            self.recover(journal,'restore-before',target_path=str(moved),journal_path=str(missing),confirmed_target=str(moved),confirmed_journal=str(missing))
        self.assertEqual(self.recovery.show(journal.operation_id)['state'],'recovery_required')
        self.assertEqual(self.recovery.show(journal.operation_id)['validation'],'deferred')
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(moved)
        self.assertFalse(missing.exists());self.assertFalse((moved/'new').exists())

    def test_recovery_file_io_leaves_metadata_and_other_projects_available(self):
        import threading
        plan,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        entered=threading.Event();release=threading.Event();observed=threading.Event();errors=[];responses=[];blocked=[]
        def pause(name):
            if name=='intent:1':
                entered.set()
                if not release.wait(10):raise RuntimeError('test release absent')
        self.recovery.publisher.failpoint=pause
        def recover():
            try:self.recover(journal)
            except BaseException as error:errors.append(error)
        def inspect_other():
            try:
                responses.append(self.service.handle(self.request('task.show',task=plan.task_id)))
                blocked.append(self.service.handle(self.request('checkpoint.create',message='must stay blocked')))
                unrelated=self.base/'unrelated';unrelated.mkdir()
                responses.append(self.service.handle(self.request('project.add',root=str(unrelated))))
            finally:observed.set()
        worker=threading.Thread(target=recover);worker.start()
        self.assertTrue(entered.wait(5))
        reader=threading.Thread(target=inspect_other);reader.start()
        try:
            if not observed.wait(5):
                import sys, traceback
                print('Concurrent responses completed:',responses,'blocked:',blocked)
                traceback.print_stack(sys._current_frames()[reader.ident])
                self.fail('Recovery file I/O blocks metadata and unrelated registration')
        finally:
            release.set();worker.join(10);reader.join(10)
        self.assertTrue(all(response['ok'] for response in responses),responses)
        self.assertEqual(blocked[0]['error']['code'],'INTEGRATION_TARGET_BUSY')
        # New registration changes the exact reviewed boundary set. Completion
        # conservatively refuses it while retaining files and ownership.
        self.assertTrue(all(isinstance(error,CoreError) for error in errors),errors)
        if errors:
            with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(self.root)

    def test_rejected_location_does_not_claim_foreign_tree_or_poison_next_request(self):
        _,journal=self.staged_replacement();self.interrupt(journal,'old_moved:0')
        foreign=self.base/'foreign-target';foreign.mkdir();(foreign/'keep').write_bytes(b'foreign')
        with self.assertRaises(CoreError):
            self.recover(journal,target_path=str(foreign),journal_path=str(journal.path),confirmed_target=str(foreign),confirmed_journal=str(journal.path))
        self.service.workspace_paths.assert_access(foreign)
        _,_,done=self.recover(journal)
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual((foreign/'keep').read_bytes(),b'foreign')

    def moved_restoration(self, point):
        _,journal=self.staged_replacement();self.interrupt(journal,'new_published:1')
        original=self.catalog.get_operation(journal.operation_id)
        def crash(name):
            if name==point:raise SystemExit('interrupted inverse move')
        self.recovery.publisher.failpoint=crash
        with self.assertRaises(SystemExit):self.recover(journal,'restore-before')
        self.restart()
        old=self.registry.get(self.project)
        moved=self.base/'moved-source';store=self.base/'moved-store';moved_journal=self.base/('moved-'+journal.path.name)
        self.root.rename(moved);Path(old['store']).rename(store);journal.path.rename(moved_journal)
        self.call('project.relink',project=self.project,root=str(moved),store=str(store),confirmed_root=str(moved))
        self.restart()
        self.assertFalse((moved/'file').exists())
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(moved)
        return journal,original,moved,moved_journal

    def finish_moved_restoration(self, point):
        journal,original,moved,moved_journal=self.moved_restoration(point)
        with self.catalog.connect() as db:
            original_intent=db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?',(journal.operation_id,)).fetchone()[0]
            approvals=[tuple(row) for row in db.execute('SELECT * FROM integration_approvals')]
        _,_,done=self.recover(journal,'restore-before',target_path=str(moved),journal_path=str(moved_journal),confirmed_target=str(moved),confirmed_journal=str(moved_journal))
        self.assertEqual(done['state'],'succeeded',done)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?',(journal.operation_id,)).fetchone()[0],original_intent)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM integration_approvals')],approvals)
        self.assertEqual((moved/'file').read_bytes(),b'original')
        self.assertTrue(any(p.read_bytes()==b'incoming' for p in moved_journal.iterdir() if p.is_file() and p.name.startswith('restore-')))
        self.assertEqual(self.catalog.get_operation(journal.operation_id),original)
        self.service.workspace_paths.assert_access(moved)

    def test_moved_restoration_reconciles_move_before_progress(self):
        self.finish_moved_restoration('restore_changed:0')

    def test_moved_restoration_reconciles_progress_before_crash(self):
        self.finish_moved_restoration('restore_progress:0')

    def test_moved_restoration_refuses_mismatched_inverse_marker_before_handoff(self):
        journal,original,moved,moved_journal=self.moved_restoration('restore_progress:0')
        marker=next(moved_journal.glob('restore-*.done.json'))
        value=json.loads(marker.read_text());value['record']['destination']['name']='foreign'
        marker.write_text(json.dumps(value))
        preserved={str(path.relative_to(moved_journal)):path.read_bytes() for path in moved_journal.rglob('*') if path.is_file()}
        with self.assertRaisesRegex(CoreError,'Restoration completion marker changed'):
            self.recover(journal,'restore-before',target_path=str(moved),journal_path=str(moved_journal),confirmed_target=str(moved),confirmed_journal=str(moved_journal))
        self.assertFalse((moved/'file').exists())
        self.assertEqual(self.catalog.get_operation(journal.operation_id),original)
        with self.catalog.connect() as db:
            row=db.execute('SELECT journal_path FROM integration_journals WHERE operation_id=?',(journal.operation_id,)).fetchone()
            self.assertEqual(row[0],str(journal.path))
        self.assertEqual({str(path.relative_to(moved_journal)):path.read_bytes() for path in moved_journal.rglob('*') if path.is_file()},preserved)
        with self.assertRaises(CoreError):self.service.workspace_paths.assert_access(moved)
