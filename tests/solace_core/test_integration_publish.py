"""Publication preserves roots, competing entries and durable displaced versions."""
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from unittest.mock import patch

from integration_fixture import IntegrationCase
from solace_core.errors import CoreError
from solace_core.integration_claims import IntegrationClaims
from solace_core.integration_review import IntegrationReview
from solace_core.workspace_trees import tree_directory


class PublishTests(IntegrationCase):
    def plan_with(self, edit, **params):
        task = self.create_task(**params)
        workspace = Path(self.start(task)['path'])
        edit(workspace)
        done = self.wait_operation(self.mutate('task.ready', task)['operation_id'])
        self.assertEqual(done['state'], 'succeeded', done)
        return self.prepare_direct(task, done['result']['checkpoint_id'])

    def reserve(self, plan):
        claims = IntegrationClaims(self.service, self.catalog)
        shown = IntegrationReview(self.service, self.catalog).show(plan.id)
        approved = claims.approve(self.request('integration.approve', plan=plan.id,
            revision=plan.revision, token=shown['review_token'], accept_unchecked=True))
        request = self.request('integration.apply', plan=plan.id, revision=plan.revision,
                               approval=approved['approval'], writers_paused=True)
        operation = self.catalog.accept_operation(request)['operation_id']
        self.catalog.transition(operation, 'running')
        claims.validate(plan, scan=True)
        with self.registry.lock, self.service.coordinator(self.project).lock, self.catalog.connect(write=True) as db:
            journal = claims.reserve(db, operation, plan)
            approval = dict(db.execute('SELECT * FROM integration_approvals WHERE token_sha256=?',
                                      (hashlib.sha256(approved['approval'].encode()).hexdigest(),)).fetchone())
            intent = json.loads(db.execute('SELECT intent_json FROM integration_journals WHERE operation_id=?', (operation,)).fetchone()[0])
            intent['approval'] = dict(id=approval['id'], token_sha256=approval['token_sha256'], binding=json.loads(approval['binding_json']))
            db.execute('UPDATE integration_journals SET intent_json=? WHERE operation_id=?', (json.dumps(intent), operation))
        from solace_core.integration_publish import IntegrationPublisher
        self.publisher = IntegrationPublisher(self.service, self.catalog)
        return journal

    def staged_replacement(self, name='file'):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'original')
        plan = self.plan_with(lambda root: (root/name).write_bytes(b'incoming'))
        reserved = self.reserve(plan)
        return plan, self.publisher.stage(reserved.operation_id, plan)

    def actions(self, journal):
        with self.catalog.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM integration_actions WHERE operation_id=? ORDER BY ordinal', (journal.operation_id,))]

    def displaced_version_exists(self, journal, data):
        with tree_directory(journal.path/'old') as fd:
            for action in self.actions(journal):
                if action['kind'] != 'move_old': continue
                try: entry = os.open(action['slot'], os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=fd)
                except FileNotFoundError: continue
                try:
                    self.assertTrue(stat.S_ISREG(os.fstat(entry).st_mode))
                    actual = b''
                    while block := os.read(entry, 65536): actual += block
                    if hashlib.sha256(actual).digest() == hashlib.sha256(data).digest(): return True
                finally: os.close(entry)
        return False

    def assert_retained(self, journal):
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase FROM integration_journals WHERE operation_id=?', (journal.operation_id,)).fetchone()[0], 'recovery_required')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?', (journal.operation_id,)).fetchone()[0], 2)

    def test_mixed_publication_preserves_root_unchanged_and_protected_entries(self):
        for name, data in {'file': b'original', 'delete': b'delete me', 'mode': b'mode', 'keep': b'keep'}.items():
            (self.root/name).write_bytes(data)
        (self.root/'link').symlink_to('keep')
        for name in ('.git', '.solace_vault', 'empty', 'nested/.git', 'excluded'):
            (self.root/name).mkdir(parents=True)
        (self.root/'.git/config').write_bytes(b'git')
        (self.root/'.solace_vault/private').write_bytes(b'vault')
        (self.root/'nested/.git/private').write_bytes(b'nested git')
        (self.root/'excluded/private').write_bytes(b'excluded')
        before = {name: (self.root/name).lstat().st_ino for name in ('keep', 'empty', '.git', '.solace_vault', 'excluded/private')}
        root_inode = self.root.stat().st_ino
        def edit(root):
            (root/'file').write_bytes(b'incoming')
            (root/'delete').unlink()
            (root/'mode').chmod(0o700)
            (root/'new').mkdir(); (root/'new/added').write_bytes(b'added')
            (root/'link').unlink(); (root/'link').symlink_to('file')
        plan = self.plan_with(edit, exclusions=['excluded']); reserved = self.reserve(plan)
        journal = self.publisher.stage(reserved.operation_id, plan)
        self.assertIsNotNone(journal.identity)
        self.assertEqual(stat.S_IMODE(journal.path.stat().st_mode), 0o700)
        self.publisher.publish(journal); self.publisher.verify_final(journal)
        self.assertEqual(self.root.stat().st_ino, root_inode)
        self.assertEqual({name: (self.root/name).lstat().st_ino for name in before}, before)
        self.assertEqual((self.root/'file').read_bytes(), b'incoming')
        self.assertFalse((self.root/'delete').exists())
        self.assertEqual((self.root/'new/added').read_bytes(), b'added')
        self.assertEqual(stat.S_IMODE((self.root/'new').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root/'mode').stat().st_mode), 0o700)
        self.assertEqual(os.readlink(self.root/'link'), 'file')
        self.assertEqual((self.root/'.git/config').read_bytes(), b'git')
        self.assertEqual((self.root/'.solace_vault/private').read_bytes(), b'vault')
        self.assertEqual((self.root/'nested/.git/private').read_bytes(), b'nested git')
        self.assertEqual((self.root/'excluded/private').read_bytes(), b'excluded')
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        self.assertTrue(self.displaced_version_exists(journal, b'delete me'))
        self.assertTrue(all(row['phase'] == 'done' for row in self.actions(journal)))
        with tree_directory(journal.path) as fd:
            descriptor = os.open('intent.json', os.O_RDONLY|os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(descriptor) as stream: intent = json.load(stream)
        self.assertEqual(intent['approval']['binding']['candidate']['manifest_sha256'], plan.candidate.manifest_sha256)
        self.assertEqual(set(intent['approval']), {'id', 'token_sha256', 'binding'})

    def test_foreign_entry_is_not_overwritten(self):
        _, journal = self.staged_replacement()
        def interleave(name):
            if name == 'old_moved:0': (self.root/'file').write_bytes(b'foreign winner')
        self.publisher.failpoint = interleave
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'file').read_bytes(), b'foreign winner')
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        self.assert_retained(journal)

    def test_equal_byte_foreign_inode_is_not_displaced(self):
        _, journal = self.staged_replacement()
        (self.root/'file').rename(self.root/'saved')
        (self.root/'file').write_bytes(b'original')
        winner = (self.root/'file').stat().st_ino
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'file').stat().st_ino, winner)
        self.assert_retained(journal)

    def test_hardlinked_old_file_does_not_mutate_other_link(self):
        _, journal = self.staged_replacement()
        os.link(self.root/'file', self.base/'other-link')
        self.publisher.publish(journal); self.publisher.verify_final(journal)
        self.assertEqual((self.base/'other-link').read_bytes(), b'original')
        self.assertEqual((self.root/'file').read_bytes(), b'incoming')

    def test_substituted_ancestor_does_not_write_through_symlink(self):
        _, journal = self.staged_replacement('dir/file')
        (self.root/'dir').rename(self.root/'saved-dir')
        (self.root/'dir').symlink_to('saved-dir', target_is_directory=True)
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'saved-dir/file').read_bytes(), b'original')
        self.assert_retained(journal)

    def test_substituted_journal_slot_directory_is_refused(self):
        _, journal = self.staged_replacement()
        (journal.path/'old').rename(journal.path/'saved-old')
        (journal.path/'old').mkdir()
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assert_retained(journal)

    def test_displaced_content_changed_at_move_is_preserved_and_refused(self):
        _, journal = self.staged_replacement()
        def interleave(name):
            if name == 'old_moved:0':
                with tree_directory(journal.path/'old') as fd:
                    slot = self.actions(journal)[0]['slot']
                    file = os.open(slot, os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW, dir_fd=fd)
                    try: os.write(file, b'changed during move')
                    finally: os.close(file)
        self.publisher.failpoint = interleave
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertFalse((self.root/'file').exists())
        self.assertTrue(self.displaced_version_exists(journal, b'changed during move'))
        self.assert_retained(journal)

    def test_directory_to_file_and_file_to_directory(self):
        (self.root/'directory').mkdir(); (self.root/'directory/child').write_bytes(b'old child')
        (self.root/'leaf').write_bytes(b'old leaf')
        def edit(root):
            (root/'directory/child').unlink(); (root/'directory').rmdir()
            (root/'directory').write_bytes(b'new leaf')
            (root/'leaf').unlink(); (root/'leaf').mkdir(); (root/'leaf/child').write_bytes(b'new child')
        plan = self.plan_with(edit); reserved = self.reserve(plan)
        journal = self.publisher.stage(reserved.operation_id, plan)
        self.publisher.publish(journal); self.publisher.verify_final(journal)
        self.assertEqual((self.root/'directory').read_bytes(), b'new leaf')
        self.assertEqual((self.root/'leaf/child').read_bytes(), b'new child')
        self.assertTrue(self.displaced_version_exists(journal, b'old child'))
        self.assertTrue(self.displaced_version_exists(journal, b'old leaf'))

    def test_target_parent_becomes_read_only_before_stage(self):
        (self.root/'file').write_bytes(b'original')
        plan = self.plan_with(lambda root: (root/'file').write_bytes(b'incoming'))
        reserved = self.reserve(plan)
        self.base.chmod(0o500)
        try:
            with self.assertRaises(CoreError): self.publisher.stage(reserved.operation_id, plan)
            self.assertFalse(reserved.path.exists())
            self.assertEqual((self.root/'file').read_bytes(), b'original')
        finally: self.base.chmod(0o700)
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase,identity_json FROM integration_journals WHERE operation_id=?', (reserved.operation_id,)).fetchone()[:], ('reserved', None))

    def test_failed_receiving_directory_sync_keeps_intent_and_old_bytes(self):
        _, journal = self.staged_replacement()
        original = os.fsync
        old = (journal.path/'old').stat()
        def fail(fd):
            if os.path.samestat(os.fstat(fd), old): raise OSError(errno.EIO, 'injected directory sync failure')
            return original(fd)
        with patch('os.fsync', side_effect=fail):
            with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        self.assertEqual(self.actions(journal)[0]['phase'], 'intent')
        self.assert_retained(journal)

    def test_final_verification_catches_edit_at_verification_boundary(self):
        _, journal = self.staged_replacement()
        self.publisher.publish(journal)
        self.publisher.failpoint = lambda name: (self.root/'file').write_bytes(b'edited!!') if name == 'files_verified' else None
        with self.assertRaises(CoreError): self.publisher.verify_final(journal)
        self.assertEqual((self.root/'file').read_bytes(), b'edited!!')
        self.assert_retained(journal)

    def test_sql_action_substitution_cannot_change_approved_path(self):
        _, journal = self.staged_replacement()
        (self.root/'foreign').write_bytes(b'original')
        with self.catalog.connect(write=True) as db:
            row = db.execute('SELECT old_json FROM integration_actions WHERE operation_id=? AND ordinal=0', (journal.operation_id,)).fetchone()
            value = json.loads(row[0]); value['name'] = 'foreign'
            meta = (self.root/'foreign').stat()
            value['identity'] = dict(device=meta.st_dev, inode=meta.st_ino, mode=meta.st_mode)
            db.execute('UPDATE integration_actions SET old_json=? WHERE operation_id=? AND ordinal=0', (json.dumps(value), journal.operation_id))
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'foreign').read_bytes(), b'original')
        self.assertEqual((self.root/'file').read_bytes(), b'original')

    def test_ambiguous_mkdir_crash_retains_directory_intent_and_claims(self):
        plan = self.plan_with(lambda root: ((root/'new').mkdir(), (root/'new/file').write_bytes(b'incoming')))
        reserved = self.reserve(plan); journal = self.publisher.stage(reserved.operation_id, plan)
        def crash(name):
            if name == 'dir_changed:0': raise SystemExit('simulated process interruption')
        self.publisher.failpoint = crash
        with self.assertRaises(SystemExit): self.publisher.publish(journal)
        self.assertTrue((self.root/'new').is_dir())
        self.assertFalse((self.root/'new/file').exists())
        action = self.actions(journal)[0]
        self.assertEqual(action['phase'], 'intent'); self.assertIsNone(action['observed_json'])
        self.assertIsNone(json.loads(action['new_json'])['identity'])
        from solace_core.service import CoreService
        from solace_core.integration_publish import IntegrationPublisher
        self.service.close()
        self.service = CoreService(self.registry, config=self.config); self.addCleanup(self.service.close)
        self.catalog = self.service.catalog(self.project)
        self.publisher = IntegrationPublisher(self.service, self.catalog)
        self.assert_retained(journal)
        self.publisher.failpoint = None
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertTrue((self.root/'new').is_dir())

    def test_journal_root_substitution_is_not_followed(self):
        _, journal = self.staged_replacement()
        saved = journal.path.with_name(journal.path.name+'-saved')
        journal.path.rename(saved); journal.path.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assertTrue((saved/'new').is_dir())
        self.assert_retained(journal)

    def test_leaf_substitution_after_intent_is_not_moved(self):
        _, journal = self.staged_replacement()
        def interleave(name):
            if name == 'intent:0':
                (self.root/'file').rename(self.base/'saved-file')
                (self.root/'file').symlink_to('../saved-file')
        self.publisher.failpoint = interleave
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertTrue((self.root/'file').is_symlink())
        self.assertEqual((self.base/'saved-file').read_bytes(), b'original')
        self.assert_retained(journal)

    def test_ancestor_substitution_after_intent_is_not_followed(self):
        _, journal = self.staged_replacement('dir/file')
        def interleave(name):
            if name == 'intent:0':
                (self.root/'dir').rename(self.base/'saved-directory')
                (self.root/'dir').symlink_to('../saved-directory', target_is_directory=True)
        self.publisher.failpoint = interleave
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.base/'saved-directory/file').read_bytes(), b'original')
        self.assert_retained(journal)

    def test_stage_exhausts_provisional_snapshot_stream_before_publication(self):
        (self.root/'file').write_bytes(b'original')
        plan = self.plan_with(lambda root: (root/'file').write_bytes(b'incoming'))
        reserved = self.reserve(plan); original = self.publisher.snapshots.iter_bytes
        def corrupted(entry):
            for block in original(entry): yield block
            if (reserved.path/'new').is_dir():
                raise CoreError('CORRUPT_OBJECT', 'Injected final stream digest mismatch.', 'Preserve evidence.')
        with patch.object(self.publisher.snapshots, 'iter_bytes', corrupted):
            with self.assertRaises(CoreError): self.publisher.stage(reserved.operation_id, plan)
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assert_retained(reserved)

    def test_new_slot_equal_byte_inode_substitution_is_refused(self):
        _, journal = self.staged_replacement()
        action = next(row for row in self.actions(journal) if row['kind'] == 'publish_new')
        slot = journal.path/'new'/action['slot']
        data = slot.read_bytes(); slot.rename(journal.path/'new/saved'); slot.write_bytes(data)
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        self.assertFalse((self.root/'file').exists())
        self.assert_retained(journal)

    def _rename_error(self, number):
        _, journal = self.staged_replacement()
        with patch('solace_core.integration_publish._rename_noreplace', side_effect=OSError(number, 'injected rename failure')):
            with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assertEqual(self.actions(journal)[0]['phase'], 'intent')
        self.assert_retained(journal)

    def test_cross_device_rename_is_refused(self): self._rename_error(errno.EXDEV)
    def test_permission_denied_rename_is_refused(self): self._rename_error(errno.EACCES)
    def test_unsupported_rename_is_refused(self): self._rename_error(errno.EOPNOTSUPP)
    def test_enospc_rename_is_refused(self): self._rename_error(errno.ENOSPC)

    def _sync_failure(self, location, phase):
        _, journal = self.staged_replacement()
        wanted = (self.root if location == 'target' else journal.path/location if location else journal.path).stat()
        original = os.fsync
        def fail(fd):
            if os.path.samestat(os.fstat(fd), wanted): raise OSError(errno.EIO, 'injected sync failure')
            return original(fd)
        with patch('os.fsync', side_effect=fail):
            with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual(self.actions(journal)[phase]['phase'], 'intent' if location else 'planned')
        self.assert_retained(journal)
        if location: self.assertTrue(self.displaced_version_exists(journal, b'original'))
        else: self.assertEqual((self.root/'file').read_bytes(), b'original')

    def test_target_removing_directory_sync_failure(self): self._sync_failure('target', 0)
    def test_new_slot_removing_directory_sync_failure(self): self._sync_failure('new', 1)
    def test_journal_intent_directory_sync_failure(self): self._sync_failure('', 0)

    def test_target_receiving_directory_sync_failure_after_publish(self):
        plan = self.plan_with(lambda root: (root/'added').write_bytes(b'incoming'))
        reserved = self.reserve(plan); journal = self.publisher.stage(reserved.operation_id, plan)
        original = os.fsync; target = self.root.stat()
        def fail(fd):
            if os.path.samestat(os.fstat(fd), target): raise OSError(errno.EIO, 'injected receiving sync failure')
            return original(fd)
        with patch('os.fsync', side_effect=fail):
            with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'added').read_bytes(), b'incoming')
        self.assertEqual(self.actions(journal)[0]['phase'], 'intent')
        self.assert_retained(journal)

    def test_journal_staging_directory_sync_failure_retains_reservation(self):
        (self.root/'file').write_bytes(b'original')
        plan = self.plan_with(lambda root: (root/'file').write_bytes(b'incoming'))
        reserved = self.reserve(plan); original = os.fsync
        def fail(fd):
            if os.readlink('/proc/self/fd/'+str(fd)) == str(reserved.path/'new'):
                raise OSError(errno.EIO, 'injected staging sync failure')
            return original(fd)
        with patch('os.fsync', side_effect=fail):
            with self.assertRaises(CoreError): self.publisher.stage(reserved.operation_id, plan)
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assert_retained(reserved)

    def test_journal_parent_sync_failure_retains_unidentified_created_directory(self):
        (self.root/'file').write_bytes(b'original')
        plan = self.plan_with(lambda root: (root/'file').write_bytes(b'incoming'))
        reserved = self.reserve(plan); original = os.fsync; parent = self.root.parent.stat()
        def fail(fd):
            if os.path.samestat(os.fstat(fd), parent): raise OSError(errno.EIO, 'injected sibling parent sync failure')
            return original(fd)
        with patch('os.fsync', side_effect=fail):
            with self.assertRaises(CoreError): self.publisher.stage(reserved.operation_id, plan)
        self.assertTrue(reserved.path.is_dir())
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        with self.catalog.connect() as db:
            self.assertEqual(db.execute('SELECT phase,identity_json FROM integration_journals WHERE operation_id=?', (reserved.operation_id,)).fetchone()[:], ('reserved', None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM integration_claims WHERE operation_id=?', (reserved.operation_id,)).fetchone()[0], 2)

    def test_target_parent_permission_change_stops_before_displacement(self):
        _, journal = self.staged_replacement('dir/file')
        (self.root/'dir').chmod(0o500)
        try:
            with self.assertRaises(CoreError): self.publisher.publish(journal)
            self.assertEqual((self.root/'dir/file').read_bytes(), b'original')
            self.assert_retained(journal)
        finally: (self.root/'dir').chmod(0o700)

    def test_mount_root_placement_change_is_refused_before_journal_creation(self):
        (self.root/'file').write_bytes(b'original')
        plan = self.plan_with(lambda root: (root/'file').write_bytes(b'incoming'))
        reserved = self.reserve(plan)
        self.policy.mountinfo += f'2 1 0:1 / {self.root} rw - ext4 x rw\n'
        with self.assertRaises(CoreError): self.publisher.stage(reserved.operation_id, plan)
        self.assertFalse(reserved.path.exists())
        self.assertEqual((self.root/'file').read_bytes(), b'original')

    def test_extra_directory_child_blocks_empty_rmdir(self):
        (self.root/'dir').mkdir(); (self.root/'dir/child').write_bytes(b'original')
        def edit(root):
            (root/'dir/child').unlink(); (root/'dir').rmdir(); (root/'dir').write_bytes(b'incoming')
        plan = self.plan_with(edit); reserved = self.reserve(plan)
        journal = self.publisher.stage(reserved.operation_id, plan)
        (self.root/'dir/foreign').write_bytes(b'foreign')
        with self.assertRaises(CoreError): self.publisher.publish(journal)
        self.assertEqual((self.root/'dir/foreign').read_bytes(), b'foreign')
        self.assertTrue(self.displaced_version_exists(journal, b'original'))
        self.assert_retained(journal)

    def _blocked_directory(self, child):
        (self.root/'dir').mkdir(); (self.root/'dir/child').write_bytes(b'original')
        (self.root/'dir'/child).mkdir()
        def edit(root):
            (root/'dir/child').unlink()
            # Empty directories are not captured into task workspaces.
            (root/'dir').rmdir(); (root/'dir').write_bytes(b'incoming')
        with self.assertRaises(CoreError): self.plan_with(edit)
        self.assertTrue((self.root/'dir'/child).is_dir())
        self.assertEqual((self.root/'dir/child').read_bytes(), b'original')

    def test_protected_descendant_blocks_directory_replacement(self): self._blocked_directory('.git')
    def test_empty_descendant_blocks_directory_replacement(self): self._blocked_directory('empty')

    def test_child_publication_preserves_parent_workspace_root(self):
        (self.root/'file').write_bytes(b'original')
        parent = self.create_task(); parent_root = Path(self.start(parent)['path'])
        task, result, _ = self.ready_task({'file': b'incoming'}, parent=parent)
        plan = self.prepare_direct(task, result); reserved = self.reserve(plan)
        journal = self.publisher.stage(reserved.operation_id, plan)
        original_inode = parent_root.stat().st_ino
        self.publisher.publish(journal); self.publisher.verify_final(journal)
        self.assertEqual(parent_root.stat().st_ino, original_inode)
        self.assertEqual((parent_root/'file').read_bytes(), b'incoming')
        self.assertEqual((self.root/'file').read_bytes(), b'original')
        self.assertTrue(self.displaced_version_exists(journal, b'original'))

    def test_noop_candidate_preserves_all_inodes(self):
        (self.root/'file').write_bytes(b'original')
        plan = self.plan_with(lambda root: None); reserved = self.reserve(plan)
        journal = self.publisher.stage(reserved.operation_id, plan)
        inode = (self.root/'file').stat().st_ino
        self.publisher.publish(journal); self.publisher.verify_final(journal)
        self.assertEqual((self.root/'file').stat().st_ino, inode)
        self.assertEqual(self.actions(journal), [])
