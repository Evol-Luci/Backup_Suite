"""Immutable prepare, ancestry and atomic catalog publication acceptance."""
from dataclasses import replace
import json
import os
import shutil
import uuid
from unittest.mock import patch

from integration_fixture import IntegrationCase
from solace_core.errors import CoreError
from solace_core.lineage import Lineage


class PrepareTests(IntegrationCase):
    def seed(self):
        (self.root/'file').write_bytes(b'a\nb\nc\n')
        return self.ready_task({'file': b'A\nb\nc\n'})

    def test_prepare_merges_unsaved_destination_without_advancing_heads(self):
        task, result, root = self.seed()
        (self.root/'file').write_bytes(b'a\nb\nC\n')
        catalog = self.service.catalog(self.project)
        with catalog.connect() as db:
            heads = (Lineage(catalog).source_head(db), self.show(task)['workspace']['head_checkpoint'])
        plan = self.prepare_direct(task, result)
        self.assertEqual(plan.state, 'prepared')
        self.assertEqual(self.version_bytes(plan.current)['file'], b'a\nb\nC\n')
        self.assertEqual(self.version_bytes(plan.candidate)['file'], b'A\nb\nC\n')
        self.assertEqual((self.root/'file').read_bytes(), b'a\nb\nC\n')
        self.assertEqual((root/'file').read_bytes(), b'A\nb\nc\n')
        self.assertEqual(self.plans.load(plan.id), plan)
        with catalog.connect() as db:
            self.assertEqual(Lineage(catalog).source_head(db), heads[0])
            self.assertEqual(self.show(task)['workspace']['head_checkpoint'], heads[1])
            self.assertEqual(db.execute('SELECT parent_id FROM checkpoints WHERE id=?', (plan.current.checkpoint_id,)).fetchone()[0], heads[0])
            self.assertEqual(db.execute('SELECT parent_id FROM checkpoints WHERE id=?', (plan.candidate.checkpoint_id,)).fetchone()[0], plan.current.checkpoint_id)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_refs WHERE owner_id=?', (plan.id,)).fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_claims').fetchone()[0], 0)
            self.assertEqual([r[0] for r in db.execute('SELECT published FROM checkpoint_context WHERE checkpoint_id IN (?,?)', (plan.current.checkpoint_id, plan.candidate.checkpoint_id))], [0, 0])
            expected = json.loads(db.execute('SELECT expected_json FROM integration_revisions WHERE plan_id=?', (plan.id,)).fetchone()[0])
            self.assertEqual(expected['checks'], 'not_run_by_solace')
            self.assertEqual(expected['target']['head'], heads[0])
        self.assertEqual(catalog.get_operation(self.operation)['state'], 'succeeded')

    def test_child_targets_recorded_parent_and_pins_revision(self):
        (self.root/'file').write_bytes(b'a\nb\nc\n')
        parent = self.create_task(); target = self.start(parent)
        task, result, _ = self.ready_task({'file': b'A\nb\nc\n'}, parent=parent)
        from pathlib import Path
        (Path(target['path'])/'file').write_bytes(b'a\nb\nC\n')
        request = self.prepare_request(task, result)
        request['params']['expected_target_revision'] -= 1
        with self.assertRaises(CoreError) as error:
            self.prepare_direct(task, result, request=request)
        self.assertEqual(error.exception.code, 'TASK_REVISION_CONFLICT')
        plan = self.prepare_direct(task, result)
        self.assertEqual(plan.target.task_id, parent)
        self.assertEqual(plan.target.key, 'workspace:'+target['workspace_id'])
        self.assertEqual(self.version_bytes(plan.candidate)['file'], b'A\nb\nC\n')

    def test_missing_source_workspace_does_not_hide_immutable_result(self):
        task, result, root = self.seed(); shutil.rmtree(root)
        plan = self.prepare_direct(task, result)
        self.assertEqual(self.version_bytes(plan.incoming)['file'], b'A\nb\nc\n')

    def test_snapshot_open_does_not_require_registered_root(self):
        task, result, _ = self.seed()
        catalog = self.service.catalog(self.project)
        from solace_core.integration_snapshots import SnapshotStore
        profile = self.show(task)['profile']
        shutil.rmtree(self.root)
        store = SnapshotStore(catalog)
        version = store.version(result, profile)
        self.assertEqual(store.read_bytes(store.entries(version)[0]), b'A\nb\nc\n')
        with self.assertRaises(CoreError):
            store.version(result, dict(profile, exclusions=['different']))

    def test_profile_mismatch_refuses_before_capture(self):
        task, result, _ = self.seed()
        catalog = self.service.catalog(self.project)
        with catalog.connect(write=True) as db:
            db.execute('UPDATE checkpoint_context SET profile_json=? WHERE checkpoint_id=?', ('{}', result))
        with self.assertRaises(CoreError) as error:
            self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'INTEGRATION_PROFILE_MISMATCH')
        self.assertEqual(catalog.get_operation(self.operation)['state'], 'failed')

    def receipt(self, plan, *, result=None):
        """Stand in for Task 7's success transaction, using the real schema."""
        receipt = uuid.uuid4().hex
        with self.catalog.connect(write=True) as db:
            db.execute('INSERT INTO integration_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (receipt, plan.id, plan.revision, plan.task_id, result or plan.result_id, plan.target.key,
                 self.operation, plan.current.checkpoint_id, plan.candidate.checkpoint_id, '{}', self.catalog._now()))
        return receipt

    def test_same_result_receipt_replays_before_revision_or_availability(self):
        task, result, _ = self.seed(); plan = self.prepare_direct(task, result)
        receipt = self.receipt(plan)
        request = self.prepare_request(task, result)
        self.mutate('task.resume', task)
        shutil.rmtree(self.root)
        replay = self.prepare_direct(task, result, request=request)
        self.assertEqual(replay.id, plan.id)
        self.assertEqual(self.catalog.get_operation(self.operation)['result']['receipt_id'], receipt)

    def test_latest_contribution_is_baseline_and_divergence_is_refused(self):
        task, result, root = self.seed(); plan = self.prepare_direct(task, result)
        receipt = self.receipt(plan)
        self.mutate('task.resume', task)
        (root/'file').write_bytes(b'A\nB\nc\n')
        done = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        result2 = done['result']['checkpoint_id']
        plan2 = self.prepare_direct(task, result2)
        self.assertEqual(plan2.base.checkpoint_id, result)
        self.assertEqual(self.version_bytes(plan2.candidate)['file'], b'a\nB\nc\n')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT previous_receipt_id FROM integration_revisions WHERE plan_id=?', (plan2.id,)).fetchone()[0], receipt)
        # The latest contribution diverges, even though the older receipt matches.
        from solace_core.integration_snapshots import SnapshotStore
        store = SnapshotStore(self.catalog)
        with self.catalog.connect(write=True) as db:
            fork = store.insert(db, store.entries(plan.base), parent_id=plan.base.checkpoint_id,
                profile=json.loads(plan.base.profile_json), task_id=task, purpose='workspace')
        self.receipt(plan2, result=fork.checkpoint_id)
        from solace_core.integration_plans import IntegrationPlans
        with self.catalog.connect() as db, self.assertRaises(CoreError) as error:
            IntegrationPlans(self.service, self.catalog).baseline(db, task, result2, 'source')
        self.assertEqual(error.exception.code, 'INTEGRATION_ANCESTRY_DIVERGED')

    def test_not_descendant_of_start_is_refused(self):
        task, result, _ = self.seed()
        catalog = self.service.catalog(self.project)
        with catalog.connect(write=True) as db:
            db.execute('UPDATE checkpoints SET parent_id=NULL WHERE id=?', (result,))
        with self.assertRaises(CoreError) as error:
            self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'INTEGRATION_ANCESTRY_DIVERGED')

    def test_root_refuses_unapplied_ready_child(self):
        parent = self.create_task(); self.start(parent)
        child, child_result, _ = self.ready_task({'child': b'change'}, parent=parent)
        done = self.wait_operation(self.mutate('task.ready', parent)['operation_id'])
        with self.assertRaises(CoreError) as error:
            self.prepare_direct(parent, done['result']['checkpoint_id'])
        self.assertEqual(error.exception.code, 'TASK_CHILDREN_PENDING')

    def test_failure_between_current_and_plan_rolls_back_all_snapshots(self):
        task, result, _ = self.seed()
        catalog = self.service.catalog(self.project)
        with catalog.connect() as db:
            before = db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]
        from solace_core.integration_snapshots import SnapshotStore
        original = SnapshotStore.insert
        calls = []
        def fail_after_current(store, db, *args, **kwargs):
            value = original(store, db, *args, **kwargs); calls.append(value)
            if len(calls) == 2: raise RuntimeError('injected publication failure')
            return value
        with patch.object(SnapshotStore, 'insert', fail_after_current), self.assertRaises(CoreError):
            self.prepare_direct(task, result)
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_plans').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM task_claims').fetchone()[0], 0)
        self.assertEqual(catalog.get_operation(self.operation)['state'], 'failed')

    def test_warning_prevents_incomplete_candidate_and_protected_paths_survive(self):
        task, result, _ = self.seed()
        (self.root/'.git').mkdir(); (self.root/'.git'/'private').write_bytes(b'git')
        os.mkfifo(self.root/'pipe')
        with self.assertRaises(CoreError) as error:
            self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'INTEGRATION_CAPTURE_INCOMPLETE')
        (self.root/'pipe').unlink()
        plan = self.prepare_direct(task, result)
        self.assertNotIn('.git/private', self.version_bytes(plan.current))
        self.assertEqual((self.root/'.git'/'private').read_bytes(), b'git')

    def test_snapshot_rejects_corrupt_whole_file_and_unsafe_link(self):
        task, result, _ = self.seed(); plan = self.prepare_direct(task, result)
        from solace_core.integration_snapshots import SnapshotStore
        store = SnapshotStore(self.catalog)
        entry = store.entries(plan.incoming)[0]
        with self.assertRaises(CoreError): store.read_bytes(replace(entry, size=entry.size+1))
        link = replace(store.write_bytes('link', b'../escape', 0o777), kind='symlink')
        with self.catalog.connect(write=True) as db, self.assertRaises(CoreError):
            store.insert(db, (link,), parent_id=result, profile=json.loads(plan.incoming.profile_json), task_id=task, purpose='workspace')

    def test_abandon_retains_draft_refs_and_replays_request(self):
        task, result, _ = self.seed(); plan = self.prepare_direct(task, result)
        request = self.request('integration.abandon', plan=plan.id, revision=1)
        abandoned = self.plans.abandon(request)
        self.assertEqual(abandoned['state'], 'abandoned')
        self.assertEqual(self.plans.abandon(request), abandoned)
        self.assertEqual(self.plans.load(plan.id).state, 'abandoned')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_refs WHERE owner_id=?', (plan.id,)).fetchone()[0], 4)

    def test_replacement_inventory_records_deleted_versioned_files(self):
        (self.root/'folder').mkdir(); (self.root/'folder'/'old').write_bytes(b'old')
        task = self.create_task(); started = self.start(task)
        from pathlib import Path
        workspace = Path(started['path'])
        (workspace/'folder'/'old').unlink(); (workspace/'folder').rmdir()
        (workspace/'folder').write_bytes(b'replacement')
        result = self.wait_operation(self.mutate('task.ready', task)['operation_id'])['result']['checkpoint_id']
        plan = self.prepare_direct(task, result)
        self.assertEqual(self.version_bytes(plan.candidate), {'folder': b'replacement'})
        with self.catalog.connect() as db:
            expected = json.loads(db.execute('SELECT expected_json FROM integration_revisions WHERE plan_id=?', (plan.id,)).fetchone()[0])
            self.assertEqual(expected['directory_inventories']['folder']['entries'][0]['path'], 'old')

    def test_empty_or_protected_descendants_prevent_directory_replacement(self):
        for descendant in ('empty', '.git'):
            with self.subTest(descendant=descendant):
                if (self.root/'folder').exists(): shutil.rmtree(self.root/'folder')
                (self.root/'folder').mkdir(); (self.root/'folder'/'old').write_bytes(b'old')
                task = self.create_task(); started = self.start(task)
                from pathlib import Path
                folder = Path(started['path'])/'folder'
                shutil.rmtree(folder); folder.write_bytes(b'replacement')
                result = self.wait_operation(self.mutate('task.ready', task)['operation_id'])['result']['checkpoint_id']
                (self.root/'folder'/descendant).mkdir()
                with self.assertRaises(CoreError) as error:
                    self.prepare_direct(task, result)
                self.assertEqual(error.exception.code, 'INTEGRATION_DIRECTORY_NOT_EMPTY')
                self.assertTrue((self.root/'folder'/descendant).is_dir())

    def test_newly_owned_descendant_prevents_incoming_ancestor_creation(self):
        task, result, _ = self.ready_task({'future': b'file'})
        from solace_core.integration_plans import IntegrationPlans
        original = IntegrationPlans._boundaries
        def roots(plans):
            return sorted(original(plans)+[str(self.root/'future'/'reserved-journal')])
        with patch.object(IntegrationPlans, '_boundaries', roots), self.assertRaises(CoreError) as error:
            self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'INTEGRATION_PROTECTED_PATH')
        self.assertFalse((self.root/'future').exists())

    def test_corrupted_chunk_fails_immutable_read(self):
        task, result, _ = self.seed()
        from solace_core.integration_snapshots import SnapshotStore
        catalog = self.service.catalog(self.project); store = SnapshotStore(catalog)
        version = store.version(result, self.show(task)['profile'])
        entry = store.entries(version)[0]
        (catalog.store/'objects'/entry.chunk_digests[0][:2]/entry.chunk_digests[0]).write_bytes(b'corrupt')
        with self.assertRaises(CoreError) as error: store.read_bytes(entry)
        self.assertEqual(error.exception.code, 'CORRUPT_OBJECT')

    def test_conflicts_and_sorted_decisions_are_durable(self):
        task, result, _ = self.seed()
        (self.root/'file').write_bytes(b'other\nb\nc\n')
        plan = self.prepare_direct(task, result)
        self.assertEqual(plan.state, 'conflicted')
        self.assertEqual(plan.conflicts[0].reason, 'overlapping_text')
        self.assertEqual(self.plans.load(plan.id), plan)
        self.assertEqual(self.version_bytes(plan.candidate)['file'], b'other\nb\nc\n')
        with self.catalog.connect() as db:
            rows = db.execute('SELECT path,kind FROM integration_decisions WHERE plan_id=? ORDER BY ordinal', (plan.id,)).fetchall()
            self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))
            self.assertEqual(rows[0][1], 'conflict')

    def test_baseline_profile_mismatch_is_not_reinterpreted(self):
        task, result, _ = self.seed()
        catalog = self.service.catalog(self.project)
        base = self.show(task)['workspace']['base_checkpoint']
        with catalog.connect(write=True) as db:
            db.execute('UPDATE checkpoint_context SET profile_json=? WHERE checkpoint_id=?', ('{}', base))
        with self.assertRaises(CoreError) as error: self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'INTEGRATION_PROFILE_MISMATCH')

    def test_result_resumed_or_superseded_is_not_eligible(self):
        task, result, root = self.seed()
        self.mutate('task.resume', task)
        with self.assertRaises(CoreError) as error: self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'TASK_STATE_INVALID')
        (root/'file').write_bytes(b'next')
        self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        with self.assertRaises(CoreError) as error: self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'TASK_STATE_INVALID')

    def reserve_journal(self, plan, *, action_phase=None, journal_phase='reserved'):
        from dataclasses import asdict
        operation = self.catalog.accept_operation(self.request('integration.apply', plan=plan.id, revision=1))['operation_id']
        journal_path = self.root.parent/('journal-'+operation)
        with self.catalog.connect(write=True) as db:
            db.execute('INSERT INTO integration_journals VALUES (?,?,?,?,?,?,?,?)',
                (operation, plan.id, 1, str(journal_path), None, json.dumps(asdict(plan.target.identity)), '{}', journal_phase))
            db.execute('INSERT INTO integration_claims VALUES (?,?,?,?,?)',
                (str(self.root), operation, 'target', None, self.catalog._now()))
            if action_phase:
                db.execute('INSERT INTO integration_actions VALUES (?,?,?,?,?,?,?,?,?)',
                    (operation, 0, 'file', 'move_old', action_phase, None, None, 'old-0', None))
        return operation, journal_path

    def test_abandon_retains_safe_journal_reservation(self):
        task, result, _ = self.seed(); plan = self.prepare_direct(task, result)
        operation, journal = self.reserve_journal(plan)
        self.plans.abandon(self.request('integration.abandon', plan=plan.id, revision=1))
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM integration_journals WHERE operation_id=?', (operation,)).fetchone()[0], 'abandoned')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims').fetchone()[0], 0)
        self.assertFalse(journal.exists())

    def test_abandon_cannot_release_claim_after_action_intent(self):
        task, result, _ = self.seed(); plan = self.prepare_direct(task, result)
        operation, _ = self.reserve_journal(plan, action_phase='intent')
        with self.assertRaises(CoreError) as error:
            self.plans.abandon(self.request('integration.abandon', plan=plan.id, revision=1))
        self.assertEqual(error.exception.code, 'RECOVERY_REQUIRED')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?', (operation,)).fetchone()[0], 1)

    def test_incomplete_ready_result_is_refused_even_if_destination_complete(self):
        task = self.create_task(); started = self.start(task)
        from pathlib import Path
        os.mkfifo(Path(started['path'])/'unsupported')
        result = self.wait_operation(self.mutate('task.ready', task)['operation_id'])['result']['checkpoint_id']
        with self.assertRaises(CoreError) as error: self.prepare_direct(task, result)
        self.assertEqual(error.exception.code, 'INTEGRATION_CAPTURE_INCOMPLETE')

    def test_failure_record_unavailable_preserves_short_claim(self):
        task, result, _ = self.seed()
        os.mkfifo(self.root/'unsupported')
        catalog = self.service.catalog(self.project)
        transition = catalog._transition
        def fail_record(db, operation, state, *args, **kwargs):
            if state == 'failed': raise OSError('failure receipt unavailable')
            return transition(db, operation, state, *args, **kwargs)
        with patch.object(catalog, '_transition', fail_record), self.assertRaises(OSError):
            self.prepare_direct(task, result)
        with catalog.connect() as db:
            self.assertEqual(db.execute('SELECT operation_id FROM task_claims WHERE task_id=?', (task,)).fetchone()[0], self.operation)
            self.assertEqual(db.execute('SELECT state FROM operations WHERE id=?', (self.operation,)).fetchone()[0], 'running')

    def test_capture_parent_is_stored_source_head_not_global_latest_task_result(self):
        (self.root/'file').write_bytes(b'a\nb\nc\n')
        source_head = self.wait_operation(self.call('checkpoint.create', project=self.project, message='source head')['operation_id'])['result']['checkpoint_id']
        task, result, _ = self.ready_task({'file': b'A\nb\nc\n'})
        other, other_result, _ = self.ready_task({'other': b'later'})
        plan = self.prepare_direct(task, result)
        self.assertEqual(plan.target.head, source_head)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT parent_id FROM checkpoints WHERE id=?', (plan.current.checkpoint_id,)).fetchone()[0], source_head)
            self.assertEqual(Lineage(self.catalog).source_head(db), source_head)
        self.assertNotIn(plan.target.head, (result, other_result))

    def test_owned_path_remains_protected_when_replaced_by_symlink(self):
        task, result, _ = self.ready_task({'ordinary': b'new'})
        (self.root/'owned').symlink_to('ordinary')
        from solace_core.integration_plans import IntegrationPlans
        original = IntegrationPlans._boundaries
        def roots(plans):
            return sorted(original(plans)+[str(self.root/'owned')])
        with patch.object(IntegrationPlans, '_boundaries', roots):
            plan = self.prepare_direct(task, result)
        self.assertNotIn('owned', self.version_bytes(plan.current))
        self.assertEqual(self.version_bytes(plan.candidate), {'ordinary': b'new'})
        self.assertEqual(os.readlink(self.root/'owned'), 'ordinary')

    def test_file_directory_prefix_conflict_prepares_without_traversing_file(self):
        task, result, _ = self.ready_task({'folder/child': b'incoming'})
        (self.root/'folder').write_bytes(b'current')
        plan = self.prepare_direct(task, result)
        self.assertEqual(plan.state, 'conflicted')
        self.assertTrue(all(conflict.reason == 'path_prefix' for conflict in plan.conflicts))
        self.assertEqual(self.version_bytes(plan.candidate), {'folder': b'current'})
        self.assertEqual(self.plans.load(plan.id), plan)

    def test_snapshot_verification_streams_large_objects_and_checks_digest(self):
        task, result, _ = self.seed()
        plan = self.prepare_direct(task, result)
        store = self.plans.snapshots
        data = b'\0large-object' * 150000
        entry = store.write_bytes('large', data, 0o644)
        with patch.object(store, 'read_bytes', side_effect=AssertionError('whole-file buffering')):
            with patch.object(store.objects, 'read_object', side_effect=AssertionError('whole-object buffering')):
                blocks = list(store.iter_bytes(entry))
                self.assertTrue(all(len(block) <= 128*1024 for block in blocks))
                self.assertEqual(b''.join(blocks), data)
                with self.catalog.connect(write=True) as db:
                    version = store.insert(db, (entry,), parent_id=None,
                        profile=json.loads(plan.candidate.profile_json),
                        task_id=task, purpose='source')
                self.assertEqual(store.entries(version), (entry,))
        object_path = store.objects._object_path(entry.chunk_digests[0])
        with object_path.open('r+b') as stream:
            stream.seek(-1, os.SEEK_END); stream.write(b'!')
        with self.assertRaises(CoreError) as error:
            list(store.iter_bytes(entry))
        self.assertEqual(error.exception.code, 'CORRUPT_OBJECT')
