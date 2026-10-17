"""Byte preservation, conservative manifests and bounded worker failures."""
import hashlib
import multiprocessing
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from solace_engine.models import FileEntry


def never_replies(connection, base, current, incoming):
    time.sleep(60)


def conditional_worker(connection, base, current, incoming):
    if base.startswith(b'hang'):
        never_replies(connection, base, current, incoming)
    else:
        from solace_core.integration_merge import _text_worker
        _text_worker(connection, base, current, incoming)


def malformed_worker(connection, *args):
    connection.send_bytes(b'?bad')
    connection.close()


def invalid_text_worker(connection, *args):
    connection.send_bytes(b'B\xff')
    connection.close()


def oversized_frame_worker(connection, *args):
    import os
    os.write(connection.fileno(), b'\x7f\xff\xff\xff')
    connection.close()


def crash_after_reply_worker(connection, *args):
    import os
    connection.send_bytes(b'BA\nb\nC')
    connection.close()
    os._exit(3)


def hang_after_reply_worker(connection, *args):
    connection.send_bytes(b'BA\nb\nC')
    time.sleep(60)


def trailing_frame_worker(connection, *args):
    connection.send_bytes(b'BA\nb\nC')
    time.sleep(0.1)
    connection.send_bytes(b'extra frame')
    connection.close()


def partial_worker(connection, *args):
    import os
    os.write(connection.fileno(), b'\x00\x00\x00\x20x')
    time.sleep(60)


def crash_worker(connection, *args):
    connection.close()


class BytesStore:
    def __init__(self):
        self.objects = {}
        self.written = []

    def entry(self, path, data, mode=0o644, kind='file'):
        digest = hashlib.sha256(data).hexdigest()
        self.objects[digest] = data
        return FileEntry(path, kind, mode, len(data), digest, (digest,))

    def read(self, entry):
        return self.objects[entry.file_digest]

    def write(self, path, data, mode):
        self.written.append((path, data, mode))
        return self.entry(path, data, mode)


class TextMergeTests(unittest.TestCase):
    def merge(self, *args):
        from solace_core.integration_merge import merge_text
        return merge_text(*args)

    def test_disjoint_crlf_preserves_bytes(self):
        self.assertEqual(self.merge(b'a\r\nb\r\nc', b'A\r\nb\r\nc', b'a\r\nb\r\nC'),
                         (b'A\r\nb\r\nC', None))

    def test_same_position_different_insertions_conflict(self):
        self.assertEqual(self.merge(b'a\n', b'x\na\n', b'y\na\n'), (None, 'overlapping_text'))

    def test_equality_fast_paths_preserve_even_non_text(self):
        for b, c, i, expected in [(b'\0', b'\xff', b'\xff', b'\xff'),
                                  (b'\0', b'\xff', b'\0', b'\xff'),
                                  (b'\0', b'\0', b'\xff', b'\xff')]:
            with self.subTest(b=b, c=c, i=i):
                self.assertEqual(self.merge(b, c, i), (expected, None))

    def test_disjoint_deletion_and_adjacent_replacements(self):
        for b, c, i, expected in [(b'a\nb\nc\nd\n', b'b\nc\nd\n', b'a\nb\nc\n', b'b\nc\n'),
                                  (b'a\nb\n', b'A\nb\n', b'a\nB\n', b'A\nB\n'),
                                  (b'a\nb\n', b'A\nb\n', b'a\nb\nx', b'A\nb\nx'),
                                  (b'a\nb\nc\n', b'A\nb\nc\n', b'A\nb\nC\n', b'A\nb\nC\n'),
                                  (b'a\nb\n', b'x\na\nb\n', b'x\na\nB\n', b'x\na\nB\n'),
                                  ('é\r\nb\r\nø'.encode(), 'É\r\nb\r\nø'.encode(),
                                   'é\r\nb\r\nØ'.encode(), 'É\r\nb\r\nØ'.encode())]:
            with self.subTest(b=b, c=c, i=i):
                self.assertEqual(self.merge(b, c, i), (expected, None))

    def test_boundary_insertions_and_overlaps_conflict(self):
        for c, i in [(b'x\na\nb\n', b'A\nb\n'), (b'a\nx\nb\n', b'A\nb\n'),
                     (b'A\nb\n', b'X\nb\n'), (b'a\nB\n', b'a\nb\nx')]:
            with self.subTest(c=c, i=i):
                self.assertEqual(self.merge(b'a\nb\n', c, i), (None, 'overlapping_text'))

    def test_invalid_utf8_and_nul_require_whole_file_selection(self):
        for invalid in [b'\xff\n', b'\0\n']:
            for slot in range(3):
                values = [b'a\n', b'b\n', b'c\n']; values[slot] = invalid
                with self.subTest(slot=slot, invalid=invalid):
                    self.assertEqual(self.merge(*values), (None, 'non_text'))

    def test_exact_byte_limit_and_one_over_on_each_input(self):
        b = b'a\n' + b'z' * (1024 * 1024 - 4) + b'\nc'
        self.assertEqual(self.merge(b, b'A' + b[1:], b[:-1] + b'C'),
                         (b'A' + b[1:-1] + b'C', None))
        for slot in range(3):
            values = [b'a', b'b', b'c']; values[slot] = b'x' * (1024 * 1024 + 1)
            with self.subTest(slot=slot):
                self.assertEqual(self.merge(*values), (None, 'text_too_large'))

    def test_exact_line_limit_and_one_over_on_each_input(self):
        # Distinct lines keep this exact-limit test linear, not a diff stress test.
        b = b''.join(str(n).encode() + b'\n' for n in range(50000))
        self.assertEqual(self.merge(b, b'A\n' + b[2:], b[:-6] + b'Z\n'),
                         (b'A\n' + b[2:-6] + b'Z\n', None))
        for slot in range(3):
            values = [b'a', b'b', b'c']; values[slot] = b'x\n' * 50001
            with self.subTest(slot=slot):
                self.assertEqual(self.merge(*values), (None, 'text_too_many_lines'))


class ManifestFixture(unittest.TestCase):
    def setUp(self):
        self.store = BytesStore()

    def merge(self, b=(), c=(), i=()):
        from solace_core.integration_merge import merge_entries
        return merge_entries(tuple(b), tuple(c), tuple(i), read_bytes=self.store.read,
                             write_bytes=self.store.write)


class ManifestMergeTests(ManifestFixture):
    def test_reuses_unchanged_objects_and_keeps_literal_backslash_path(self):
        b = self.store.entry('a\\b', b'\xff')
        i = self.store.entry('new', b'\0')
        result = self.merge([b], [b], [b, i])
        self.assertIs(result.entries[0], b)
        self.assertIs(result.entries[1], i)
        self.assertEqual(result.conflicts, ())
        self.assertEqual(self.store.written, [])

    def test_independent_content_and_mode_edits_both_directions(self):
        b = self.store.entry('a', b'a\nb\n')
        content = self.store.entry('a', b'A\nb\n')
        executable = replace(b, mode=0o755)
        for c, i in [(content, executable), (executable, content)]:
            with self.subTest(c=c):
                result = self.merge([b], [c], [i])
                self.assertEqual(result.entries, (replace(content, mode=0o755),))
                self.assertEqual(result.conflicts, ())
        self.assertEqual(self.store.written, [])

    def test_identical_kind_transition_merges_new_content_and_mode_independently(self):
        b = self.store.entry('a', b'base')
        c = self.store.entry('a', b'target', 0o755, 'symlink')
        i = self.store.entry('a', b'target', 0o644, 'symlink')
        result = self.merge([b], [c], [i])
        self.assertEqual(result.entries, (c,))
        self.assertEqual(result.conflicts, ())
        # Differing target text has no compatible old-kind content baseline.
        i = self.store.entry('a', b'other', 0o644, 'symlink')
        result = self.merge([b], [c], [i])
        self.assertEqual(result.entries, (c,))
        self.assertEqual([x.reason for x in result.conflicts], ['incompatible_kind'])

    def test_non_utf8_symlink_text_is_preserved_without_text_interpretation(self):
        i = self.store.entry('link', b'target-\xff', 0o777, 'symlink')
        result = self.merge([], [], [i])
        self.assertIs(result.entries[0], i)
        self.assertEqual(self.store.read(result.entries[0]), b'target-\xff')
        self.assertEqual(result.conflicts, ())

    def test_generated_text_uses_writer_and_merged_mode(self):
        b = self.store.entry('a', b'a\nb\nc')
        c = self.store.entry('a', b'A\nb\nc', 0o755)
        i = self.store.entry('a', b'a\nb\nC')
        result = self.merge([b], [c], [i])
        self.assertEqual(self.store.read(result.entries[0]), b'A\nb\nC')
        self.assertEqual(self.store.written, [('a', b'A\nb\nC', 0o755)])
        self.assertEqual(result.conflicts, ())

    def test_conflicts_keep_current_and_all_input_metadata(self):
        for kind, reason in [('binary', 'non_text'), ('delete', 'delete_modify'),
                             ('mode', 'incompatible_mode'), ('kind', 'incompatible_kind'),
                             ('link', 'link_target'), ('add', 'add_add')]:
            with self.subTest(kind=kind):
                b = self.store.entry('a', b'base\0' if kind == 'binary' else b'base')
                c = self.store.entry('a', b'current\0' if kind == 'binary' else b'current')
                i = self.store.entry('a', b'incoming\0' if kind == 'binary' else b'incoming')
                if kind == 'delete': c = None
                if kind == 'mode': c = replace(c, mode=0o755); i = replace(i, mode=0o600)
                if kind == 'kind': i = replace(i, kind='symlink')
                if kind == 'link': b = replace(b, kind='symlink'); c = replace(c, kind='symlink'); i = replace(i, kind='symlink')
                if kind == 'add': b = None
                result = self.merge([b] if b else [], [c] if c else [], [i])
                self.assertEqual(result.entries, (c,) if c else ())
                self.assertEqual([(x.path, x.reason, x.base, x.current, x.incoming) for x in result.conflicts],
                                 [('a', reason, b, c, i)])

    def test_symlink_text_and_mode_are_independent_attributes(self):
        b = self.store.entry('link', b'a', 0o777, 'symlink')
        c = replace(b, mode=0o700)
        i = self.store.entry('link', b'b', 0o777, 'symlink')
        result = self.merge([b], [c], [i])
        self.assertEqual(result.entries, (replace(i, mode=0o700),))
        self.assertEqual(result.conflicts, ())

    def test_prefix_collision_retains_current_subtree_and_other_work(self):
        c = self.store.entry('a/b', b'current')
        i = self.store.entry('a', b'incoming')
        other = self.store.entry('other', b'ok')
        result = self.merge([], [c], [i, other])
        self.assertEqual(result.entries, (c, other))
        self.assertIn(('a', 'path_prefix'), [(x.path, x.reason) for x in result.conflicts])
        self.assertIn(('a/b', 'path_prefix'), [(x.path, x.reason) for x in result.conflicts])

    def test_file_replaced_by_directory_retains_current_subtree_on_conflict(self):
        b = self.store.entry('a', b'base')
        c = self.store.entry('a', b'edited')
        i = self.store.entry('a/b', b'incoming')
        result = self.merge([b], [c], [i])
        self.assertEqual(result.entries, (c,))
        self.assertIn('a/b', [x.path for x in result.conflicts])

    def test_unchanged_link_becomes_unsafe_after_another_link_changes(self):
        # C adds l -> a/..; R changes a -> '.'. Each input is safe, but
        # combining them makes C's unchanged link escape the tree.
        b = self.store.entry('a', b'b', 0o777, 'symlink')
        link = self.store.entry('l', b'a/..', 0o777, 'symlink')
        i = self.store.entry('a', b'.', 0o777, 'symlink')
        other = self.store.entry('other', b'keep evidence')
        result = self.merge([b], [b, link], [i, other])
        self.assertEqual(result.entries, (b, link))
        self.assertEqual({x.path for x in result.conflicts}, {'a', 'l', 'other'})
        self.assertTrue(all(x.reason == 'unsafe_link' for x in result.conflicts))

    def test_invalid_current_fallback_raises_structured_refusal(self):
        from solace_core.errors import CoreError
        c = self.store.entry('link', b'/escape', 0o777, 'symlink')
        with self.assertRaises(CoreError) as raised:
            self.merge([], [c], [])
        self.assertEqual(raised.exception.code, 'INTEGRATION_MANIFEST_INVALID')

    def test_duplicate_and_unsafe_paths_are_refused_without_silent_loss(self):
        from solace_core.errors import CoreError
        for entries in [[self.store.entry('a', b'a'), self.store.entry('a', b'b')],
                        [self.store.entry('../escape', b'a')]]:
            with self.subTest(entries=entries), self.assertRaises(CoreError):
                self.merge([], entries, [])


class WorkerMergeTests(ManifestFixture):
    def test_worker_failures_keep_whole_current_file_and_leave_no_children(self):
        import solace_core.integration_merge as module
        baseline = {p.pid for p in multiprocessing.active_children()}
        b = self.store.entry('a', b'a\nb\nc')
        c = self.store.entry('a', b'A\nb\nc')
        i = self.store.entry('a', b'a\nb\nC')
        for worker, reason in [(never_replies, 'merge_timeout'), (partial_worker, 'merge_timeout'),
                               (malformed_worker, 'merge_worker_malformed'),
                               (invalid_text_worker, 'merge_worker_malformed'),
                               (oversized_frame_worker, 'merge_worker_malformed'),
                               (crash_worker, 'merge_worker_failed'),
                               (crash_after_reply_worker, 'merge_worker_failed'),
                               (hang_after_reply_worker, 'merge_timeout')]:
            with self.subTest(worker=worker), patch.object(module, '_text_worker', worker):
                started = time.monotonic()
                result = self.merge([b], [c], [i])
                self.assertLess(time.monotonic() - started, 3.0)
                self.assertEqual(result.entries, (c,))
                self.assertEqual([x.reason for x in result.conflicts], [reason])
                self.assertEqual({p.pid for p in multiprocessing.active_children()}, baseline)

    def test_extra_frame_after_valid_reply_is_malformed(self):
        import solace_core.integration_merge as module
        b = self.store.entry('a', b'a\nb\nc')
        c = self.store.entry('a', b'A\nb\nc')
        i = self.store.entry('a', b'a\nb\nC')
        with patch.object(module, '_text_worker', trailing_frame_worker):
            result = self.merge([b], [c], [i])
        self.assertEqual(result.entries, (c,))
        self.assertEqual([x.reason for x in result.conflicts], ['merge_worker_malformed'])

    def test_another_project_responds_while_one_worker_never_replies(self):
        import solace_core.integration_merge as module
        b = self.store.entry('hang', b'hang\nb\nc')
        c = self.store.entry('hang', b'HANG\nb\nc')
        i = self.store.entry('hang', b'hang\nb\nC')
        results = []
        with patch.object(module, '_text_worker', conditional_worker):
            thread = threading.Thread(target=lambda: results.append(self.merge([b], [c], [i])))
            thread.start()
            try:
                b2 = self.store.entry('other', b'a\nb\nc')
                c2 = self.store.entry('other', b'A\nb\nc')
                i2 = self.store.entry('other', b'a\nb\nC')
                result = self.merge([b2], [c2], [i2])
                self.assertEqual(self.store.read(result.entries[0]), b'A\nb\nC')
                self.assertTrue(thread.is_alive(), 'unrelated project waited for hung calculation')
            finally:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([x.reason for x in results[0].conflicts], ['merge_timeout'])

    def test_manifest_boundaries_are_enforced_before_or_inside_worker(self):
        b = b'a\n' + b'z' * (1024 * 1024 - 4) + b'\nc'
        c, i = b'A' + b[1:], b[:-1] + b'C'
        entries = [self.store.entry('a', data) for data in (b, c, i)]
        result = self.merge([entries[0]], [entries[1]], [entries[2]])
        self.assertEqual(self.store.read(result.entries[0]), b'A' + b[1:-1] + b'C')
        self.assertEqual(result.conflicts, ())
        b = b''.join(str(n).encode() + b'\n' for n in range(50000))
        entries = [self.store.entry('a', data) for data in (b, b'A\n' + b[2:], b[:-6] + b'Z\n')]
        result = self.merge([entries[0]], [entries[1]], [entries[2]])
        self.assertEqual(self.store.read(result.entries[0]), b'A\n' + b[2:-6] + b'Z\n')
        self.assertEqual(result.conflicts, ())
        for data, reason in [(b'x' * (1024 * 1024 + 1), 'text_too_large'),
                             (b'x\n' * 50001, 'text_too_many_lines')]:
            for slot in range(3):
                values = [b'a', b'b', b'c']; values[slot] = data
                entries = [self.store.entry('a', value) for value in values]
                with self.subTest(slot=slot, reason=reason):
                    result = self.merge([entries[0]], [entries[1]], [entries[2]])
                    self.assertEqual(result.entries, (entries[1],))
                    self.assertEqual([x.reason for x in result.conflicts], [reason])

    def test_metadata_cannot_pass_unbounded_bytes_to_worker(self):
        b = self.store.entry('a', b'x' * (1024 * 1024 + 1))
        c = self.store.entry('a', b'current')
        i = self.store.entry('a', b'incoming')
        result = self.merge([replace(b, size=1)], [c], [i])
        self.assertEqual(result.entries, (c,))
        self.assertEqual([x.reason for x in result.conflicts], ['text_too_large'])

    def test_saturated_workers_return_explicit_conflict_without_waiting(self):
        import solace_core.integration_merge as module
        b = self.store.entry('a', b'a\nb\nc')
        c = self.store.entry('a', b'A\nb\nc')
        i = self.store.entry('a', b'a\nb\nC')
        gate = threading.BoundedSemaphore(1)
        gate.acquire()
        with patch.object(module, '_WORKER_SLOTS', gate):
            result = self.merge([b], [c], [i])
        self.assertEqual(result.entries, (c,))
        self.assertEqual([x.reason for x in result.conflicts], ['merge_worker_busy'])


if __name__ == '__main__':
    unittest.main()
