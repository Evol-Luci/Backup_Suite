"""Retained review revisions, bounded byte views and independent exports."""
import base64
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

from integration_fixture import IntegrationCase
from solace_core.errors import CoreError
from solace_core.integration_snapshots import canonical
from solace_core.observations import encoded_size


class ReviewTests(IntegrationCase):
    def resolve(self, plan, **params):
        return self.review.resolve(self.request('integration.resolve', plan=plan.id,
            revision=plan.revision, path=params.pop('path', 'data.bin'), **params))

    def test_take_incoming_appends_revision_preserves_all_versions_and_target(self):
        plan = self.make_binary_conflict()
        request = self.request('integration.resolve', plan=plan.id, revision=1, path='data.bin', take='incoming')
        result = self.review.resolve(request)
        self.assertEqual((result['revision'], result['state']), (2, 'prepared'))
        revised = self.plans.load(plan.id)
        self.assertEqual(self.version_bytes(revised.candidate)['data.bin'], b'\0incoming')
        self.assertEqual(self.version_bytes(plan.current)['data.bin'], b'\0current')
        self.assertEqual(self.version_bytes(plan.incoming)['data.bin'], b'\0incoming')
        self.assertEqual(self.plans.load(plan.id, 1).candidate, plan.candidate)
        self.assertEqual((self.root/'data.bin').read_bytes(), b'\0current')
        self.assertEqual(self.review.resolve(request), result)
        self.assertNotEqual(self.review.show(plan.id)['review_token'], self.token(plan.id, 1))

    def token(self, plan, revision):
        with self.catalog.connect() as db:
            return db.execute('SELECT review_token FROM integration_revisions WHERE plan_id=? AND revision=?', (plan, revision)).fetchone()[0]

    def test_historical_conflict_can_be_resolved_again_after_approval(self):
        plan = self.make_binary_conflict()
        self.resolve(plan, take='incoming')
        with self.catalog.connect(write=True) as db:
            db.execute("UPDATE integration_plans SET state='approved' WHERE id=?", (plan.id,))
        second = self.plans.load(plan.id)
        self.resolve(second, take='current')
        third = self.plans.load(plan.id)
        self.assertEqual((third.revision, third.state), (3, 'prepared'))
        self.assertEqual(self.version_bytes(third.candidate)['data.bin'], b'\0current')

    def test_stale_revision_absent_conflict_invalid_deletion_and_terminal_refused(self):
        plan = self.make_binary_conflict()
        for params in (dict(path='other', take='delete'), dict(take='unknown')):
            with self.assertRaises(CoreError): self.resolve(plan, **params)
        self.resolve(plan, take='delete')
        with self.assertRaises(CoreError): self.resolve(self.plans.load(plan.id), take='delete')
        with self.assertRaises(CoreError): self.resolve(plan, take='current')
        for state in ('applying','applied','recovery_required','restored','abandoned'):
            with self.catalog.connect(write=True) as db:
                db.execute('UPDATE integration_plans SET state=? WHERE id=?', (state, plan.id))
            with self.assertRaises(CoreError): self.resolve(self.plans.load(plan.id), take='incoming')

    def test_never_conflicted_file_cannot_be_edited(self):
        (self.root/'unrelated').write_bytes(b'keep')
        plan = self.make_binary_conflict()
        with self.assertRaises(CoreError): self.resolve(plan, path='unrelated', take='delete')
        self.assertEqual(self.plans.load(plan.id).revision, 1)

    def test_prefix_resolution_selects_complete_subtree(self):
        (self.root/'a').mkdir(); (self.root/'a'/'b').write_bytes(b'base')
        task = self.create_task(); started = self.start(task); root = Path(started['path'])
        (root/'a'/'b').unlink(); (root/'a').rmdir(); (root/'a').write_bytes(b'incoming')
        done = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        (self.root/'a'/'b').write_bytes(b'current')
        plan = self.prepare_direct(task, done['result']['checkpoint_id'])
        from solace_core.integration_review import IntegrationReview
        self.review = IntegrationReview(self.service, self.catalog)
        self.assertTrue(plan.conflicts)
        self.resolve(plan, path='a', take='incoming')
        revised = self.plans.load(plan.id)
        self.assertEqual(self.version_bytes(revised.candidate)['a'], b'incoming')
        self.assertNotIn('a/b', self.version_bytes(revised.candidate))
        self.assertEqual(revised.conflicts, ())

    def test_literal_backslash_resolution_imports_exact_stable_bytes(self):
        plan = self.make_binary_conflict('literal\\name')
        edited = self.base/'edited'; edited.write_bytes(b'edited\0bytes')
        alias = self.base/'hardlink'; os.link(edited, alias)
        self.resolve(plan, path='literal\\name', from_file=str(edited))
        self.assertEqual(self.version_bytes(self.plans.load(plan.id).candidate)['literal\\name'], b'edited\0bytes')
        self.assertEqual(alias.read_bytes(), b'edited\0bytes')
        self.assertEqual(edited.stat().st_ino, alias.stat().st_ino)

    def test_import_rejects_symlink_ancestors_leaf_directories_and_fifo(self):
        plan = self.make_binary_conflict()
        real = self.base/'real'; real.mkdir(); (real/'edit').write_bytes(b'ok')
        linked = self.base/'linked'; linked.symlink_to(real, target_is_directory=True)
        leaf = self.base/'leaf'; leaf.symlink_to(real/'edit')
        fifo = self.base/'fifo'; os.mkfifo(fifo)
        for path in (linked/'edit', leaf, real, fifo):
            with self.assertRaises(CoreError): self.resolve(plan, from_file=str(path))
        self.assertEqual(self.plans.load(plan.id).revision, 1)

    def test_import_refuses_file_changed_during_stream(self):
        plan = self.make_binary_conflict()
        edited = self.base/'edited'; edited.write_bytes(b'x'*200000)
        read = os.read; changed = False
        identity = (edited.stat().st_dev, edited.stat().st_ino)
        def mutate(fd, size):
            nonlocal changed
            data = read(fd, size)
            opened = os.fstat(fd)
            if not changed and data and (opened.st_dev, opened.st_ino) == identity:
                changed = True; edited.write_bytes(b'y'*200000)
            return data
        with patch('solace_core.integration_review.os.read', side_effect=mutate):
            with self.assertRaises(CoreError): self.resolve(plan, from_file=str(edited))
        self.assertEqual(self.plans.load(plan.id).revision, 1)

    def test_resolution_revalidates_unchanged_symlink_graph(self):
        # Both retained input graphs are safe independently.
        (self.root/'gate').mkdir(); (self.root/'gate'/'leaf').write_bytes(b'base')
        (self.root/'dir').mkdir()
        (self.root/'dir'/'link').symlink_to('../gate/leaf')
        task = self.create_task(); root = Path(self.start(task)['path'])
        (root/'gate'/'leaf').unlink(); (root/'gate').rmdir(); (root/'gate').symlink_to('dir/link')
        (root/'dir'/'link').unlink()  # Incoming graph remains valid independently.
        done = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        (self.root/'gate'/'leaf').write_bytes(b'current')
        (self.root/'dir'/'link').unlink(); (self.root/'dir'/'link').symlink_to('../gate')
        plan = self.prepare_direct(task, done['result']['checkpoint_id'])
        from solace_core.integration_review import IntegrationReview
        self.review = IntegrationReview(self.service, self.catalog)
        # Selecting incoming gate would cycle through the retained current link.
        with self.assertRaises(CoreError): self.resolve(plan, path='gate', take='incoming')
        self.assertEqual(self.plans.load(plan.id).revision, 1)

    def test_summary_is_bounded_and_last_validation_is_not_live_freshness(self):
        plan = self.make_binary_conflict()
        (self.root/'data.bin').write_bytes(b'changed again')
        summary = self.review.show(plan.id)
        self.assertLessEqual(encoded_size(summary), 48*1024)
        self.assertIn('validated_at', summary)
        self.assertEqual(summary['counts']['conflicts'], 1)
        self.assertNotIn('fresh', summary)
        self.assertNotIn('review_token', summary)

    def test_metadata_pages_bound_escaped_rows_and_pin_revision(self):
        plan = self.make_binary_conflict()
        with self.catalog.connect(write=True) as db:
            rows = [(plan.id,1,i+1, f'{i:05d}'+('"\\\n'*100), 'keep_current',None, canonical({'candidate':None})) for i in range(10000)]
            db.executemany('INSERT INTO integration_decisions VALUES (?,?,?,?,?,?,?)', rows)
        after = 0; count = 0
        while True:
            page = self.review.diff(dict(plan=plan.id, revision=1, after=after, limit=256))
            self.assertLessEqual(encoded_size(page),128*1024)
            count += len(page['entries'])
            if page['next_after'] is None: break
            self.assertGreater(page['next_after'],after); after = page['next_after']
        self.assertEqual(count,10001)
        with self.assertRaises(CoreError): self.review.diff(dict(plan=plan.id, after=1))
        self.resolve(plan,take='incoming')
        with self.assertRaises(CoreError) as error: self.review.diff(dict(plan=plan.id,revision=1,after=1))
        self.assertEqual(error.exception.code,'INTEGRATION_PLAN_STALE')

    def test_one_huge_row_is_structured_error_without_destroying_plan(self):
        plan = self.make_binary_conflict()
        with self.catalog.connect(write=True) as db:
            db.execute('UPDATE integration_decisions SET reason=? WHERE plan_id=?', ('"'*150000, plan.id))
        with self.assertRaises(CoreError) as error: self.review.diff(dict(plan=plan.id))
        self.assertEqual(error.exception.code, 'RESPONSE_TOO_LARGE')
        self.assertIn('export',error.exception.next_step.lower())
        self.assertEqual(self.plans.load(plan.id).revision,1)

    def test_explicit_content_pages_reassemble_large_file_with_digest(self):
        data = b'\0bytes'*220000
        task, result, _ = self.ready_task({'large': data})
        plan = self.prepare_direct(task,result)
        from solace_core.integration_review import IntegrationReview
        self.review = IntegrationReview(self.service,self.catalog)
        offset = 0; pieces = []
        while True:
            page = self.review.diff(dict(plan=plan.id,revision=1,path='large',version='incoming',offset=offset))
            self.assertLessEqual(encoded_size(page),128*1024)
            piece = base64.b64decode(page['data']); self.assertLessEqual(len(piece),48*1024)
            pieces.append(piece); self.assertEqual(page['digest'],hashlib.sha256(data).hexdigest())
            if page['next_offset'] is None: break
            offset=page['next_offset']
        self.assertEqual(b''.join(pieces),data)
        for params in (dict(path='large'),dict(path='large',revision=1),dict(path='large',version='incoming',revision=1,after=0),dict(offset=0)):
            with self.assertRaises(CoreError): self.review.diff(dict(plan=plan.id,**params))

    def test_editing_export_does_not_change_candidate(self):
        plan = self.make_binary_conflict(); before = plan.candidate.manifest_sha256
        destination=self.base/'review'; result=self.export_direct(plan,destination)
        self.assertEqual(result['phase'],'published')
        for name, data in (('base',b'\0base'),('current',b'\0current'),('incoming',b'\0incoming'),('candidate',b'\0current')):
            self.assertEqual((destination/name/'data.bin').read_bytes(),data)
        (destination/'incoming'/'data.bin').write_bytes(b'external edit')
        self.assertEqual(self.plans.load(plan.id).candidate.manifest_sha256,before)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_refs WHERE owner_id=?',(self.export_operation,)).fetchone()[0],4)

    def test_export_refuses_owned_sync_and_symlink_destinations(self):
        plan = self.make_binary_conflict()
        for destination in (self.root/'export',self.catalog.store/'export',self.config.workspace_root/'export'):
            with self.assertRaises(CoreError): self.export_direct(plan,destination)
            self.assertFalse(destination.exists())
        real=self.base/'exports'; real.mkdir(); linked=self.base/'linked'; linked.symlink_to(real)
        with self.assertRaises(CoreError): self.export_direct(plan,linked/'export')

    def test_export_no_replace_race_preserves_competing_destination_and_intent(self):
        plan=self.make_binary_conflict(); destination=self.base/'review'
        from solace_core.workspace_trees import _rename_noreplace
        def race(source,dest,parent_fd,*args):
            destination.mkdir(); (destination/'competitor').write_text('keep')
            return _rename_noreplace(source,dest,parent_fd,*args)
        with patch('solace_core.integration_review._rename_noreplace',side_effect=race):
            with self.assertRaises(CoreError): self.export_direct(plan,destination)
        self.assertEqual((destination/'competitor').read_text(),'keep')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM integration_exports WHERE operation_id=?',(self.export_operation,)).fetchone()[0],'interrupted')
            self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?',(self.export_operation,)).fetchone()[0],2)

    def test_export_crash_before_and_after_rename_retains_inspectable_intent(self):
        plan=self.make_binary_conflict()
        from solace_core.workspace_trees import _rename_noreplace
        for after in (False,True):
            destination=self.base/('after' if after else 'before')
            def crash(*args):
                if after: _rename_noreplace(*args)
                raise SystemExit('simulated process death')
            with patch('solace_core.integration_review._rename_noreplace',side_effect=crash):
                with self.assertRaises(SystemExit): self.export_direct(plan,destination)
            with self.catalog.connect() as db:
                row=db.execute('SELECT * FROM integration_exports WHERE operation_id=?',(self.export_operation,)).fetchone()
                self.assertEqual(row['phase'],'staged')
                self.assertIsNotNone(row['identity_json'])
                self.assertGreaterEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?',(self.export_operation,)).fetchone()[0],2)
            self.assertEqual(destination.exists(),after)
            # Reconstruct the reviewer as restart would, and replay only the intent.
            from solace_core.integration_review import IntegrationReview
            replay=IntegrationReview(self.service,self.catalog).export(self.export_operation,self.export_request)
            self.assertEqual(replay['phase'],'staged')
            self.assertEqual(destination.exists(),after)
            staging=destination.parent/('.solace-export-'+self.export_operation)
            self.assertEqual(staging.exists(),not after)
            with self.assertRaises(CoreError): self.export_direct(plan,destination/'adopted')

    def test_resolution_keeps_prior_explicit_decisions_in_complete_revision(self):
        (self.root/'first').write_bytes(b'\0base'); (self.root/'second').write_bytes(b'\0base')
        task,result,_=self.ready_task({'first':b'\0incoming','second':b'\0incoming'})
        (self.root/'first').write_bytes(b'\0current'); (self.root/'second').write_bytes(b'\0current')
        plan=self.prepare_direct(task,result)
        from solace_core.integration_review import IntegrationReview
        self.review=IntegrationReview(self.service,self.catalog)
        self.resolve(plan,path='first',take='incoming')
        self.resolve(self.plans.load(plan.id),path='second',take='current')
        page=self.review.diff(dict(plan=plan.id))
        decisions={entry['path']:entry for entry in page['entries']}
        self.assertEqual(decisions['first']['resolution'],{'take':'incoming'})
        self.assertEqual(decisions['second']['resolution'],{'take':'current'})

    def test_review_token_matches_preparation_binding(self):
        plan=self.make_binary_conflict()
        from solace_core.integration_review import review_token
        with self.catalog.connect() as db:
            expected=json.loads(db.execute('SELECT expected_json FROM integration_revisions WHERE plan_id=?',(plan.id,)).fetchone()[0])
        self.assertEqual(review_token(plan,expected),self.token(plan.id,1))
        reversed_expected=dict(reversed(list(expected.items())))
        self.assertEqual(review_token(plan,reversed_expected),self.token(plan.id,1))
        expected['target']['head']='0'*32
        self.assertNotEqual(review_token(plan,expected),self.token(plan.id,1))

    def test_export_ancestor_move_does_not_record_success(self):
        plan=self.make_binary_conflict(); parent=self.base/'export-parent'; parent.mkdir()
        destination=parent/'review'; moved=self.base/'moved-parent'
        from solace_core.workspace_paths import directory_identity
        def move_after_identity(path,policy):
            value=directory_identity(path,policy)
            if Path(path)==destination:
                parent.rename(moved); parent.mkdir()
            return value
        with patch('solace_core.integration_review.directory_identity',side_effect=move_after_identity):
            with self.assertRaises(CoreError): self.export_direct(plan,destination)
        self.assertNotEqual(self.catalog.get_operation(self.export_operation)['state'],'succeeded')
        self.assertTrue((moved/'review'/'candidate'/'data.bin').exists())

    def test_export_respects_configured_sync_root(self):
        plan=self.make_binary_conflict(); synced=self.base/'sync'; synced.mkdir()
        from solace_core.workspace_paths import CoreConfig
        self.review.paths.config=CoreConfig(sync_roots=(synced,),workspace_root=self.config.workspace_root)
        with self.assertRaises(CoreError): self.export_direct(plan,synced/'review')
        self.assertFalse((synced/'review').exists())

    def test_export_and_content_stream_a_large_single_cas_object(self):
        data=b'\0large-object'*150000
        task,result,_=self.ready_task({'large':data})
        plan=self.prepare_direct(task,result)
        from solace_core.integration_review import IntegrationReview
        self.review=IntegrationReview(self.service,self.catalog)
        store=self.plans.snapshots
        digest=store.objects.put_bytes(data)
        # Coalesce the candidate's chunk representation without changing any
        # versioned metadata, exact bytes, manifest digest or review binding.
        with self.catalog.connect(write=True) as db:
            db.execute('DELETE FROM chunk_refs WHERE checkpoint_id=? AND path=?',(plan.candidate.checkpoint_id,'large'))
            db.execute('INSERT INTO chunk_refs VALUES (?,?,?,?)',(plan.candidate.checkpoint_id,'large',0,digest))
        with patch('solace_core.integration_snapshots.SnapshotStore.read_bytes',side_effect=AssertionError('whole-file buffering')):
            with patch('solace_engine.object_store.ObjectStore.read_object',side_effect=AssertionError('whole-object buffering')):
                page=self.review.diff(dict(plan=plan.id,revision=1,path='large',version='candidate'))
                self.assertEqual(base64.b64decode(page['data']),data[:48*1024])
                self.export_direct(plan,self.base/'streamed')
        self.assertEqual((self.base/'streamed'/'candidate'/'large').read_bytes(),data)
