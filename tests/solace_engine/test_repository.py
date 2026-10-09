"""Repository capture, isolation, and integrity behavior."""

import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from solace_engine.errors import CorruptObject, RepositoryError
from solace_engine.repository import ProjectRepository


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.store = self.project / ".solace"
        self.repo = ProjectRepository(self.project, self.store)

    def test_capture_exclusions_symlink_modes_and_nested_store(self):
        (self.project / "source.txt").write_text("shared")
        (self.project / ".hidden").write_text("shared")
        (self.project / "excluded.tmp").write_text("omit")
        (self.base / "outside.txt").write_text("outside")
        (self.project / "external").symlink_to(self.base / "outside.txt")
        executable = self.project / "run.sh"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)

        first = self.repo.create_checkpoint("first", exclusions=("*.tmp",))
        self.assertTrue(self.repo.verify_checkpoint(first.id).ok)
        entries = {entry.path: entry for entry in self.repo.get_file_entries(first.id)}
        self.assertEqual(set(entries), {"source.txt", ".hidden", "external", "run.sh"})
        self.assertEqual(entries["external"].kind, "symlink")
        self.assertEqual(entries["external"].size, len(str(self.base / "outside.txt").encode()))
        self.assertEqual(entries["external"].path, "external")
        self.assertEqual(entries["source.txt"].chunk_digests, entries[".hidden"].chunk_digests)
        self.assertTrue(entries["run.sh"].mode & 0o111)
        self.assertFalse(entries["source.txt"].mode & 0o111)
        self.assertEqual(first.file_count, 4)

        (self.project / "source.txt").write_text("changed")
        second = self.repo.create_checkpoint("second", exclusions=("*.tmp",))
        old = {entry.path: entry for entry in self.repo.get_file_entries(first.id)}
        new = {entry.path: entry for entry in self.repo.get_file_entries(second.id)}
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(old["source.txt"].file_digest, new["source.txt"].file_digest)
        self.assertEqual(self.repo.object_store.read_object(old["source.txt"].chunk_digests[0]), b"shared")
        self.assertEqual(old[".hidden"].chunk_digests, new[".hidden"].chunk_digests)
        self.assertTrue(self.repo.verify_checkpoint(first.id).ok)
        self.assertEqual([row.id for row in self.repo.list_checkpoints()], [first.id, second.id])

    def test_project_stores_are_isolated(self):
        (self.project / "a").write_bytes(b"same")
        first = self.repo.create_checkpoint("one")
        second_project = self.base / "second"
        second_project.mkdir()
        (second_project / "a").write_bytes(b"same")
        other = ProjectRepository(second_project, self.base / "second-store")
        second = other.create_checkpoint("two")
        digest = self.repo.get_file_entries(first.id)[0].chunk_digests[0]
        self.assertEqual(digest, other.get_file_entries(second.id)[0].chunk_digests[0])
        self.assertTrue(self.repo.object_store._object_path(digest).exists())
        self.assertTrue(other.object_store._object_path(digest).exists())
        self.repo.object_store._object_path(digest).unlink()
        self.assertFalse(self.repo.verify_checkpoint(first.id).ok)
        self.assertTrue(other.verify_checkpoint(second.id).ok)

    def test_database_failure_leaves_only_reclaimable_objects(self):
        (self.project / "a").write_bytes(b"orphan")
        def insert_then_fail(connection, checkpoint, entries):
            connection.execute(
                "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                (checkpoint.id, checkpoint.message, checkpoint.created_at,
                 checkpoint.parent_id, checkpoint.file_count, checkpoint.logical_bytes, "[]"),
            )
            raise sqlite3.OperationalError("injected after first insert")

        with patch.object(self.repo, "_insert_checkpoint", side_effect=insert_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.repo.create_checkpoint("failed")
        self.assertEqual(self.repo.list_checkpoints(), [])
        with closing(sqlite3.connect(self.repo.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM file_entries").fetchone()[0], 0)
        self.assertGreater(self.repo.storage_usage().reclaimable_bytes, 0)

    def test_file_swap_during_capture_does_not_read_outside_project(self):
        inside = self.project / "a"
        inside.write_bytes(b"inside")
        outside = self.base / "outside"
        outside.write_bytes(b"outside secret")
        original_put_file = self.repo.object_store.put_file

        def swap_before_read(path):
            inside.unlink()
            inside.symlink_to(outside)
            return original_put_file(path)

        with patch.object(self.repo.object_store, "put_file", side_effect=swap_before_read):
            with self.assertRaisesRegex(RepositoryError, "changed"):
                self.repo.create_checkpoint("swapped")
        self.assertEqual(self.repo.list_checkpoints(), [])
        objects = [path.read_bytes() for path in (self.store / "objects").glob("*/*")]
        self.assertEqual(objects, [b"inside"])

    def test_directory_swap_before_open_is_rejected(self):
        child = self.project / "sub"
        child.mkdir()
        (child / "a").write_bytes(b"inside")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"outside")
        original_open = os.open

        def swap_directory(path, flags, *args, **kwargs):
            if path == "sub":
                child.rename(self.base / "old-sub")
                child.symlink_to(outside, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch("solace_engine.repository.os.open", side_effect=swap_directory):
            with self.assertRaises(OSError):
                self.repo.create_checkpoint("swapped directory")
        self.assertEqual(self.repo.list_checkpoints(), [])

    def test_file_swapped_to_fifo_cannot_block_open(self):
        source = self.project / "a"
        source.write_bytes(b"inside")
        original_open = os.open

        def swap_to_fifo(path, flags, *args, **kwargs):
            if path == "a":
                self.assertTrue(flags & os.O_NONBLOCK, "file open must be nonblocking")
                source.unlink()
                os.mkfifo(source)
            return original_open(path, flags, *args, **kwargs)

        with patch("solace_engine.repository.os.open", side_effect=swap_to_fifo):
            with self.assertRaises(RepositoryError):
                self.repo.create_checkpoint("swapped to fifo")
        self.assertEqual(self.repo.list_checkpoints(), [])

    def test_concurrent_instances_publish_complete_checkpoints(self):
        (self.project / "a").write_bytes(b"concurrent")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def capture(index):
            try:
                repository = ProjectRepository(self.project, self.store)
                barrier.wait(timeout=5)
                results.append(repository.create_checkpoint(str(index)))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=capture, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.repo.list_checkpoints()), 2)
        self.assertTrue(all(self.repo.verify_checkpoint(row.id).ok for row in results))

    def test_corrupt_object_is_reported(self):
        (self.project / "a").write_bytes(b"content")
        checkpoint = self.repo.create_checkpoint("created")
        digest = self.repo.get_file_entries(checkpoint.id)[0].chunk_digests[0]
        self.repo.object_store._object_path(digest).write_bytes(b"corrupt")
        report = self.repo.verify_checkpoint(checkpoint.id)
        self.assertFalse(report.ok)
        self.assertTrue(report.errors)

    def test_manifest_digest_mismatch_is_reported(self):
        (self.project / "a").write_bytes(b"content")
        checkpoint = self.repo.create_checkpoint("created")
        with closing(sqlite3.connect(self.repo.database_path)) as connection:
            with connection:
                connection.execute("UPDATE file_entries SET file_digest = ? WHERE checkpoint_id = ?",
                                   ("0" * 64, checkpoint.id))
        report = self.repo.verify_checkpoint(checkpoint.id)
        self.assertFalse(report.ok)
        self.assertTrue(any("whole-file digest mismatch" in error for error in report.errors))

    def test_changed_multichunk_reference_fails_reconstruction(self):
        (self.project / "large").write_bytes(bytes(range(256)) * 4096 + bytes(reversed(range(256))) * 4096)
        checkpoint = self.repo.create_checkpoint("large")
        chunks = self.repo.get_file_entries(checkpoint.id)[0].chunk_digests
        self.assertGreater(len(chunks), 1)
        self.assertNotEqual(chunks[0], chunks[1])
        with closing(sqlite3.connect(self.repo.database_path)) as connection:
            with connection:
                connection.execute(
                    "UPDATE chunk_refs SET digest = ? WHERE checkpoint_id = ? AND ordinal = 0",
                    (chunks[1], checkpoint.id),
                )
        report = self.repo.verify_checkpoint(checkpoint.id)
        self.assertFalse(report.ok)
        self.assertTrue(any("whole-file digest mismatch" in error for error in report.errors))

    def test_special_file_yields_warning(self):
        os.mkfifo(self.project / "pipe")
        checkpoint = self.repo.create_checkpoint("created")
        self.assertEqual(checkpoint.file_count, 0)
        self.assertTrue(any("pipe" in warning for warning in checkpoint.warnings))

    def test_restore_content_mode_and_symlink_to_new_destination(self):
        (self.project / "sub").mkdir()
        (self.project / "sub" / "text").write_bytes(b"saved content")
        executable = self.project / "run.sh"
        executable.write_bytes(b"#!/bin/sh\n")
        executable.chmod(0o755)
        (self.project / "link").symlink_to("sub/text")
        checkpoint = self.repo.create_checkpoint("saved")
        destination = self.base / "restored"

        self.repo.restore_checkpoint(checkpoint.id, destination)

        self.assertEqual((destination / "sub" / "text").read_bytes(), b"saved content")
        self.assertEqual((destination / "run.sh").read_bytes(), b"#!/bin/sh\n")
        self.assertTrue((destination / "run.sh").stat().st_mode & 0o111)
        self.assertTrue((destination / "link").is_symlink())
        self.assertEqual(os.readlink(destination / "link"), "sub/text")

    def test_restore_refuses_existing_destination_including_broken_symlink(self):
        (self.project / "a").write_bytes(b"saved")
        checkpoint = self.repo.create_checkpoint("saved")
        destination = self.base / "restored"
        destination.mkdir()
        (destination / "keep").write_bytes(b"untouched")
        with self.assertRaises(FileExistsError):
            self.repo.restore_checkpoint(checkpoint.id, destination)
        self.assertEqual((destination / "keep").read_bytes(), b"untouched")
        destination = self.base / "broken"
        destination.symlink_to("missing")
        with self.assertRaises(FileExistsError):
            self.repo.restore_checkpoint(checkpoint.id, destination)
        self.assertTrue(destination.is_symlink())

    def test_corrupt_restore_does_not_publish_or_leave_staging(self):
        (self.project / "a").write_bytes(b"saved")
        checkpoint = self.repo.create_checkpoint("saved")
        digest = self.repo.get_file_entries(checkpoint.id)[0].chunk_digests[0]
        self.repo.object_store._object_path(digest).write_bytes(b"corrupt")
        destination = self.base / "restored"
        with self.assertRaises(CorruptObject):
            self.repo.restore_checkpoint(checkpoint.id, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.base.glob(".restored.*")), [])

    def test_restore_write_failure_cleans_only_its_staging_tree(self):
        (self.project / "a").write_bytes(b"saved")
        checkpoint = self.repo.create_checkpoint("saved")
        destination = self.base / "restored"
        original_read = self.repo.object_store.read_object
        calls = 0

        def fail_after_preflight(digest):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise CorruptObject("changed after preflight")
            return original_read(digest)

        with patch.object(self.repo.object_store, "read_object", side_effect=fail_after_preflight):
            with self.assertRaises(CorruptObject):
                self.repo.restore_checkpoint(checkpoint.id, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.base.glob(".restored.*")), [])

    def test_restore_publication_refuses_destination_created_during_staging(self):
        (self.project / "a").write_bytes(b"saved")
        checkpoint = self.repo.create_checkpoint("saved")
        destination = self.base / "restored"
        original_publish = self.repo._publish_restore

        def create_destination_then_publish(staging, target):
            destination.mkdir()
            (destination / "keep").write_bytes(b"untouched")
            original_publish(staging, target)

        with patch.object(self.repo, "_publish_restore", side_effect=create_destination_then_publish):
            with self.assertRaises(FileExistsError):
                self.repo.restore_checkpoint(checkpoint.id, destination)
        self.assertEqual((destination / "keep").read_bytes(), b"untouched")
        self.assertEqual(list(self.base.glob(".restored.*")), [])

    def test_restore_rejects_unsafe_catalog_paths_before_materialization(self):
        (self.project / "a").write_bytes(b"saved")
        checkpoint = self.repo.create_checkpoint("saved")
        with closing(sqlite3.connect(self.repo.database_path)) as connection:
            with connection:
                connection.execute("UPDATE file_entries SET path = ? WHERE checkpoint_id = ?",
                                   ("../escaped", checkpoint.id))
                connection.execute("UPDATE chunk_refs SET path = ? WHERE checkpoint_id = ?",
                                   ("../escaped", checkpoint.id))
        destination = self.base / "restored"
        with self.assertRaises(RepositoryError):
            self.repo.restore_checkpoint(checkpoint.id, destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.base / "escaped").exists())

    def test_storage_usage_uses_newest_snapshot_and_only_valid_unreferenced_objects(self):
        (self.project / "a").write_bytes(b"first")
        first = self.repo.create_checkpoint("first")
        (self.project / "a").write_bytes(b"second version")
        second = self.repo.create_checkpoint("second")
        self.repo.object_store.put_bytes(b"orphan")
        corrupt = self.repo.object_store.put_bytes(b"bad")
        self.repo.object_store._object_path(corrupt).write_bytes(b"corrupt-data")
        with closing(sqlite3.connect(self.repo.database_path)) as connection:
            with connection:
                connection.execute("UPDATE checkpoints SET logical_bytes = 999 WHERE id = ?",
                                   (second.id,))

        usage = self.repo.storage_usage()

        self.assertEqual(usage.logical_bytes, second.logical_bytes)
        self.assertEqual(usage.physical_bytes,
                         len(b"first") + len(b"second version") + len(b"orphan") + len(b"corrupt-data"))
        self.assertEqual(usage.reclaimable_bytes, len(b"orphan"))
        self.assertTrue(self.repo.verify_checkpoint(first.id).ok)


if __name__ == "__main__":
    unittest.main()
