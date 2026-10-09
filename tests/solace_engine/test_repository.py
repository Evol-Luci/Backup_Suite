"""Repository capture, isolation, and integrity behavior."""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

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
        with patch.object(self.repo, "_insert_checkpoint", side_effect=sqlite3.OperationalError("injected")):
            with self.assertRaises(sqlite3.OperationalError):
                self.repo.create_checkpoint("failed")
        self.assertEqual(self.repo.list_checkpoints(), [])
        self.assertGreater(self.repo.storage_usage().reclaimable_bytes, 0)

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
        with sqlite3.connect(self.repo.database_path) as connection:
            connection.execute("UPDATE file_entries SET file_digest = ? WHERE checkpoint_id = ?",
                               ("0" * 64, checkpoint.id))
        report = self.repo.verify_checkpoint(checkpoint.id)
        self.assertFalse(report.ok)
        self.assertTrue(any("whole-file digest mismatch" in error for error in report.errors))

    def test_special_file_yields_warning(self):
        import os
        os.mkfifo(self.project / "pipe")
        checkpoint = self.repo.create_checkpoint("created")
        self.assertEqual(checkpoint.file_count, 0)
        self.assertTrue(any("pipe" in warning for warning in checkpoint.warnings))


if __name__ == "__main__":
    unittest.main()
