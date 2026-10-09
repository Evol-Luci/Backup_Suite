import dataclasses
import unittest

from solace_engine import Checkpoint, FileEntry, StorageUsage, VerificationReport
from solace_engine.errors import (
    CheckpointNotFound,
    CorruptObject,
    InvalidProjectPath,
    RepositoryError,
)


class CheckpointModelTests(unittest.TestCase):
    def test_checkpoint_preserves_field_values_and_optional_parent(self):
        checkpoint = Checkpoint("cp-1", "Initial", "2026-10-09T12:00:00Z", None, 2, 128)

        self.assertEqual(
            checkpoint,
            Checkpoint("cp-1", "Initial", "2026-10-09T12:00:00Z", None, 2, 128, ()),
        )
        self.assertIsNone(checkpoint.parent_id)
        self.assertEqual(checkpoint.warnings, ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            checkpoint.message = "changed"

    def test_checkpoint_accepts_parent_and_warnings(self):
        checkpoint = Checkpoint("cp-2", "Next", "stamp", "cp-1", 1, 64, ("special file skipped",))

        self.assertEqual(checkpoint.parent_id, "cp-1")
        self.assertEqual(checkpoint.warnings, ("special file skipped",))

    def test_file_entry_preserves_field_values(self):
        entry = FileEntry("notes.txt", "file", 0o644, 8, "sha256:abc", ("sha256:def",))

        self.assertEqual(entry.path, "notes.txt")
        self.assertEqual(entry.kind, "file")
        self.assertEqual(entry.mode, 0o644)
        self.assertEqual(entry.size, 8)
        self.assertEqual(entry.file_digest, "sha256:abc")
        self.assertEqual(entry.chunk_digests, ("sha256:def",))

    def test_verification_report_preserves_field_values(self):
        report = VerificationReport("cp-1", False, 3, ("missing object",))

        self.assertEqual(report, VerificationReport("cp-1", False, 3, ("missing object",)))

    def test_storage_usage_preserves_field_values(self):
        usage = StorageUsage(100, 60, 20)

        self.assertEqual(usage, StorageUsage(100, 60, 20))


class ErrorHierarchyTests(unittest.TestCase):
    def test_domain_errors_inherit_from_repository_error(self):
        for error_type in (InvalidProjectPath, CheckpointNotFound, CorruptObject):
            with self.subTest(error_type=error_type.__name__):
                self.assertTrue(issubclass(error_type, RepositoryError))


if __name__ == "__main__":
    unittest.main()
