# Branch Tree Visualizer Vertical Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Redesign the branch tree visualizer from horizontal rows to vertical columns with proper branch ownership of commits, diverge/merge arrows, and tooltips.

**Architecture:** Rewrite `branch_visualizer.py` keeping the same `BranchTreeVisualizer(tk.Canvas)` public API. Data transformation builds a shared Y-axis timeline with per-branch column assignment. Canvas rendering draws vertical branch lines, commit dots in correct columns, and horizontal connector arrows at branch points. No changes to `backup_suite.pyw`.

**Tech Stack:** tkinter Canvas (existing), Python 3

---

## Context

**Current file:** `backup-suite/branch_visualizer.py` (227 lines)
**Integration:** `backup_suite.pyw` imports and uses this widget at lines 9, 1440, 1512-1513, 1552-1553, 1562, 1937-1938
**Data source:** `ProjectVault` provides `_load_meta()`, `get_commits(branch, include_inherited=False)`, `get_commit_metadata(filename, branch)`

**Public API that MUST be preserved (no changes to backup_suite.pyw):**
- `BranchTreeVisualizer(parent, vault, on_click_callback=None, **kwargs)` — constructor
- `refresh()` — rebuild and redraw
- `set_colors(theme_colors)` — update theme colors
- `vault` attribute — set externally to change project
- Callback signature: `on_click_callback(event_type, data)` where event_type is `"commit"` or `"branch"`

**Data model:**
- `meta.json` has `branches[]`, `branch_origins{}` with `{parent, commit}` per branch
- `get_commits(branch, include_inherited=False)` returns list of commit filenames (newest first)
- `get_commit_metadata(filename, branch)` returns `{message, timestamp, parent, tags}`
- Commit filenames: `YYYY-MM-DD_HH-MM-SS_hash.zip` (new) or `YYYY-MM-DD_HH-MM-SS__message.zip` (legacy)

---

### Task 1: Test Harness — Data Transformation Unit Tests

**Files:**
- Create: `backup-suite/tests/test_branch_visualizer.py`

We need tests for the data transformation logic (column assignment, timeline building) independent of Canvas rendering. We'll mock ProjectVault.

**Step 1: Create test file with fixtures and first test**

```python
"""Tests for BranchTreeVisualizer data transformation logic."""
import unittest
from unittest.mock import MagicMock, patch
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
        c1 = make_commit_name(datetime.datetime(2026, 1, 1))           # main
        c2 = make_commit_name(datetime.datetime(2026, 1, 2), "feat111")  # feature
        c3 = make_commit_name(datetime.datetime(2026, 1, 3), "main222")  # main
        viz = self._make_visualizer(meta, {"main": [c3, c1], "feature": [c2]})
        viz.refresh()
        # Timeline should have all 3 in order
        self.assertEqual(len(viz.timeline), 3)
        self.assertEqual(viz.timeline[0]["branch"], "main")
        self.assertEqual(viz.timeline[1]["branch"], "feature")
        self.assertEqual(viz.timeline[2]["branch"], "main")


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/lucievol/.local/share/solace/backup-suite && python -m pytest tests/test_branch_visualizer.py -v`

Expected: FAIL — `branch_columns` and `timeline` attributes don't exist yet on the visualizer.

**Step 3: Commit test harness**

```bash
cd /home/lucievol/.local/share/solace/backup-suite
git add tests/test_branch_visualizer.py
git commit -m "test: add unit tests for branch visualizer data transformation"
```

---

### Task 2: Rewrite Data Transformation in branch_visualizer.py

**Files:**
- Modify: `backup-suite/branch_visualizer.py`

Rewrite the `refresh()` method's data loading section to produce the new data structures: `branch_columns`, `timeline`, and `branch_map` with per-branch sorted commits. Keep the same constructor and `set_colors()` method.

**Step 1: Rewrite the data transformation section of refresh()**

Replace the entire file with the new implementation. Key data structures:

- `self.branch_columns = {}` — maps branch_name -> column index (0-based)
- `self.timeline = []` — all commits sorted oldest-first, each `{name, time, branch}`
- `self.branch_map = {}` — branch_name -> `{commits: [], origin: {...}}`

Column assignment algorithm:
1. Build a tree from `branch_origins` (each branch knows its parent)
2. Walk the tree depth-first: main=0, first child=1, grandchild=2, etc.
3. Multiple children of the same parent get incrementing column indices

**Step 2: Run tests to verify data transformation passes**

Run: `cd /home/lucievol/.local/share/solace/backup-suite && python -m pytest tests/test_branch_visualizer.py -v`

Expected: All tests PASS

**Step 3: Commit**

```bash
git add branch_visualizer.py
git commit -m "feat: rewrite branch visualizer data transformation for vertical layout"
```

---

### Task 3: Rewrite Canvas Rendering — Vertical Layout

**Files:**
- Modify: `backup-suite/branch_visualizer.py`

Replace the drawing passes in `refresh()` with the new vertical column layout.

**Step 1: Implement the new rendering logic**

Layout constants:
- `self.col_spacing = 80` — horizontal space between branch columns
- `self.row_spacing = 40` — vertical space between timeline rows
- `self.header_height = 40` — space for branch name headers at top
- `self.padding = 30` — canvas edge padding

Drawing passes:

**Pass 0 — Column headers:** For each branch, draw the branch name at the top of its column. Current branch in accent color + bold, others in regular text color.

**Pass 1 — Vertical branch lines:** For each branch, find its first and last commit in the timeline. Draw a solid vertical line from first commit Y to last commit Y in that branch's column.

**Pass 2 — Commit dots:** For each commit in the timeline, draw a filled circle at `(column_x, row_y)`. Tag with commit name for click binding.

**Pass 3 — Branch origin connectors:** For each branch with an origin, draw a horizontal arrow from the parent branch's column to the child branch's column at the Y position of the origin commit. Arrow points right (toward child).

**Pass 4 — Tooltips:** On hover, show a Canvas text item near the node with commit message + timestamp. On leave, delete it.

Coordinate helpers:
```python
def col_x(self, col_index):
    return self.padding + col_index * self.col_spacing

def row_y(self, row_index):
    return self.padding + self.header_height + row_index * self.row_spacing
```

**Step 2: Verify visually**

Run: `cd /home/lucievol/.local/share/solace/backup-suite && python backup_suite.pyw`

Load a project with multiple branches and verify:
- Branch names appear as column headers
- Commits flow top to bottom
- Each branch's commits appear in the correct column
- Origin arrows connect parent to child branch

**Step 3: Run unit tests still pass**

Run: `cd /home/lucievol/.local/share/solace/backup-suite && python -m pytest tests/test_branch_visualizer.py -v`

Expected: PASS

**Step 4: Commit**

```bash
git add branch_visualizer.py
git commit -m "feat: implement vertical column layout rendering for branch tree"
```

---

### Task 4: Tooltips and Hover Behavior

**Files:**
- Modify: `backup-suite/branch_visualizer.py`

**Step 1: Implement tooltip on hover**

Add tooltip functionality:
- `on_node_hover(name, branch)`: Query `vault.get_commit_metadata(name, branch)` to get message and timestamp. Create a Canvas text/rectangle near the node showing this info.
- `on_node_leave()`: Delete the tooltip items (tagged `"tooltip"`).

Tooltip appearance:
- Background rectangle with slight contrast from canvas bg
- Text showing: `"message\nYYYY-MM-DD HH:MM"`
- Positioned to the right of the node, offset by 15px
- If tooltip would go off-canvas right edge, position to the left instead

**Step 2: Test hover behavior visually**

Run the app, hover over commit nodes, verify tooltip appears and disappears cleanly.

**Step 3: Commit**

```bash
git add branch_visualizer.py
git commit -m "feat: add hover tooltips showing commit message and timestamp"
```

---

### Task 5: HEAD Indicator and Auto-Scroll

**Files:**
- Modify: `backup-suite/branch_visualizer.py`

**Step 1: Add HEAD indicator**

After drawing all commit dots, find the `head_commit` from `meta.json`. Draw a slightly larger or differently-colored ring around the HEAD commit node. Optionally add a small `"HEAD"` label next to it.

**Step 2: Add auto-scroll to bottom**

At the end of `refresh()`, after setting `scrollregion`, scroll to the bottom so the most recent commits are visible:

```python
self.update_idletasks()
self.yview_moveto(1.0)
```

**Step 3: Test visually**

Verify HEAD is visually distinct and the view scrolls to show recent commits on load.

**Step 4: Commit**

```bash
git add branch_visualizer.py
git commit -m "feat: add HEAD indicator and auto-scroll to latest commits"
```

---

### Task 6: Continuation Lines for Active Branches

**Files:**
- Modify: `backup-suite/branch_visualizer.py`

**Step 1: Draw continuation lines through empty timeline slots**

When a branch has commits at timeline positions 2 and 5, draw a dashed or lighter vertical line through positions 3 and 4 to show the branch is still active but has no commits at those time slots. This connects the dots visually.

Implementation: For each branch, iterate through the timeline. Between the branch's first and last commit, draw a continuous vertical line. The solid commit-to-commit segments are already drawn in Task 3. This task ensures the line is continuous even when other branches have commits in between.

**Step 2: Test with interleaved commits across branches**

Verify that branch lines look continuous and connected.

**Step 3: Commit**

```bash
git add branch_visualizer.py
git commit -m "feat: add continuation lines between commits on same branch"
```

---

### Task 7: Final Polish and Edge Cases

**Files:**
- Modify: `backup-suite/branch_visualizer.py`

**Step 1: Handle edge cases**

- Empty branches (no commits): Show branch header with "(empty)" label, no dots
- Single commit branch: Draw dot with no line
- Many branches: Ensure horizontal scrollbar works for wide graphs
- Legacy commit filenames (`__message` format): Already handled by timestamp parser

**Step 2: Current branch highlight**

Mark the current branch column header with the accent color and bold font. Other branches use regular text color.

**Step 3: Run all tests**

Run: `cd /home/lucievol/.local/share/solace/backup-suite && python -m pytest tests/test_branch_visualizer.py -v`

**Step 4: Visual testing with real data**

Load various projects with different branch configurations and verify everything renders correctly.

**Step 5: Commit**

```bash
git add branch_visualizer.py
git commit -m "feat: polish branch visualizer with edge cases and current branch highlight"
```

---

## Notes

**Merge visualization:** The current `ProjectVault` does not track merge metadata (which branch was merged into which). The `apply_merge` method performs file-level operations without recording merge provenance. Showing merge arrows (child ──> parent) would require adding merge tracking to `meta.json`. This is noted as a future enhancement — for now, only branch diverge points (origin arrows) are visualized.

**No changes to backup_suite.pyw:** The entire rewrite is contained within `branch_visualizer.py`. The public API is preserved exactly.
