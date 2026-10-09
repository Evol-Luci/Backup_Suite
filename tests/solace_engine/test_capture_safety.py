"""Regression coverage for capture identity, stability, and durability."""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solace_engine.chunking import iter_file_chunks
from solace_engine.errors import InvalidProjectPath, RepositoryError
from solace_engine.repository import ProjectRepository


class CaptureSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "source").write_bytes(b"saved")

    def test_dotdot_and_symlink_store_aliases_are_excluded(self):
        (self.project / "sub").mkdir()
        alias = self.base / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        for store in (self.project / "sub" / ".." / "store", alias / "store"):
            with self.subTest(store=store):
                repo = ProjectRepository(self.project, store)
                checkpoint = repo.create_checkpoint("alias")
                self.assertEqual([entry.path for entry in repo.get_file_entries(checkpoint.id)],
                                 ["source"])

    def test_project_root_alias_is_rejected_as_store(self):
        (self.project / "sub").mkdir()
        alias = self.base / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        for store in (self.project / "sub" / "..", alias):
            with self.subTest(store=store), self.assertRaises(InvalidProjectPath):
                ProjectRepository(self.project, store)
        self.assertFalse((self.project / "checkpoints.sqlite3").exists())

    def test_parent_symlink_project_alias_does_not_capture_nested_store(self):
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        repo = ProjectRepository(alias / "project", self.project / "store")
        checkpoint = repo.create_checkpoint("aliased project")
        self.assertEqual([entry.path for entry in repo.get_file_entries(checkpoint.id)], ["source"])

    def test_backslash_names_round_trip_on_linux(self):
        directory = self.project / "dir\\name"
        directory.mkdir()
        (directory / "file\\name").write_bytes(b"backslash")
        (self.project / "link\\name").symlink_to("dir\\name/file\\name")
        repo = ProjectRepository(self.project, self.base / "store")
        checkpoint = repo.create_checkpoint("posix names")
        destination = self.base / "restored"
        repo.restore_checkpoint(checkpoint.id, destination)
        self.assertEqual((destination / "dir\\name" / "file\\name").read_bytes(), b"backslash")
        self.assertEqual(os.readlink(destination / "link\\name"), "dir\\name/file\\name")

    def test_in_place_edits_during_object_write_prevent_checkpoint_publication(self):
        for size in (32, 2 * 1048576):
            with self.subTest(size=size):
                source = self.project / "source"
                source.write_bytes(b"a" * size)
                repo = ProjectRepository(self.project, self.base / f"store-{size}")
                original_put = repo.object_store.put_bytes
                edited = False

                def edit_after_first_chunk(data):
                    nonlocal edited
                    digest = original_put(data)
                    if not edited:
                        edited = True
                        with source.open("r+b") as stream:
                            stream.seek(size - 1)
                            stream.write(b"b")
                        current = source.stat()
                        os.utime(source, ns=(current.st_atime_ns, current.st_mtime_ns + 1000000))
                    return digest

                with patch.object(repo.object_store, "put_bytes", side_effect=edit_after_first_chunk):
                    with self.assertRaisesRegex(RepositoryError, "changed"):
                        repo.create_checkpoint("changing file")
                self.assertEqual(repo.list_checkpoints(), [])

    def test_chunk_iterator_rejects_growth_after_whole_file_yield(self):
        source = self.project / "source"
        chunks = iter_file_chunks(source)
        self.assertEqual(next(chunks), b"saved")
        with source.open("ab") as stream:
            stream.write(b"more")
        with self.assertRaisesRegex(RepositoryError, "changed"):
            list(chunks)

    def test_directory_entries_are_synced_before_checkpoint_insertion(self):
        store = self.base / "new-parent" / "store"
        synced = []
        original_fsync = os.fsync

        def record_sync(fd):
            result = original_fsync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                synced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
            return result

        with patch("solace_engine.object_store.os.fsync", side_effect=record_sync):
            repo = ProjectRepository(self.project, store)
            original_insert = repo._insert_checkpoint

            def check_before_insert(connection, checkpoint, entries):
                digest = entries[0].chunk_digests[0]
                shard = store / "objects" / digest[:2]
                for directory in (shard, store / "objects", store, store.parent, self.base):
                    self.assertIn(directory, synced)
                self.assertLess(synced.index(shard), synced.index(store / "objects"))
                return original_insert(connection, checkpoint, entries)

            with patch.object(repo, "_insert_checkpoint", side_effect=check_before_insert):
                checkpoint = repo.create_checkpoint("durable")
        self.assertTrue(repo.verify_checkpoint(checkpoint.id).ok)

    def test_parent_sync_failure_prevents_checkpoint_and_reuse_syncs_again(self):
        store = self.base / "store"
        repo = ProjectRepository(self.project, store)
        original_fsync = os.fsync

        def fail_objects_parent(fd):
            if (stat.S_ISDIR(os.fstat(fd).st_mode) and
                    Path(os.readlink(f"/proc/self/fd/{fd}")) == store / "objects"):
                raise OSError("directory sync failed")
            return original_fsync(fd)

        with patch("solace_engine.object_store.os.fsync", side_effect=fail_objects_parent):
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                repo.create_checkpoint("unsynced")
            self.assertEqual(repo.list_checkpoints(), [])
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                repo.create_checkpoint("unsynced existing object")
        checkpoint = repo.create_checkpoint("retry")
        self.assertTrue(repo.verify_checkpoint(checkpoint.id).ok)


if __name__ == "__main__":
    unittest.main()
