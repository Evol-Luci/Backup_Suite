"""Tests for BranchTreeVisualizer data transformation logic."""
import unittest
from unittest.mock import MagicMock
import datetime
import tkinter as tk
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def make_commit_name(dt, hash_str="abcd1234"):
    """Helper to create commit filename from datetime."""
    return dt.strftime("%Y-%m-%d_%H-%M-%S") + f"_{hash_str}.zip"


class TestColumnAssignment(unittest.TestCase):
    """Test that branches get correct column indices."""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def _make_visualizer(self, meta, commits_by_branch):
        """Create a BranchTreeVisualizer with mocked vault."""
        from branch_visualizer import BranchTreeVisualizer

        vault = MagicMock()
        vault._load_meta.return_value = meta
        vault.get_commits.side_effect = lambda b, include_inherited=False: commits_by_branch.get(b, [])
        vault.get_commit_metadata.return_value = {"message": "test", "timestamp": "2026-01-01T00:00:00"}

        viz = BranchTreeVisualizer(self.root, vault)
        return viz

    def test_single_branch_gets_column_zero(self):
        meta = {"branches": ["main"], "branch_origins": {}}
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))
        viz = self._make_visualizer(meta, {"main": [c1]})
        viz.refresh()
        self.assertEqual(viz.branch_columns["main"], 0)

    def test_child_branch_gets_column_one(self):
        meta = {
            "branches": ["main", "feature"],
            "branch_origins": {"feature": {"parent": "main", "commit": "c1.zip"}}
        }
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))
        c2 = make_commit_name(datetime.datetime(2026, 1, 2), "feat1234")
        viz = self._make_visualizer(meta, {"main": [c1], "feature": [c2]})
        viz.refresh()
        self.assertEqual(viz.branch_columns["main"], 0)
        self.assertEqual(viz.branch_columns["feature"], 1)

    def test_nested_branch_gets_column_two(self):
        meta = {
            "branches": ["main", "feature", "hotfix"],
            "branch_origins": {
                "feature": {"parent": "main", "commit": "c1.zip"},
                "hotfix": {"parent": "feature", "commit": "c2.zip"}
            }
        }
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))
        c2 = make_commit_name(datetime.datetime(2026, 1, 2), "feat1234")
        c3 = make_commit_name(datetime.datetime(2026, 1, 3), "hotf1234")
        viz = self._make_visualizer(meta, {"main": [c1], "feature": [c2], "hotfix": [c3]})
        viz.refresh()
        self.assertEqual(viz.branch_columns["main"], 0)
        self.assertEqual(viz.branch_columns["feature"], 1)
        self.assertEqual(viz.branch_columns["hotfix"], 2)

    def test_sibling_branches_get_different_columns(self):
        meta = {
            "branches": ["main", "feature-a", "feature-b"],
            "branch_origins": {
                "feature-a": {"parent": "main", "commit": "c1.zip"},
                "feature-b": {"parent": "main", "commit": "c2.zip"}
            }
        }
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))
        c2 = make_commit_name(datetime.datetime(2026, 1, 2), "aaaa1111")
        c3 = make_commit_name(datetime.datetime(2026, 1, 3), "bbbb2222")
        viz = self._make_visualizer(meta, {"main": [c1], "feature-a": [c2], "feature-b": [c3]})
        viz.refresh()
        self.assertEqual(viz.branch_columns["main"], 0)
        # Siblings should get different columns (both > 0)
        self.assertNotEqual(viz.branch_columns["feature-a"], viz.branch_columns["feature-b"])
        self.assertGreater(viz.branch_columns["feature-a"], 0)
        self.assertGreater(viz.branch_columns["feature-b"], 0)


class TestTimelineBuilding(unittest.TestCase):
    """Test that commits are ordered correctly on the shared Y-axis."""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self.root.destroy()

    def _make_visualizer(self, meta, commits_by_branch):
        from branch_visualizer import BranchTreeVisualizer

        vault = MagicMock()
        vault._load_meta.return_value = meta
        vault.get_commits.side_effect = lambda b, include_inherited=False: commits_by_branch.get(b, [])
        vault.get_commit_metadata.return_value = {"message": "test", "timestamp": "2026-01-01T00:00:00"}

        viz = BranchTreeVisualizer(self.root, vault)
        return viz

    def test_commits_sorted_oldest_first(self):
        meta = {"branches": ["main"], "branch_origins": {}}
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))
        c2 = make_commit_name(datetime.datetime(2026, 1, 2), "bbbb2222")
        # Pass newest first (as vault returns), visualizer should sort oldest first
        viz = self._make_visualizer(meta, {"main": [c2, c1]})
        viz.refresh()
        self.assertEqual(viz.timeline[0]["name"], c1)
        self.assertEqual(viz.timeline[1]["name"], c2)

    def test_interleaved_branch_commits_share_timeline(self):
        meta = {
            "branches": ["main", "feature"],
            "branch_origins": {"feature": {"parent": "main", "commit": "x.zip"}}
        }
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))            # main
        c2 = make_commit_name(datetime.datetime(2026, 1, 2), "feat111")  # feature
        c3 = make_commit_name(datetime.datetime(2026, 1, 3), "main222")  # main
        viz = self._make_visualizer(meta, {"main": [c3, c1], "feature": [c2]})
        viz.refresh()
        # Timeline should have all 3 in order
        self.assertEqual(len(viz.timeline), 3)
        self.assertEqual(viz.timeline[0]["branch"], "main")
        self.assertEqual(viz.timeline[1]["branch"], "feature")
        self.assertEqual(viz.timeline[2]["branch"], "main")

    def test_empty_branch_produces_no_timeline_entries(self):
        meta = {
            "branches": ["main", "empty-branch"],
            "branch_origins": {"empty-branch": {"parent": "main", "commit": "c1.zip"}}
        }
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))
        viz = self._make_visualizer(meta, {"main": [c1], "empty-branch": []})
        viz.refresh()
        self.assertEqual(len(viz.timeline), 1)
        self.assertEqual(viz.timeline[0]["branch"], "main")


if __name__ == "__main__":
    unittest.main()
