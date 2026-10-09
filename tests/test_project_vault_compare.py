import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from project_vault import ProjectVault


class ProjectVaultCompareTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_root.cleanup)
        self.project = Path(self.temp_root.name) / "project"
        self.project.mkdir()
        self.vault = ProjectVault(str(self.project))
        self.snapshot_path = Path(self.vault.branches_dir) / "main" / "snapshot.zip"

    def test_same_size_content_change_is_reported_as_modified(self):
        (self.project / "sample.txt").write_bytes(b"current")
        with zipfile.ZipFile(self.snapshot_path, "w") as archive:
            archive.writestr("sample.txt", b"snapsh0")

        diffs, temp_dir = self.vault.compare_snapshot("snapshot.zip")
        self.addCleanup(shutil.rmtree, temp_dir)

        self.assertEqual(
            [item["status"] for item in diffs if item["file"] == "sample.txt"],
            ["MODIFIED"],
        )


if __name__ == "__main__":
    unittest.main()
