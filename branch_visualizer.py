
import tkinter as tk
from tkinter import ttk
from tkinter import font
import datetime
import os

class BranchTreeVisualizer(tk.Canvas):
    def __init__(self, parent, vault, on_click_callback=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.vault = vault
        self.on_click_callback = on_click_callback
        self.meta = None
        self.timeline = []          # All commits sorted oldest-first: [{name, time, branch}, ...]
        self.branch_map = {}        # branch_name -> {commits: [], origin: {...}}
        self.branch_columns = {}    # branch_name -> column index (0-based)
        self._tooltip_id = None     # Canvas item id for current tooltip
        self._tooltip_bg_id = None  # Canvas item id for tooltip background

        # Layout constants
        self.col_spacing = 80       # Horizontal space between branch columns
        self.row_spacing = 40       # Vertical space between timeline rows
        self.header_height = 40     # Space for branch name headers at top
        self.padding = 30           # Canvas edge padding
        self.node_radius = 6

        self.colors = {
            "line": "#888888",
            "node": "#0078D7",
            "node_hover": "#F0F0F0",
            "text": "#333333",
            "bg": "#FFFFFF",
            "head": "#FF6B35",
            "connector": "#AAAAAA",
            "tooltip_bg": "#F5F5F5",
        }

    def set_colors(self, theme_colors):
        self.colors["line"] = theme_colors.get("BORDER", "#888888")
        self.colors["node"] = theme_colors.get("ACCENT_COLOR", "#0078D7")
        self.colors["node_hover"] = theme_colors.get("BUTTON_ACTIVE", "#F0F0F0")
        self.colors["text"] = theme_colors.get("TEXT_COLOR", "#333333")
        self.colors["bg"] = theme_colors.get("LIST_BG", "#FFFFFF")
        self.configure(bg=self.colors["bg"])

    def col_x(self, col_index):
        """Get X coordinate for a column index."""
        return self.padding + col_index * self.col_spacing

    def row_y(self, row_index):
        """Get Y coordinate for a timeline row index."""
        return self.padding + self.header_height + row_index * self.row_spacing

    def _assign_columns(self, branches, origins):
        """Assign column indices to branches based on hierarchy depth.

        Main (no origin) gets column 0. Children are placed depth-first
        so the tree reads naturally left-to-right.
        """
        self.branch_columns = {}

        # Build children map
        children = {}  # parent -> [child_names in order]
        roots = []
        for b in branches:
            if b in origins and origins[b]:
                parent = origins[b]['parent']
                children.setdefault(parent, []).append(b)
            else:
                roots.append(b)

        # Depth-first assignment
        col = [0]  # mutable counter

        def assign(branch):
            self.branch_columns[branch] = col[0]
            col[0] += 1
            for child in children.get(branch, []):
                assign(child)

        for root in roots:
            assign(root)

        # Assign any orphaned branches not reached by the tree walk
        for b in branches:
            if b not in self.branch_columns:
                self.branch_columns[b] = col[0]
                col[0] += 1

    def _parse_commit_time(self, c_name):
        """Parse timestamp from commit filename."""
        try:
            if '__' in c_name:
                ts_str = c_name.split('__')[0]
            else:
                ts_str = c_name[:19]
            return datetime.datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
        except (ValueError, IndexError):
            return datetime.datetime.min

    def refresh(self):
        self.delete("all")
        if not self.vault:
            self.create_text(100, 50, text="No Project Loaded", fill=self.colors["text"])
            return

        try:
            # === DATA TRANSFORMATION ===

            self.meta = self.vault._load_meta()
            branches = self.meta.get('branches', [])
            origins = self.meta.get('branch_origins', {})
            current_branch = self.meta.get('current_branch', 'main')
            head_commit = self.meta.get('head_commit', None)

            # Assign columns
            self._assign_columns(branches, origins)

            # Build timeline and branch_map
            all_commits = []
            self.branch_map = {}

            for b_name in branches:
                b_commits = self.vault.get_commits(b_name, include_inherited=False)
                self.branch_map[b_name] = {
                    "commits": [],
                    "origin": origins.get(b_name)
                }

                for c_name in b_commits:
                    dt = self._parse_commit_time(c_name)
                    commit_obj = {"name": c_name, "time": dt, "branch": b_name}
                    all_commits.append(commit_obj)
                    self.branch_map[b_name]["commits"].append(commit_obj)

            # Sort timeline oldest-first
            all_commits.sort(key=lambda x: x['time'])
            self.timeline = all_commits

            # Build row index map: commit_name -> row_index
            row_map = {c['name']: i for i, c in enumerate(self.timeline)}

            if not self.timeline and not branches:
                self.create_text(100, 50, text="No commits yet", fill=self.colors["text"])
                return

            # === RENDERING ===

            num_cols = max(self.branch_columns.values(), default=0) + 1
            num_rows = len(self.timeline)

            # Pass 0: Branch labels at branch start points
            for b_name, b_data in self.branch_map.items():
                col_idx = self.branch_columns[b_name]
                x = self.col_x(col_idx)

                is_current = (b_name == current_branch)
                font_weight = "bold" if is_current else "normal"
                fill = self.colors["node"] if is_current else self.colors["text"]

                # Position label above the first commit on this branch
                sorted_commits = sorted(b_data['commits'], key=lambda c: c['time']) if b_data['commits'] else []
                if sorted_commits:
                    first_row = row_map[sorted_commits[0]['name']]
                    y = self.row_y(first_row) - self.node_radius - 14
                else:
                    # Empty branch — place label near where the origin arrow lands
                    y = self.padding + self.header_height

                label_id = self.create_text(
                    x, y, text=b_name, fill=fill, anchor="s",
                    font=("Segoe UI", 9, font_weight)
                )
                self.tag_bind(label_id, "<Enter>", lambda e: self.configure(cursor="hand2"))
                self.tag_bind(label_id, "<Leave>", lambda e: self.configure(cursor=""))
                self.tag_bind(label_id, "<Button-1>", lambda e, b=b_name: self.on_branch_click(b))

            # Pass 1: Vertical branch lines (continuous from first to last commit)
            for b_name, b_data in self.branch_map.items():
                commits = b_data['commits']
                if len(commits) < 2:
                    continue

                # Sort by time for line drawing
                sorted_commits = sorted(commits, key=lambda c: c['time'])
                col_idx = self.branch_columns[b_name]
                x = self.col_x(col_idx)

                first_row = row_map[sorted_commits[0]['name']]
                last_row = row_map[sorted_commits[-1]['name']]

                y_start = self.row_y(first_row)
                y_end = self.row_y(last_row)

                self.create_line(x, y_start, x, y_end,
                                fill=self.colors["line"], width=2, tags="branch_line")

            # Pass 2: Branch origin connectors (horizontal arrows)
            for b_name, b_data in self.branch_map.items():
                origin = b_data['origin']
                if not origin:
                    continue

                parent_name = origin['parent']
                origin_commit = origin['commit']

                if parent_name not in self.branch_columns:
                    continue

                parent_col = self.branch_columns[parent_name]
                child_col = self.branch_columns[b_name]
                parent_x = self.col_x(parent_col)
                child_x = self.col_x(child_col)

                # Find the Y position: use origin commit row, or first child commit row
                if origin_commit in row_map:
                    origin_y = self.row_y(row_map[origin_commit])
                elif b_data['commits']:
                    sorted_commits = sorted(b_data['commits'], key=lambda c: c['time'])
                    origin_y = self.row_y(row_map[sorted_commits[0]['name']])
                else:
                    # Empty branch — draw connector at parent's last commit level
                    parent_commits = self.branch_map.get(parent_name, {}).get('commits', [])
                    if parent_commits:
                        sorted_parent = sorted(parent_commits, key=lambda c: c['time'])
                        origin_y = self.row_y(row_map[sorted_parent[-1]['name']])
                    else:
                        continue

                # Draw horizontal arrow: parent ──────> child
                arrow_y = origin_y
                self.create_line(
                    parent_x + self.node_radius + 2, arrow_y,
                    child_x - self.node_radius - 2, arrow_y,
                    fill=self.colors["connector"], width=2,
                    arrow=tk.LAST, arrowshape=(8, 10, 4),
                    dash=(6, 3), tags="connector"
                )

            # Pass 3: Commit dots
            for commit in self.timeline:
                col_idx = self.branch_columns[commit['branch']]
                row_idx = row_map[commit['name']]
                x = self.col_x(col_idx)
                y = self.row_y(row_idx)
                r = self.node_radius

                is_head = (commit['name'] == head_commit)

                if is_head:
                    # Draw outer ring for HEAD
                    self.create_oval(
                        x - r - 3, y - r - 3, x + r + 3, y + r + 3,
                        outline=self.colors["head"], width=2, tags="head_ring"
                    )

                fill = self.colors["head"] if is_head else self.colors["node"]
                item_id = self.create_oval(
                    x - r, y - r, x + r, y + r,
                    fill=fill, outline=self.colors["bg"], width=1,
                    tags=("node", commit['name'])
                )

                # Bindings
                self.tag_bind(item_id, "<Enter>",
                    lambda e, name=commit['name'], b=commit['branch']: self.on_node_hover(name, b))
                self.tag_bind(item_id, "<Leave>", lambda e: self.on_node_leave())
                self.tag_bind(item_id, "<Button-1>",
                    lambda e, name=commit['name'], b=commit['branch']: self.on_node_click(name, b))

            # Pass 4: Empty branch labels
            for b_name, b_data in self.branch_map.items():
                if not b_data['commits']:
                    col_idx = self.branch_columns[b_name]
                    x = self.col_x(col_idx)
                    y = self.row_y(0) if num_rows > 0 else self.padding + self.header_height
                    self.create_text(
                        x, y, text="(empty)", fill=self.colors["text"],
                        anchor="n", font=("Segoe UI", 8, "italic")
                    )

            # Set scroll region
            max_x = self.col_x(num_cols - 1) + self.padding + 40
            max_y = self.row_y(max(num_rows - 1, 0)) + self.padding + 20
            self.configure(scrollregion=(0, 0, max_x, max_y))

            # Auto-scroll to bottom
            self.update_idletasks()
            self.yview_moveto(1.0)

        except Exception as e:
            self.create_text(50, 50, text=f"Error drawing tree: {e}", fill="red", anchor="nw")

    def on_node_hover(self, name, branch):
        """Show tooltip with commit message and timestamp."""
        self.configure(cursor="hand2")
        self.on_node_leave()  # Clear any existing tooltip

        # Get commit metadata
        try:
            meta = self.vault.get_commit_metadata(name, branch)
            message = meta.get('message', 'No message')
            timestamp = meta.get('timestamp', '')
            if timestamp:
                try:
                    dt = datetime.datetime.fromisoformat(timestamp)
                    timestamp = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError):
                    pass
            tooltip_text = f"{message}\n{timestamp}" if timestamp else message
        except Exception:
            tooltip_text = name

        # Find node position
        col_idx = self.branch_columns.get(branch, 0)
        row_idx = next((i for i, c in enumerate(self.timeline) if c['name'] == name), 0)
        x = self.col_x(col_idx) + self.node_radius + 12
        y = self.row_y(row_idx)

        # Draw tooltip
        self._tooltip_id = self.create_text(
            x, y, text=tooltip_text, fill=self.colors["text"],
            anchor="w", font=("Segoe UI", 8), tags="tooltip"
        )

        # Get text bounds and draw background behind it
        bbox = self.bbox(self._tooltip_id)
        if bbox:
            pad = 4
            self._tooltip_bg_id = self.create_rectangle(
                bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad,
                fill=self.colors["tooltip_bg"], outline=self.colors["line"],
                width=1, tags="tooltip"
            )
            self.tag_raise(self._tooltip_id)

    def on_node_leave(self):
        """Remove tooltip."""
        self.configure(cursor="")
        self.delete("tooltip")
        self._tooltip_id = None
        self._tooltip_bg_id = None

    def on_node_click(self, commit_name, branch_name):
        if self.on_click_callback:
            self.on_click_callback("commit", {"commit": commit_name, "branch": branch_name})

    def on_branch_click(self, branch_name):
        if self.on_click_callback:
            self.on_click_callback("branch", {"branch": branch_name})
