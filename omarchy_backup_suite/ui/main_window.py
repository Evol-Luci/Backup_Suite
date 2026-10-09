"""
GTK4 main window for Solace Backup Suite.
Provides three views: Automated Backups, Project Vault, and Settings.
"""
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib, Gio

import sys
import os
import json
import queue
import threading
import logging
import shutil
import zipfile
import subprocess
import glob
from datetime import datetime
from pathlib import Path

# Add backup-suite root to sys.path so we can import project_vault
_BACKUP_SUITE_DIR = Path(__file__).parent.parent.parent
if str(_BACKUP_SUITE_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKUP_SUITE_DIR))

try:
    from project_vault import ProjectVault
    VAULT_AVAILABLE = True
except ImportError:
    VAULT_AVAILABLE = False

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    APS_AVAILABLE = True
except ImportError:
    APS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIG_DIR = str(_BACKUP_SUITE_DIR / "Settings")
CONFIG_FILE = "backup_config.json"
CONFIG_PATH = os.path.join(CONFIG_DIR, CONFIG_FILE)
LOG_DIR = str(_BACKUP_SUITE_DIR / "Debug")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "backup_suite_gtk_debug.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    filemode='w'
)

BACKUP_CSS = """
window {
    background-color: #2d353b;
    color: #d3c6aa;
}
.sidebar {
    background-color: #272e33;
    border-right: 1px solid #475258;
    min-width: 160px;
}
.sidebar-title {
    font-size: 13px;
    font-weight: bold;
    color: #7fbbb3;
    padding: 16px 12px 8px 12px;
}
.nav-btn {
    background: none;
    border: none;
    border-radius: 0;
    padding: 10px 16px;
    color: #d3c6aa;
    font-size: 13px;
}
.nav-btn:hover {
    background-color: #3d484d;
}
.nav-btn.active {
    background-color: #475258;
    color: #7fbbb3;
    font-weight: bold;
}
.content-area {
    background-color: #2d353b;
    padding: 16px;
}
.card {
    background-color: #3d484d;
    border-radius: 6px;
    padding: 12px;
    margin-bottom: 8px;
}
.section-title {
    font-size: 16px;
    font-weight: bold;
    color: #d3c6aa;
    margin-bottom: 8px;
}
.dim-label {
    color: #859289;
    font-size: 12px;
}
button.suggested-action {
    background-color: #7fbbb3;
    color: #2d353b;
    border: none;
    border-radius: 4px;
    padding: 6px 12px;
    font-weight: bold;
}
button.suggested-action:hover {
    background-color: #9fc9c1;
}
button.destructive-action {
    background-color: #e67e80;
    color: #2d353b;
    border: none;
    border-radius: 4px;
    padding: 6px 12px;
}
button.destructive-action:hover {
    background-color: #ed9496;
}
.action-btn {
    background-color: #475258;
    color: #d3c6aa;
    border: none;
    border-radius: 4px;
    padding: 6px 10px;
}
.action-btn:hover {
    background-color: #555e63;
}
.log-view {
    background-color: #232a2e;
    color: #d3c6aa;
    font-family: monospace;
    font-size: 12px;
    border-radius: 4px;
    padding: 8px;
}
.job-list {
    background-color: #3d484d;
    border-radius: 4px;
}
.job-row {
    background-color: #3d484d;
    color: #d3c6aa;
    padding: 8px 12px;
    border-bottom: 1px solid #475258;
}
.job-row:selected {
    background-color: #475258;
    color: #7fbbb3;
}
.job-enabled {
    color: #a7c080;
}
.job-disabled {
    color: #859289;
}
entry {
    background-color: #475258;
    color: #d3c6aa;
    border: 1px solid #596560;
    border-radius: 4px;
    padding: 6px 8px;
}
entry:focus {
    border-color: #7fbbb3;
}
progressbar trough {
    background-color: #3d484d;
    border-radius: 4px;
    min-height: 8px;
}
progressbar progress {
    background-color: #7fbbb3;
    border-radius: 4px;
}
.snapshot-row {
    padding: 6px 8px;
    color: #d3c6aa;
    font-family: monospace;
    font-size: 12px;
}
.snapshot-row:selected {
    background-color: #475258;
    color: #7fbbb3;
}
.branch-badge {
    background-color: #4f5b62;
    color: #83c092;
    border-radius: 3px;
    padding: 2px 6px;
    font-size: 11px;
}
"""


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_default_config():
    return {
        "global_settings": {
            "default_volumes_to_keep": 3,
            "default_backup_base_name": "Backups_Py",
            "theme": "Dark"
        },
        "backup_jobs": []
    }


def load_config():
    if not os.path.exists(CONFIG_PATH):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = get_default_config()
        save_config(data)
        return data
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return get_default_config()


def save_config(config_data):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config_data, f, indent=4)
        return True
    except Exception as e:
        logging.error(f"Failed to save config: {e}")
        return False


# ---------------------------------------------------------------------------
# Backup engine helpers (Linux rsync)
# ---------------------------------------------------------------------------

def run_backup_job(job_details, global_settings, log_cb):
    """Run one backup job: rsync -> zip -> rotate. Calls log_cb with strings."""
    job_name = job_details['name']
    if not job_details.get('enabled', False):
        log_cb(f"[{job_name}] SKIPPED: Job is disabled.")
        return

    volumes_to_keep = job_details.get("volumes_to_keep_override") or global_settings.get('default_volumes_to_keep', 3)
    backup_folder = job_details['destination_base']
    os.makedirs(backup_folder, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    temp_copy_dir = os.path.join(backup_folder, f"Temp_{job_name}_{timestamp}")
    zip_file = os.path.join(backup_folder, f"{job_name}_{timestamp}.zip")

    # --- rsync ---
    log_cb(f"[{job_name}] Starting rsync...")
    src = job_details['source_dir']
    if not src.endswith(os.sep):
        src += os.sep
    exclusions = job_details.get('exclusions', [])
    cmd = ["rsync", "-av", "--delete", src, temp_copy_dir]
    for ex in exclusions:
        cmd.append(f"--exclude={ex}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log_cb(f"[{job_name}] rsync FAILED (exit {result.returncode}): {result.stderr.strip()}")
            return
        log_cb(f"[{job_name}] rsync OK")
    except FileNotFoundError:
        log_cb(f"[{job_name}] ERROR: rsync not found. Please install rsync.")
        return

    # --- zip ---
    log_cb(f"[{job_name}] Zipping...")
    try:
        with zipfile.ZipFile(zip_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(temp_copy_dir):
                for fname in files:
                    fp = os.path.join(root, fname)
                    zf.write(fp, os.path.relpath(fp, temp_copy_dir))
        log_cb(f"[{job_name}] Zip created: {zip_file}")
    except Exception as e:
        log_cb(f"[{job_name}] Zip FAILED: {e}")
        return
    finally:
        if os.path.exists(temp_copy_dir):
            shutil.rmtree(temp_copy_dir, ignore_errors=True)

    # --- rotate ---
    pattern = os.path.join(backup_folder, f"{job_name}_*.zip")
    all_backups = sorted(glob.glob(pattern))
    while len(all_backups) > volumes_to_keep:
        oldest = all_backups.pop(0)
        try:
            os.remove(oldest)
            log_cb(f"[{job_name}] Rotated out: {os.path.basename(oldest)}")
        except OSError:
            pass

    log_cb(f"[{job_name}] COMPLETED SUCCESSFULLY")


# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------

def show_error(parent, title, message):
    dialog = Gtk.AlertDialog()
    dialog.set_message(title)
    dialog.set_detail(message)
    dialog.show(parent)


def show_info(parent, title, message):
    dialog = Gtk.AlertDialog()
    dialog.set_message(title)
    dialog.set_detail(message)
    dialog.show(parent)


# ---------------------------------------------------------------------------
# Job Editor Dialog
# ---------------------------------------------------------------------------

class JobEditorDialog(Gtk.Dialog):
    """Add or edit a backup job."""

    def __init__(self, parent, config, job_data=None):
        super().__init__(transient_for=parent, modal=True, title="Add / Edit Backup Job")
        self.set_default_size(600, 480)
        self.config = config
        self.original_job = job_data
        self.result = None

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(16)
        content.set_margin_bottom(8)
        content.set_margin_start(16)
        content.set_margin_end(16)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        content.append(grid)

        row = 0

        # Job name
        grid.attach(Gtk.Label(label="Job Name:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.name_entry = Gtk.Entry()
        self.name_entry.set_hexpand(True)
        grid.attach(self.name_entry, 1, row, 2, 1)
        row += 1

        # Source dir
        grid.attach(Gtk.Label(label="Source Directory:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.source_entry = Gtk.Entry()
        self.source_entry.set_hexpand(True)
        grid.attach(self.source_entry, 1, row, 1, 1)
        src_btn = Gtk.Button(label="Browse...")
        src_btn.connect("clicked", self._browse_source)
        grid.attach(src_btn, 2, row, 1, 1)
        row += 1

        # Destination base
        grid.attach(Gtk.Label(label="Destination Base:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.dest_entry = Gtk.Entry()
        self.dest_entry.set_hexpand(True)
        grid.attach(self.dest_entry, 1, row, 1, 1)
        dest_btn = Gtk.Button(label="Browse...")
        dest_btn.connect("clicked", self._browse_dest)
        grid.attach(dest_btn, 2, row, 1, 1)
        row += 1

        # Exclusions
        grid.attach(Gtk.Label(label="Exclusions\n(one per line):", halign=Gtk.Align.END, valign=Gtk.Align.START), 0, row, 1, 1)
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(100)
        scroll.set_vexpand(True)
        self.exclusions_buf = Gtk.TextBuffer()
        excl_view = Gtk.TextView(buffer=self.exclusions_buf, wrap_mode=Gtk.WrapMode.WORD)
        scroll.set_child(excl_view)
        grid.attach(scroll, 1, row, 2, 1)
        row += 1

        # Backups to keep
        grid.attach(Gtk.Label(label="Backups to Keep:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.volumes_spin = Gtk.SpinButton()
        self.volumes_spin.set_range(0, 99)
        self.volumes_spin.set_increments(1, 5)
        self.volumes_spin.set_value(0)
        grid.attach(self.volumes_spin, 1, row, 1, 1)
        grid.attach(Gtk.Label(label="(0 = global default)", halign=Gtk.Align.START), 2, row, 1, 1)
        row += 1

        # Enabled
        self.enabled_check = Gtk.CheckButton(label="Enabled")
        self.enabled_check.set_active(True)
        grid.attach(self.enabled_check, 1, row, 2, 1)
        row += 1

        # Schedule
        grid.attach(Gtk.Label(label="Schedule:", halign=Gtk.Align.END), 0, row, 1, 1)
        self.schedule_entry = Gtk.Entry()
        self.schedule_entry.set_text("manual")
        self.schedule_entry.set_placeholder_text("manual | daily@HH:MM | interval@minutes | weekly@day:HH:MM")
        self.schedule_entry.set_hexpand(True)
        grid.attach(self.schedule_entry, 1, row, 2, 1)
        row += 1

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.response(Gtk.ResponseType.CANCEL))
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save)
        btn_box.append(cancel_btn)
        btn_box.append(save_btn)
        content.append(btn_box)

        if job_data:
            self._populate(job_data)

    def _populate(self, job):
        self.name_entry.set_text(job.get("name", ""))
        self.source_entry.set_text(job.get("source_dir", ""))
        self.dest_entry.set_text(job.get("destination_base", ""))
        self.enabled_check.set_active(job.get("enabled", True))
        self.schedule_entry.set_text(job.get("schedule", "manual"))
        self.volumes_spin.set_value(job.get("volumes_to_keep_override", 0))
        excl = "\n".join(job.get("exclusions", []))
        self.exclusions_buf.set_text(excl)

    def _browse_source(self, btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Source Directory")
        dialog.select_folder(self.get_transient_for(), None, self._on_source_selected)

    def _on_source_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                self.source_entry.set_text(folder.get_path())
        except Exception:
            pass

    def _browse_dest(self, btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Destination Base Directory")
        dialog.select_folder(self.get_transient_for(), None, self._on_dest_selected)

    def _on_dest_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                self.dest_entry.set_text(folder.get_path())
        except Exception:
            pass

    def _on_save(self, btn):
        name = self.name_entry.get_text().strip()
        source = self.source_entry.get_text().strip()
        dest = self.dest_entry.get_text().strip()

        if not name or not source or not dest:
            show_error(self, "Validation Error", "Name, Source, and Destination cannot be empty.")
            return

        # Check name uniqueness
        existing_names = [j['name'] for j in self.config.get('backup_jobs', [])]
        original_name = self.original_job.get('name') if self.original_job else None
        if name in existing_names and name != original_name:
            show_error(self, "Validation Error", f"Job name '{name}' already exists.")
            return

        start, end = self.exclusions_buf.get_bounds()
        excl_text = self.exclusions_buf.get_text(start, end, False)
        exclusions = [ln.strip() for ln in excl_text.splitlines() if ln.strip()]

        volumes = int(self.volumes_spin.get_value())
        details = {
            "name": name,
            "source_dir": source,
            "destination_base": dest,
            "exclusions": exclusions,
            "enabled": self.enabled_check.get_active(),
            "schedule": self.schedule_entry.get_text().strip() or "manual",
        }
        if volumes > 0:
            details["volumes_to_keep_override"] = volumes

        self.result = details
        self.response(Gtk.ResponseType.OK)


# ---------------------------------------------------------------------------
# Snapshot Explorer Dialog (for vault)
# ---------------------------------------------------------------------------

class SnapshotDiffDialog(Gtk.Dialog):
    """Shows diff between project and a snapshot, allows merge."""

    def __init__(self, parent, vault, snapshot_name, log_cb):
        super().__init__(transient_for=parent, modal=True, title=f"Compare / Merge — {snapshot_name[:20]}")
        self.set_default_size(800, 500)
        self.vault = vault
        self.snapshot_name = snapshot_name
        self.log_cb = log_cb
        self.diffs = []
        self.temp_dir = None

        content = self.get_content_area()
        content.set_spacing(8)

        # Split pane: list on left, details on right
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_vexpand(True)
        paned.set_hexpand(True)
        content.append(paned)

        # Left: diff list
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        left_box.set_margin_top(8)
        left_box.set_margin_start(8)
        left_box.set_margin_bottom(4)

        list_label = Gtk.Label(label="Changed Files", halign=Gtk.Align.START)
        list_label.add_css_class("section-title")
        left_box.append(list_label)

        scroll_left = Gtk.ScrolledWindow()
        scroll_left.set_vexpand(True)
        scroll_left.set_min_content_width(300)

        self.diff_list = Gtk.ListBox()
        self.diff_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        scroll_left.set_child(self.diff_list)
        left_box.append(scroll_left)
        paned.set_start_child(left_box)

        # Right: info area
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right_box.set_margin_top(8)
        right_box.set_margin_start(4)
        right_box.set_margin_end(8)
        right_box.set_margin_bottom(4)

        info_label = Gtk.Label(label="Diff Summary", halign=Gtk.Align.START)
        info_label.add_css_class("section-title")
        right_box.append(info_label)

        self.summary_label = Gtk.Label(label="Loading...", halign=Gtk.Align.START, wrap=True)
        self.summary_label.add_css_class("dim-label")
        right_box.append(self.summary_label)

        paned.set_end_child(right_box)
        paned.set_position(350)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END, margin_top=8, margin_end=8, margin_bottom=8)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", self._on_close)

        merge_btn = Gtk.Button(label="Merge Selected Files")
        merge_btn.add_css_class("suggested-action")
        merge_btn.connect("clicked", self._on_merge)

        btn_box.append(close_btn)
        btn_box.append(merge_btn)
        content.append(btn_box)

        self.connect("close-request", lambda _: self._cleanup() or False)

        # Load diffs in thread
        threading.Thread(target=self._load_diffs, daemon=True).start()

    def _load_diffs(self):
        try:
            diffs, temp_dir = self.vault.compare_snapshot(self.snapshot_name)
            self.diffs = diffs
            self.temp_dir = temp_dir
            GLib.idle_add(self._populate_list)
        except Exception as e:
            self.log_cb(f"Compare FAILED: {e}")
            GLib.idle_add(self.summary_label.set_text, f"Error: {e}")

    def _populate_list(self):
        new_c = sum(1 for d in self.diffs if d['status'] == 'NEW')
        mod_c = sum(1 for d in self.diffs if d['status'] == 'MODIFIED')
        miss_c = sum(1 for d in self.diffs if d['status'] == 'MISSING')
        self.summary_label.set_text(
            f"{len(self.diffs)} differences found:\n"
            f"  NEW: {new_c}  MODIFIED: {mod_c}  MISSING: {miss_c}"
        )
        for diff in self.diffs:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_top(4)
            row.set_margin_bottom(4)
            row.set_margin_start(8)
            row.set_margin_end(8)

            status_colors = {'NEW': '#a7c080', 'MODIFIED': '#dbbc7f', 'MISSING': '#e67e80'}
            status_lbl = Gtk.Label(label=diff['status'])
            status_lbl.set_markup(
                f"<span foreground='{status_colors.get(diff['status'], '#d3c6aa')}'><b>{diff['status']}</b></span>"
            )
            status_lbl.set_size_request(80, -1)
            row.append(status_lbl)

            file_lbl = Gtk.Label(label=diff['file'], halign=Gtk.Align.START)
            file_lbl.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
            file_lbl.set_hexpand(True)
            row.append(file_lbl)

            lb_row = Gtk.ListBoxRow()
            lb_row.set_child(row)
            lb_row._diff_item = diff
            self.diff_list.append(lb_row)

    def _on_merge(self, btn):
        selected_rows = []
        row = self.diff_list.get_row_at_index(0)
        idx = 0
        while row is not None:
            if row.is_selected():
                selected_rows.append(row)
            idx += 1
            row = self.diff_list.get_row_at_index(idx)

        if not selected_rows:
            show_info(self, "No Selection", "Please select files to merge.")
            return

        to_merge = [r._diff_item for r in selected_rows]

        def do_merge():
            try:
                self.vault.apply_merge(to_merge, None)
                self.log_cb(f"Merge complete: {len(to_merge)} file(s) applied")
                GLib.idle_add(self._on_close, None)
            except Exception as e:
                self.log_cb(f"Merge FAILED: {e}")

        threading.Thread(target=do_merge, daemon=True).start()

    def _cleanup(self):
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
            except Exception:
                pass

    def _on_close(self, _):
        self._cleanup()
        self.close()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class BackupMainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Solace Backup Suite")
        self.set_default_size(960, 640)

        # Apply CSS
        provider = Gtk.CssProvider()
        provider.load_from_data(BACKUP_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.config = load_config()
        self.log_queue = queue.Queue()
        self.current_vault = None
        self._commit_snapshots = []  # maps list index -> snapshot filename
        self.scheduler = None
        if APS_AVAILABLE:
            self.scheduler = BackgroundScheduler(daemon=True)

        # Build layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_child(main_box)

        # Sidebar
        sidebar = self._build_sidebar()
        main_box.append(sidebar)

        # Content stack
        self.stack = Gtk.Stack()
        self.stack.add_css_class("content-area")
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        main_box.append(self.stack)

        # Build views
        self._build_backups_view()
        self._build_vault_view()
        self._build_settings_view()

        # Start on backups
        self._switch_view("backups")

        # Start log queue processor
        GLib.timeout_add(150, self._process_log_queue)

        # Start scheduler
        if self.scheduler:
            try:
                self._load_all_jobs_to_scheduler()
                self.scheduler.start()
                self._log("Scheduler started.")
            except Exception as e:
                self._log(f"Scheduler error: {e}")
        else:
            self._log("Scheduler not available (install apscheduler).")

        self._log("Solace Backup Suite (GTK4) ready.")

    # -----------------------------------------------------------------------
    # Sidebar
    # -----------------------------------------------------------------------

    def _build_sidebar(self):
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.add_css_class("sidebar")

        title = Gtk.Label(label="Backup Suite")
        title.add_css_class("sidebar-title")
        title.set_halign(Gtk.Align.START)
        sidebar.append(title)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar.append(sep)

        self.nav_buttons = {}
        nav_items = [
            ("backups", "Backups"),
            ("vault", "Project Vault"),
            ("settings", "Settings"),
        ]
        for name, label in nav_items:
            btn = Gtk.Button(label=label)
            btn.add_css_class("nav-btn")
            btn.set_halign(Gtk.Align.FILL)
            btn.connect("clicked", self._on_nav_clicked, name)
            sidebar.append(btn)
            self.nav_buttons[name] = btn

        return sidebar

    def _on_nav_clicked(self, btn, name):
        self._switch_view(name)

    def _switch_view(self, name):
        for n, b in self.nav_buttons.items():
            if n == name:
                b.add_css_class("active")
            else:
                b.remove_css_class("active")
        self.stack.set_visible_child_name(name)

    # -----------------------------------------------------------------------
    # Backups View
    # -----------------------------------------------------------------------

    def _build_backups_view(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_margin_bottom(16)

        # Header
        hdr = Gtk.Label(label="Automated Backups")
        hdr.add_css_class("section-title")
        hdr.set_halign(Gtk.Align.START)
        outer.append(hdr)

        # Split pane: job list | log
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_vexpand(True)
        outer.append(paned)

        # LEFT: job list
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_margin_end(8)

        list_lbl = Gtk.Label(label="Backup Jobs", halign=Gtk.Align.START)
        list_lbl.add_css_class("dim-label")
        left_box.append(list_lbl)

        scroll_jobs = Gtk.ScrolledWindow()
        scroll_jobs.set_vexpand(True)
        scroll_jobs.set_min_content_width(240)
        self.job_list = Gtk.ListBox()
        self.job_list.add_css_class("job-list")
        self.job_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroll_jobs.set_child(self.job_list)
        left_box.append(scroll_jobs)

        # Job action buttons
        job_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("action-btn")
        add_btn.connect("clicked", self._on_add_job)
        edit_btn = Gtk.Button(label="Edit")
        edit_btn.add_css_class("action-btn")
        edit_btn.connect("clicked", self._on_edit_job)
        remove_btn = Gtk.Button(label="Remove")
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect("clicked", self._on_remove_job)
        job_btn_box.append(add_btn)
        job_btn_box.append(edit_btn)
        job_btn_box.append(remove_btn)
        left_box.append(job_btn_box)

        paned.set_start_child(left_box)
        paned.set_position(280)

        # RIGHT: log
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        log_lbl = Gtk.Label(label="Activity Log", halign=Gtk.Align.START)
        log_lbl.add_css_class("dim-label")
        right_box.append(log_lbl)

        scroll_log = Gtk.ScrolledWindow()
        scroll_log.set_vexpand(True)
        self.log_buf = Gtk.TextBuffer()
        log_view = Gtk.TextView(buffer=self.log_buf, editable=False, cursor_visible=False,
                                wrap_mode=Gtk.WrapMode.WORD)
        log_view.add_css_class("log-view")
        scroll_log.set_child(log_view)
        right_box.append(scroll_log)
        self._log_scroll = scroll_log
        self._log_view = log_view

        paned.set_end_child(right_box)

        # Bottom action bar
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_margin_top(8)

        run_sel_btn = Gtk.Button(label="Run Selected")
        run_sel_btn.add_css_class("suggested-action")
        run_sel_btn.connect("clicked", self._on_run_selected)
        action_bar.append(run_sel_btn)

        run_all_btn = Gtk.Button(label="Run All Enabled")
        run_all_btn.add_css_class("action-btn")
        run_all_btn.connect("clicked", self._on_run_all)
        action_bar.append(run_all_btn)

        # Progress bar
        self.backup_progress = Gtk.ProgressBar()
        self.backup_progress.set_hexpand(True)
        self.backup_progress.set_visible(False)
        action_bar.append(self.backup_progress)

        # Status label
        self.status_label = Gtk.Label(label="Status: Idle", halign=Gtk.Align.END)
        self.status_label.add_css_class("dim-label")
        action_bar.append(self.status_label)

        outer.append(action_bar)

        self.stack.add_named(outer, "backups")
        self._populate_job_list()

    def _populate_job_list(self):
        # Clear existing rows
        while True:
            row = self.job_list.get_row_at_index(0)
            if row is None:
                break
            self.job_list.remove(row)

        for job in self.config.get('backup_jobs', []):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            row_box.set_margin_start(10)
            row_box.set_margin_end(10)

            name_lbl = Gtk.Label(label=job['name'], halign=Gtk.Align.START)
            name_lbl.set_hexpand(True)
            row_box.append(name_lbl)

            enabled = job.get('enabled', False)
            sched = job.get('schedule', 'manual')
            info_lbl = Gtk.Label(label=f"{'ON' if enabled else 'OFF'} | {sched}",
                                 halign=Gtk.Align.END)
            info_lbl.add_css_class("job-enabled" if enabled else "job-disabled")
            row_box.append(info_lbl)

            lb_row = Gtk.ListBoxRow()
            lb_row.set_child(row_box)
            lb_row._job_data = job
            self.job_list.append(lb_row)

    def _get_selected_job(self):
        row = self.job_list.get_selected_row()
        if row and hasattr(row, '_job_data'):
            return row._job_data
        return None

    def _get_selected_job_index(self):
        row = self.job_list.get_selected_row()
        if row is None:
            return -1
        idx = 0
        r = self.job_list.get_row_at_index(0)
        while r is not None:
            if r == row:
                return idx
            idx += 1
            r = self.job_list.get_row_at_index(idx)
        return -1

    def _on_add_job(self, btn):
        dialog = JobEditorDialog(self, self.config)
        dialog.connect("response", self._on_job_editor_response, None)
        dialog.present()

    def _on_edit_job(self, btn):
        job = self._get_selected_job()
        if not job:
            show_info(self, "No Selection", "Please select a job to edit.")
            return
        dialog = JobEditorDialog(self, self.config, job_data=job)
        dialog.connect("response", self._on_job_editor_response, job)
        dialog.present()

    def _on_job_editor_response(self, dialog, response, original_job):
        if response == Gtk.ResponseType.OK and dialog.result:
            details = dialog.result
            if original_job:
                # Edit existing
                jobs = self.config['backup_jobs']
                for i, j in enumerate(jobs):
                    if j['name'] == original_job['name']:
                        jobs[i] = details
                        break
            else:
                self.config['backup_jobs'].append(details)
            save_config(self.config)
            self._populate_job_list()
            self._log(f"Job '{details['name']}' saved.")
            if self.scheduler and APS_AVAILABLE:
                self._schedule_job(details)
        dialog.destroy()

    def _on_remove_job(self, btn):
        job = self._get_selected_job()
        if not job:
            show_info(self, "No Selection", "Please select a job to remove.")
            return
        # Confirm dialog
        confirm = Gtk.AlertDialog()
        confirm.set_message(f"Remove job '{job['name']}'?")
        confirm.set_detail("This only removes the job definition, not any backup files.")
        confirm.set_buttons(["Cancel", "Remove"])
        confirm.set_cancel_button(0)
        confirm.set_default_button(1)
        confirm.choose(self, None, lambda d, r: self._do_remove_job(d, r, job))

    def _do_remove_job(self, dialog, result, job):
        try:
            idx = dialog.choose_finish(result)
            if idx == 1:
                self.config['backup_jobs'] = [j for j in self.config['backup_jobs'] if j['name'] != job['name']]
                save_config(self.config)
                self._populate_job_list()
                self._log(f"Job '{job['name']}' removed.")
        except Exception:
            pass

    def _on_run_selected(self, btn):
        job = self._get_selected_job()
        if not job:
            show_info(self, "No Selection", "Please select a job to run.")
            return
        self._run_jobs([job])

    def _on_run_all(self, btn):
        jobs = [j for j in self.config.get('backup_jobs', []) if j.get('enabled', False)]
        if not jobs:
            show_info(self, "No Jobs", "No enabled backup jobs found.")
            return
        self._run_jobs(jobs)

    def _run_jobs(self, jobs):
        self.backup_progress.set_visible(True)
        self.backup_progress.set_fraction(0)
        self.status_label.set_text(f"Status: Running {len(jobs)} job(s)...")

        def worker():
            for i, job in enumerate(jobs):
                GLib.idle_add(self.status_label.set_text, f"Status: Running {job['name']}...")
                GLib.idle_add(self.backup_progress.set_fraction, i / len(jobs))
                run_backup_job(job, self.config.get('global_settings', {}), self._log_safe)

            GLib.idle_add(self.status_label.set_text, "Status: Idle")
            GLib.idle_add(self.backup_progress.set_fraction, 1.0)
            GLib.idle_add(lambda: GLib.timeout_add(2000, self._hide_progress))

        threading.Thread(target=worker, daemon=True).start()

    def _hide_progress(self):
        self.backup_progress.set_visible(False)
        self.backup_progress.set_fraction(0)
        return False  # Don't repeat

    # -----------------------------------------------------------------------
    # Scheduler
    # -----------------------------------------------------------------------

    def _schedule_job(self, job):
        if not self.scheduler:
            return
        job_id = job['name']
        schedule_str = job.get('schedule', 'manual').lower().strip()
        enabled = job.get('enabled', False)

        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        if not enabled or schedule_str == 'manual':
            return

        trigger = None
        try:
            if schedule_str.startswith('daily@'):
                h, m = map(int, schedule_str.split('@')[1].split(':'))
                trigger = CronTrigger(hour=h, minute=m)
            elif schedule_str.startswith('interval@'):
                mins = int(schedule_str.split('@')[1])
                trigger = IntervalTrigger(minutes=mins)
            elif schedule_str.startswith('weekly@'):
                parts = schedule_str.split('@')[1].split(':')
                d = parts[0][:3].lower()
                h, m = int(parts[1]), int(parts[2])
                trigger = CronTrigger(day_of_week=d, hour=h, minute=m)
        except Exception as e:
            self._log(f"Schedule parse error for '{job_id}': {e}")

        if trigger:
            self.scheduler.add_job(
                run_backup_job,
                trigger,
                id=job_id,
                args=[job, self.config.get('global_settings', {}), self._log_safe],
                replace_existing=True,
                misfire_grace_time=3600
            )
            self._log(f"Scheduled '{job_id}': {schedule_str}")

    def _load_all_jobs_to_scheduler(self):
        for job in self.config.get('backup_jobs', []):
            self._schedule_job(job)

    # -----------------------------------------------------------------------
    # Project Vault View
    # -----------------------------------------------------------------------

    def _build_vault_view(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_top(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_margin_bottom(16)

        # Header row
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr = Gtk.Label(label="Project Vault")
        hdr.add_css_class("section-title")
        hdr.set_halign(Gtk.Align.START)
        hdr.set_hexpand(True)
        hdr_box.append(hdr)
        outer.append(hdr_box)

        if not VAULT_AVAILABLE:
            warn = Gtk.Label(label="project_vault.py not found. Vault features unavailable.")
            warn.add_css_class("dim-label")
            outer.append(warn)
            self.stack.add_named(outer, "vault")
            return

        # Project control bar
        ctrl_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ctrl_card.add_css_class("card")

        path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        path_lbl = Gtk.Label(label="Project:")
        path_lbl.add_css_class("dim-label")
        path_row.append(path_lbl)
        self.vault_path_label = Gtk.Label(label="No project loaded", halign=Gtk.Align.START)
        self.vault_path_label.set_hexpand(True)
        self.vault_path_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        path_row.append(self.vault_path_label)
        load_btn = Gtk.Button(label="Load / Init Project...")
        load_btn.add_css_class("action-btn")
        load_btn.connect("clicked", self._on_load_vault_project)
        path_row.append(load_btn)
        ctrl_card.append(path_row)

        branch_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        branch_lbl = Gtk.Label(label="Branch:")
        branch_lbl.add_css_class("dim-label")
        branch_row.append(branch_lbl)
        self.vault_branch_label = Gtk.Label(label="--", halign=Gtk.Align.START)
        self.vault_branch_label.add_css_class("branch-badge")
        branch_row.append(self.vault_branch_label)

        new_branch_btn = Gtk.Button(label="New Branch...")
        new_branch_btn.add_css_class("action-btn")
        new_branch_btn.connect("clicked", self._on_new_branch)
        branch_row.append(new_branch_btn)

        switch_branch_btn = Gtk.Button(label="Switch Branch...")
        switch_branch_btn.add_css_class("action-btn")
        switch_branch_btn.connect("clicked", self._on_switch_branch)
        branch_row.append(switch_branch_btn)

        del_branch_btn = Gtk.Button(label="Delete Branch...")
        del_branch_btn.add_css_class("destructive-action")
        del_branch_btn.connect("clicked", self._on_delete_branch)
        branch_row.append(del_branch_btn)

        export_btn = Gtk.Button(label="Export Branch...")
        export_btn.add_css_class("action-btn")
        export_btn.connect("clicked", self._on_export_branch)
        branch_row.append(export_btn)

        ctrl_card.append(branch_row)
        outer.append(ctrl_card)

        # Split: snapshot list | vault log
        split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, wide_handle=True)
        split.set_vexpand(True)
        outer.append(split)

        # Top: snapshot history
        snap_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        snap_lbl = Gtk.Label(label="Snapshot History (current branch)", halign=Gtk.Align.START)
        snap_lbl.add_css_class("dim-label")
        snap_box.append(snap_lbl)

        snap_scroll = Gtk.ScrolledWindow()
        snap_scroll.set_vexpand(True)
        self.snapshot_list = Gtk.ListBox()
        self.snapshot_list.add_css_class("job-list")
        self.snapshot_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        snap_scroll.set_child(self.snapshot_list)
        snap_box.append(snap_scroll)
        split.set_start_child(snap_box)
        split.set_position(260)

        # Bottom: vault log
        vlog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vlog_lbl = Gtk.Label(label="Vault Activity Log", halign=Gtk.Align.START)
        vlog_lbl.add_css_class("dim-label")
        vlog_box.append(vlog_lbl)

        vlog_scroll = Gtk.ScrolledWindow()
        vlog_scroll.set_vexpand(True)
        vlog_scroll.set_min_content_height(100)
        self.vault_log_buf = Gtk.TextBuffer()
        vlog_view = Gtk.TextView(buffer=self.vault_log_buf, editable=False, cursor_visible=False,
                                 wrap_mode=Gtk.WrapMode.WORD)
        vlog_view.add_css_class("log-view")
        vlog_scroll.set_child(vlog_view)
        vlog_box.append(vlog_scroll)
        self._vault_log_scroll = vlog_scroll
        split.set_end_child(vlog_box)

        # Commit action bar
        commit_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        commit_bar.set_margin_top(8)

        msg_lbl = Gtk.Label(label="Commit Message:")
        msg_lbl.add_css_class("dim-label")
        commit_bar.append(msg_lbl)

        self.vault_msg_entry = Gtk.Entry()
        self.vault_msg_entry.set_placeholder_text("Describe this snapshot...")
        self.vault_msg_entry.set_hexpand(True)
        self.vault_msg_entry.connect("activate", lambda e: self._on_commit_snapshot(None))
        commit_bar.append(self.vault_msg_entry)

        self.commit_btn = Gtk.Button(label="Commit Snapshot")
        self.commit_btn.add_css_class("suggested-action")
        self.commit_btn.set_sensitive(False)
        self.commit_btn.connect("clicked", self._on_commit_snapshot)
        commit_bar.append(self.commit_btn)

        self.compare_btn = Gtk.Button(label="Compare / Merge...")
        self.compare_btn.add_css_class("action-btn")
        self.compare_btn.set_sensitive(False)
        self.compare_btn.connect("clicked", self._on_compare_snapshot)
        commit_bar.append(self.compare_btn)

        self.restore_btn = Gtk.Button(label="Full Restore...")
        self.restore_btn.add_css_class("destructive-action")
        self.restore_btn.set_sensitive(False)
        self.restore_btn.connect("clicked", self._on_full_restore)
        commit_bar.append(self.restore_btn)

        self.explore_btn = Gtk.Button(label="Explore...")
        self.explore_btn.add_css_class("action-btn")
        self.explore_btn.set_sensitive(False)
        self.explore_btn.connect("clicked", self._on_explore_snapshot)
        commit_bar.append(self.explore_btn)

        outer.append(commit_bar)

        self.stack.add_named(outer, "vault")

    def _on_load_vault_project(self, btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Project Folder")
        dialog.select_folder(self, None, self._on_vault_folder_selected)

    def _on_vault_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if not folder:
                return
            path = folder.get_path()
            self._vault_log(f"Loading project: {path}")
            self.current_vault = ProjectVault(path)
            self.vault_path_label.set_text(path)

            meta = self.current_vault._load_meta()
            current_branch = meta.get('current_branch', 'main')
            branch_count = len(meta.get('branches', []))
            commits = self.current_vault.get_commits(include_inherited=True)
            self._vault_log(f"Vault initialized: {branch_count} branch(es), {len(commits)} commit(s)")
            self._vault_log(f"Active branch: {current_branch}")

            self._refresh_vault_ui()
            self.commit_btn.set_sensitive(True)
            self.compare_btn.set_sensitive(True)
            self.restore_btn.set_sensitive(True)
            self.explore_btn.set_sensitive(True)
        except Exception as e:
            self._vault_log(f"ERROR loading vault: {e}")
            show_error(self, "Load Error", str(e))

    def _refresh_vault_ui(self):
        if not self.current_vault:
            return
        meta = self.current_vault._load_meta()
        current_branch = meta.get('current_branch', 'main')
        self.vault_branch_label.set_text(current_branch)

        # Clear snapshot list
        while True:
            row = self.snapshot_list.get_row_at_index(0)
            if row is None:
                break
            self.snapshot_list.remove(row)

        self._commit_snapshots = []
        commits = self.current_vault.get_commits(include_inherited=False)
        for i, commit_file in enumerate(commits):
            display = self.current_vault.get_commit_display_name(commit_file, current_branch)
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)

            snap_lbl = Gtk.Label(label=display, halign=Gtk.Align.START)
            snap_lbl.add_css_class("snapshot-row")
            snap_lbl.set_hexpand(True)
            snap_lbl.set_ellipsize(3)
            row_box.append(snap_lbl)

            lb_row = Gtk.ListBoxRow()
            lb_row.set_child(row_box)
            lb_row._snapshot_file = commit_file
            self.snapshot_list.append(lb_row)
            self._commit_snapshots.append(commit_file)

    def _get_selected_snapshot(self):
        row = self.snapshot_list.get_selected_row()
        if row and hasattr(row, '_snapshot_file'):
            return row._snapshot_file
        return None

    def _on_commit_snapshot(self, btn):
        if not self.current_vault:
            return
        msg = self.vault_msg_entry.get_text().strip()
        if not msg:
            show_info(self, "Message Required", "Please enter a commit message.")
            return
        self.commit_btn.set_sensitive(False)
        self._vault_log("Starting commit...")

        def do_commit():
            try:
                snapshot = self.current_vault.commit(msg, progress_callback=self._vault_log_safe)
                display = self.current_vault.get_commit_display_name(snapshot)
                self._vault_log_safe(f"Commit successful: {display}")
                GLib.idle_add(self._refresh_vault_ui)
                GLib.idle_add(self.vault_msg_entry.set_text, "")
            except Exception as e:
                self._vault_log_safe(f"Commit FAILED: {e}")
                GLib.idle_add(show_error, self, "Commit Failed", str(e))
            finally:
                GLib.idle_add(self.commit_btn.set_sensitive, True)

        threading.Thread(target=do_commit, daemon=True).start()

    def _on_new_branch(self, btn):
        if not self.current_vault:
            show_info(self, "No Project", "Load a project first.")
            return
        self._show_new_branch_dialog()

    def _show_new_branch_dialog(self):
        meta = self.current_vault._load_meta()
        current_branch = meta.get('current_branch', 'main')

        dialog = Gtk.Dialog(transient_for=self, modal=True, title="Create New Branch")
        dialog.set_default_size(380, 200)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_margin_top(16)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_bottom(8)

        name_lbl = Gtk.Label(label="Branch Name:", halign=Gtk.Align.START)
        content.append(name_lbl)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("feature-name")
        content.append(name_entry)

        link_check = Gtk.CheckButton(label=f"Link to '{current_branch}' (inherit history)")
        link_check.set_active(True)
        content.append(link_check)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: dialog.response(Gtk.ResponseType.CANCEL))
        create = Gtk.Button(label="Create Branch")
        create.add_css_class("suggested-action")
        create.connect("clicked", lambda _: dialog.response(Gtk.ResponseType.OK))
        btn_box.append(cancel)
        btn_box.append(create)
        content.append(btn_box)

        def on_response(d, r):
            if r == Gtk.ResponseType.OK:
                name = name_entry.get_text().strip()
                if not name:
                    d.destroy()
                    return
                linked = link_check.get_active()
                try:
                    self.current_vault.create_branch(name, linked=linked)
                    link_type = "linked" if linked else "independent"
                    self._vault_log(f"Created branch '{name}' ({link_type})")
                    self._refresh_vault_ui()
                    # Ask to switch
                    self._ask_switch_branch(name)
                except Exception as e:
                    self._vault_log(f"ERROR creating branch: {e}")
                    show_error(self, "Branch Error", str(e))
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _ask_switch_branch(self, branch_name):
        dialog = Gtk.AlertDialog()
        dialog.set_message(f"Switch to branch '{branch_name}'?")
        dialog.set_buttons(["No", "Switch"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)
        dialog.choose(self, None, lambda d, r: self._do_switch_branch_result(d, r, branch_name))

    def _do_switch_branch_result(self, dialog, result, branch_name):
        try:
            idx = dialog.choose_finish(result)
            if idx == 1:
                self.current_vault.switch_branch(branch_name)
                self._vault_log(f"Switched to branch '{branch_name}'")
                self._refresh_vault_ui()
        except Exception:
            pass

    def _on_switch_branch(self, btn):
        if not self.current_vault:
            show_info(self, "No Project", "Load a project first.")
            return
        meta = self.current_vault._load_meta()
        branches = meta.get('branches', [])
        current = meta.get('current_branch', 'main')

        dialog = Gtk.Dialog(transient_for=self, modal=True, title="Switch Branch")
        dialog.set_default_size(300, 300)
        content = dialog.get_content_area()
        content.set_margin_top(8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_margin_bottom(8)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        branch_list = Gtk.ListBox()
        branch_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for b in branches:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)
            lbl = Gtk.Label(label=b + (" (current)" if b == current else ""), halign=Gtk.Align.START)
            row_box.append(lbl)
            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row._branch_name = b
            branch_list.append(row)
        scroll.set_child(branch_list)
        content.append(scroll)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END, margin_top=8)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: dialog.response(Gtk.ResponseType.CANCEL))
        switch = Gtk.Button(label="Switch")
        switch.add_css_class("suggested-action")
        switch.connect("clicked", lambda _: dialog.response(Gtk.ResponseType.OK))
        btn_box.append(cancel)
        btn_box.append(switch)
        content.append(btn_box)

        def on_response(d, r):
            if r == Gtk.ResponseType.OK:
                sel = branch_list.get_selected_row()
                if sel and hasattr(sel, '_branch_name'):
                    b = sel._branch_name
                    try:
                        self.current_vault.switch_branch(b)
                        self._vault_log(f"Switched to branch '{b}'")
                        self._refresh_vault_ui()
                    except Exception as e:
                        self._vault_log(f"Switch failed: {e}")
                        show_error(self, "Switch Error", str(e))
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _on_delete_branch(self, btn):
        if not self.current_vault:
            show_info(self, "No Project", "Load a project first.")
            return
        meta = self.current_vault._load_meta()
        current = meta.get('current_branch', 'main')
        candidates = [b for b in meta.get('branches', []) if b != current]
        if not candidates:
            show_info(self, "Delete Branch", "No other branches available to delete.\n(You cannot delete the current branch.)")
            return

        dialog = Gtk.Dialog(transient_for=self, modal=True, title="Delete Branch")
        dialog.set_default_size(280, 300)
        content = dialog.get_content_area()
        content.set_margin_top(8)
        content.set_margin_start(8)
        content.set_margin_end(8)
        content.set_margin_bottom(8)

        lbl = Gtk.Label(label="Select branch to delete:", halign=Gtk.Align.START)
        lbl.add_css_class("dim-label")
        content.append(lbl)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        branch_list = Gtk.ListBox()
        branch_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for b in candidates:
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(4)
            row_box.set_margin_bottom(4)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)
            lbl2 = Gtk.Label(label=b, halign=Gtk.Align.START)
            row_box.append(lbl2)
            row = Gtk.ListBoxRow()
            row.set_child(row_box)
            row._branch_name = b
            branch_list.append(row)
        scroll.set_child(branch_list)
        content.append(scroll)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          halign=Gtk.Align.END, margin_top=8)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _: dialog.response(Gtk.ResponseType.CANCEL))
        del_btn = Gtk.Button(label="Delete Selected")
        del_btn.add_css_class("destructive-action")
        del_btn.connect("clicked", lambda _: dialog.response(Gtk.ResponseType.OK))
        btn_box.append(cancel)
        btn_box.append(del_btn)
        content.append(btn_box)

        def on_response(d, r):
            if r == Gtk.ResponseType.OK:
                sel = branch_list.get_selected_row()
                if sel and hasattr(sel, '_branch_name'):
                    target = sel._branch_name
                    try:
                        self.current_vault.delete_branch(target)
                        self._vault_log(f"Deleted branch '{target}'")
                        self._refresh_vault_ui()
                    except Exception as e:
                        self._vault_log(f"Delete failed: {e}")
                        show_error(self, "Delete Error", str(e))
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _on_export_branch(self, btn):
        if not self.current_vault:
            show_info(self, "No Project", "Load a project first.")
            return
        meta = self.current_vault._load_meta()
        current_branch = meta.get('current_branch', 'main')

        dialog = Gtk.FileDialog()
        dialog.set_title(f"Export branch '{current_branch}' to...")
        dialog.select_folder(self, None, lambda d, r: self._do_export_branch(d, r, current_branch))

    def _do_export_branch(self, dialog, result, branch_name):
        try:
            folder = dialog.select_folder_finish(result)
            if not folder:
                return
            dest = folder.get_path()
        except Exception:
            return

        self._vault_log(f"Exporting branch '{branch_name}' to {dest}...")

        def worker():
            try:
                snap = self.current_vault.export_branch_head(branch_name, dest,
                                                             progress_callback=self._vault_log_safe)
                display = self.current_vault.get_commit_display_name(snap, branch_name)
                self._vault_log_safe(f"Export complete: {display}")
            except Exception as e:
                self._vault_log_safe(f"Export FAILED: {e}")
                GLib.idle_add(show_error, self, "Export Failed", str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_compare_snapshot(self, btn):
        if not self.current_vault:
            return
        snap = self._get_selected_snapshot()
        if not snap:
            show_info(self, "No Snapshot Selected", "Please select a snapshot from the history list.")
            return
        dlg = SnapshotDiffDialog(self, self.current_vault, snap, self._vault_log_safe)
        dlg.present()

    def _on_full_restore(self, btn):
        if not self.current_vault:
            return
        snap = self._get_selected_snapshot()
        if not snap:
            show_info(self, "No Snapshot Selected", "Please select a snapshot from the history list.")
            return
        display = self.current_vault.get_commit_display_name(snap)

        alert = Gtk.AlertDialog()
        alert.set_message("Full Restore — This is Destructive")
        alert.set_detail(
            f"Restore snapshot:\n{display}\n\n"
            "This will WIPE all current files in the project folder (except .solace_vault) "
            "and replace them with the snapshot.\n\nUnsaved changes will be LOST."
        )
        alert.set_buttons(["Cancel", "Restore"])
        alert.set_cancel_button(0)
        alert.set_default_button(0)  # Default to Cancel for safety
        alert.choose(self, None, lambda d, r: self._do_full_restore(d, r, snap, display))

    def _do_full_restore(self, dialog, result, snap, display):
        try:
            idx = dialog.choose_finish(result)
            if idx != 1:
                return
        except Exception:
            return

        self.restore_btn.set_sensitive(False)
        self._vault_log(f"Starting full restore: {display}")

        def worker():
            try:
                self.current_vault.restore_snapshot(snap, progress_callback=self._vault_log_safe)
                self._vault_log_safe("Restore complete.")
                GLib.idle_add(self._refresh_vault_ui)
            except Exception as e:
                self._vault_log_safe(f"Restore FAILED: {e}")
                GLib.idle_add(show_error, self, "Restore Failed", str(e))
            finally:
                GLib.idle_add(self.restore_btn.set_sensitive, True)

        threading.Thread(target=worker, daemon=True).start()

    def _on_explore_snapshot(self, btn):
        if not self.current_vault:
            return
        snap = self._get_selected_snapshot()
        if not snap:
            show_info(self, "No Snapshot Selected", "Please select a snapshot from the history list.")
            return
        self._vault_log(f"Mounting snapshot: {snap}")

        def worker():
            try:
                import tempfile
                meta = self.current_vault._load_meta()
                branch = meta['current_branch']
                snap_path = self.current_vault.find_snapshot_path(snap, branch)
                temp_mount = os.path.join(tempfile.gettempdir(), f"Solace_Mount_{snap}")
                if not os.path.exists(temp_mount):
                    os.makedirs(temp_mount)
                    with zipfile.ZipFile(snap_path, 'r') as zf:
                        zf.extractall(temp_mount)
                    self._vault_log_safe(f"Extracted to {temp_mount}")
                subprocess.Popen(['xdg-open', temp_mount])
                self._vault_log_safe("Opened in file manager.")
            except Exception as e:
                self._vault_log_safe(f"Explore FAILED: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _vault_log(self, message):
        """Append message to vault log buffer (must be called from main thread)."""
        end_iter = self.vault_log_buf.get_end_iter()
        ts = datetime.now().strftime("%H:%M:%S")
        self.vault_log_buf.insert(end_iter, f"[{ts}] {message}\n")
        # Auto-scroll
        adj = self._vault_log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

    def _vault_log_safe(self, message):
        """Thread-safe vault log."""
        GLib.idle_add(self._vault_log, message)

    # -----------------------------------------------------------------------
    # Settings View
    # -----------------------------------------------------------------------

    def _build_settings_view(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        outer.set_margin_bottom(16)

        hdr = Gtk.Label(label="Settings")
        hdr.add_css_class("section-title")
        hdr.set_halign(Gtk.Align.START)
        outer.append(hdr)

        settings = self.config.get('global_settings', {})

        # Global backup settings card
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("card")
        card_lbl = Gtk.Label(label="Global Backup Settings", halign=Gtk.Align.START)
        card_lbl.add_css_class("dim-label")
        card.append(card_lbl)

        grid = Gtk.Grid(row_spacing=8, column_spacing=12)
        card.append(grid)

        # Volumes to keep
        grid.attach(Gtk.Label(label="Default backups to keep:", halign=Gtk.Align.END), 0, 0, 1, 1)
        self.volumes_spin = Gtk.SpinButton()
        self.volumes_spin.set_range(1, 99)
        self.volumes_spin.set_increments(1, 5)
        self.volumes_spin.set_value(settings.get('default_volumes_to_keep', 3))
        grid.attach(self.volumes_spin, 1, 0, 1, 1)

        # Base name
        grid.attach(Gtk.Label(label="Default backup base name:", halign=Gtk.Align.END), 0, 1, 1, 1)
        self.base_name_entry = Gtk.Entry()
        self.base_name_entry.set_text(settings.get('default_backup_base_name', 'Backups_Py'))
        self.base_name_entry.set_hexpand(True)
        grid.attach(self.base_name_entry, 1, 1, 1, 1)

        outer.append(card)

        # Autostart card
        auto_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        auto_card.add_css_class("card")
        auto_lbl = Gtk.Label(label="System Integration", halign=Gtk.Align.START)
        auto_lbl.add_css_class("dim-label")
        auto_card.append(auto_lbl)

        autostart_path = os.path.expanduser("~/.config/autostart/solace_backup.desktop")
        self.autostart_check = Gtk.CheckButton(label="Start with system (autostart)")
        self.autostart_check.set_active(os.path.exists(autostart_path))
        auto_card.append(self.autostart_check)

        outer.append(auto_card)

        # Save button
        save_btn = Gtk.Button(label="Save Settings")
        save_btn.add_css_class("suggested-action")
        save_btn.set_halign(Gtk.Align.START)
        save_btn.connect("clicked", self._on_save_settings)
        outer.append(save_btn)

        # Log file path info
        log_lbl = Gtk.Label(label=f"Log file: {LOG_FILE}", halign=Gtk.Align.START)
        log_lbl.add_css_class("dim-label")
        outer.append(log_lbl)

        open_log_btn = Gtk.Button(label="Open Log File")
        open_log_btn.add_css_class("action-btn")
        open_log_btn.set_halign(Gtk.Align.START)
        open_log_btn.connect("clicked", lambda _: subprocess.Popen(['xdg-open', LOG_FILE]))
        outer.append(open_log_btn)

        self.stack.add_named(outer, "settings")

    def _on_save_settings(self, btn):
        gs = self.config.get('global_settings', {})
        gs['default_volumes_to_keep'] = int(self.volumes_spin.get_value())
        gs['default_backup_base_name'] = self.base_name_entry.get_text().strip() or 'Backups_Py'
        self.config['global_settings'] = gs

        if save_config(self.config):
            self._log("Settings saved.")
        else:
            show_error(self, "Save Error", "Failed to save configuration file.")

        # Autostart
        autostart_path = os.path.expanduser("~/.config/autostart/solace_backup.desktop")
        autostart_dir = os.path.dirname(autostart_path)
        desired = self.autostart_check.get_active()
        current_exists = os.path.exists(autostart_path)

        if desired and not current_exists:
            try:
                os.makedirs(autostart_dir, exist_ok=True)
                python_exe = sys.executable
                script_path = str(_BACKUP_SUITE_DIR / "backup_suite_gtk.py")
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=Solace Backup Suite\n"
                    f"Exec={python_exe} \"{script_path}\"\n"
                    "Hidden=false\n"
                    "NoDisplay=false\n"
                    "X-GNOME-Autostart-enabled=true\n"
                    "Comment=Solace Backup Suite GTK4\n"
                )
                with open(autostart_path, 'w') as f:
                    f.write(content)
                os.chmod(autostart_path, 0o755)
                self._log("Added to system autostart.")
            except Exception as e:
                self._log(f"ERROR adding autostart: {e}")
        elif not desired and current_exists:
            try:
                os.remove(autostart_path)
                self._log("Removed from system autostart.")
            except Exception as e:
                self._log(f"ERROR removing autostart: {e}")

    # -----------------------------------------------------------------------
    # Log helpers
    # -----------------------------------------------------------------------

    def _log(self, message):
        """Append to main activity log (must be called from main thread)."""
        end_iter = self.log_buf.get_end_iter()
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_buf.insert(end_iter, f"[{ts}] {message}\n")
        logging.info(message)
        # Auto-scroll
        adj = self._log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

    def _log_safe(self, message):
        """Thread-safe log append."""
        self.log_queue.put(message)

    def _process_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        return True  # Keep the timer going
