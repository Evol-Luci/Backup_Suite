import shutil
import tempfile
import unittest
from pathlib import Path

from project_vault import ProjectVault


class ProjectVaultMergeTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)
        root = Path(self.temp_root.name)
        self.project = root / "project"
        self.project.mkdir()
        self.vault = ProjectVault(str(self.project))
        self.snapshot_dir = root / "snapshot"
        self.snapshot_dir.mkdir()
        self.snapshot_file = self.snapshot_dir / "restored.txt"
        self.snapshot_file.write_text("snapshot content", encoding="utf-8")

    def test_merge_without_owned_temp_dir_applies_changes_without_cleanup_error(self):
        diffs = [{
            "file": "restored.txt",
            "status": "NEW",
            "temp_path": str(self.snapshot_file),
        }]

        self.vault.apply_merge(diffs, None)

        self.assertEqual(
            (self.project / "restored.txt").read_text(encoding="utf-8"),
            "snapshot content",
        )
        self.assertTrue(self.snapshot_file.exists())

    def test_merge_removes_temp_dir_when_one_is_supplied(self):
        temp_dir = Path(tempfile.mkdtemp(dir=self.temp_root.name))
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        temp_file = temp_dir / "restored.txt"
        temp_file.write_text("snapshot content", encoding="utf-8")

        self.vault.apply_merge([{
            "file": "restored.txt",
            "status": "NEW",
            "temp_path": str(temp_file),
        }], str(temp_dir))

        self.assertFalse(temp_dir.exists())
        self.assertEqual(
            (self.project / "restored.txt").read_text(encoding="utf-8"),
            "snapshot content",
        )


if __name__ == "__main__":
    unittest.main()
