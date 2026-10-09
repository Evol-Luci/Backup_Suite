import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import project_vault
from project_vault import ProjectVault


class ProjectVaultRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)
        self.project = Path(self.temp_root.name) / "project"
        self.project.mkdir()
        self.vault = ProjectVault(str(self.project))
        self.snapshot_path = Path(self.vault.branches_dir) / "main" / "snapshot.zip"

    def write_snapshot(self, *, corrupt=False):
        with zipfile.ZipFile(self.snapshot_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("restored.txt", b"restore payload")
        if corrupt:
            archive_bytes = bytearray(self.snapshot_path.read_bytes())
            payload_index = archive_bytes.index(b"restore payload")
            archive_bytes[payload_index] ^= 0x01
            self.snapshot_path.write_bytes(archive_bytes)

    def test_corrupt_archive_does_not_remove_current_project_files(self):
        important_file = self.project / "important.txt"
        important_file.write_text("keep this working file", encoding="utf-8")
        self.write_snapshot(corrupt=True)

        with self.assertRaisesRegex(RuntimeError, "corrupted"):
            self.vault.restore_snapshot("snapshot.zip")

        self.assertEqual(important_file.read_text(encoding="utf-8"), "keep this working file")
        self.assertTrue((self.project / ".solace_vault" / "meta.json").is_file())
        self.assertFalse(any(p.name.startswith(".solace-restore-") for p in self.project.parent.iterdir()))

    def test_successful_restore_replaces_files_and_preserves_vault(self):
        (self.project / "old.txt").write_text("old", encoding="utf-8")
        vault_meta = self.project / ".solace_vault" / "meta.json"
        original_meta = vault_meta.read_bytes()
        self.write_snapshot()

        self.vault.restore_snapshot("snapshot.zip")

        self.assertFalse((self.project / "old.txt").exists())
        self.assertEqual((self.project / "restored.txt").read_bytes(), b"restore payload")
        self.assertEqual(vault_meta.read_bytes(), original_meta)
        self.assertTrue(self.snapshot_path.is_file())

    def test_publish_failure_rolls_back_the_previous_project(self):
        old_file = self.project / "old.txt"
        old_file.write_text("keep", encoding="utf-8")
        self.write_snapshot()
        real_rename = os.rename

        def fail_stage_publish(source, destination):
            source_path = Path(source)
            if (source_path.name.startswith(".project.solace-restore-")
                    and os.path.abspath(destination) == str(self.project)):
                raise OSError("injected staging publication failure")
            return real_rename(source, destination)

        with patch.object(project_vault.os, "rename", side_effect=fail_stage_publish):
            with self.assertRaisesRegex(OSError, "injected staging publication failure"):
                self.vault.restore_snapshot("snapshot.zip")

        self.assertEqual(old_file.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.project / "restored.txt").exists())
        self.assertFalse(any("solace-restore-" in p.name for p in self.project.parent.iterdir()))

    def test_archive_cannot_replace_the_vault_metadata_directory(self):
        vault_meta = self.project / ".solace_vault" / "meta.json"
        original_meta = vault_meta.read_bytes()
        with zipfile.ZipFile(self.snapshot_path, "w") as archive:
            archive.writestr(".solace_vault/meta.json", b"attacker-controlled metadata")

        with self.assertRaisesRegex(RuntimeError, "reserved .solace_vault"):
            self.vault.restore_snapshot("snapshot.zip")

        self.assertEqual(vault_meta.read_bytes(), original_meta)

    def test_path_traversal_archive_does_not_touch_project_or_escape_staging(self):
        old_file = self.project / "old.txt"
        old_file.write_text("keep", encoding="utf-8")
        with zipfile.ZipFile(self.snapshot_path, "w") as archive:
            archive.writestr("../escaped.txt", b"outside")

        with self.assertRaisesRegex(ValueError, "path traversal"):
            self.vault.restore_snapshot("snapshot.zip")

        self.assertEqual(old_file.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.project.parent / "escaped.txt").exists())
        self.assertFalse(any("solace-restore-" in p.name for p in self.project.parent.iterdir()))

    def test_unreadable_archive_does_not_remove_current_project_files(self):
        old_file = self.project / "old.txt"
        old_file.write_text("keep", encoding="utf-8")
        self.snapshot_path.write_bytes(b"not a zip archive")

        with self.assertRaises(zipfile.BadZipFile):
            self.vault.restore_snapshot("snapshot.zip")

        self.assertEqual(old_file.read_text(encoding="utf-8"), "keep")
        self.assertTrue((self.project / ".solace_vault" / "meta.json").is_file())


if __name__ == "__main__":
    unittest.main()
