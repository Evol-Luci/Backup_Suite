# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Solace Backup Suite is a Python GUI application with two main features:
1. **Automated Backups** — Scheduled/manual backup job management with retention policies
2. **Project Vault** — A snapshot-based "Git-Lite" version control system designed for creative assets (images, 3D models, code)

Cross-platform (Windows + Linux). Uses tkinter for GUI, APScheduler for scheduling, pystray for system tray.

## Running the Application

### GUI Application

```bash
source venv/bin/activate
python backup_suite.pyw
```

Dependencies: `pip install -r requirements.txt` (apscheduler, pystray, Pillow)

### Unified CLI (`solace-backup`)

Single command for all backup suite operations. Installed at `~/.local/bin/solace-backup`.

```bash
# GUI
solace-backup                              # Show GUI (signal running instance or start new)
solace-backup show                         # Same as above

# Status
solace-backup --status                     # Print backup suite status and job summary

# Vault operations
solace-backup vault info /path/to/project  # Show vault metadata (branch, commits, size)
solace-backup vault diff /path             # Show changes since last snapshot
solace-backup vault diff /path -v          # Show per-file change details
solace-backup vault commit /path "Message" # Create snapshot
solace-backup vault log /path              # Show commit history
solace-backup vault log /path -n 5         # Limit to 5 commits
solace-backup vault log /path --local-only # Exclude inherited commits
solace-backup vault branches /path         # List all branches
solace-backup vault branch /path new-feat  # Create new branch
solace-backup vault switch /path new-feat  # Switch active branch
solace-backup vault merge /path snapshot.zip  # Merge snapshot into working tree
solace-backup vault delete-branch /path old -y  # Delete branch (skip confirm)

# Backup job management (delegates to backup_cli.py)
solace-backup backup list                  # List backup jobs
solace-backup backup run my_job --blocking # Run a backup job

# JSON output (for scripting)
solace-backup --status --json
solace-backup vault info /path --json
```

**Architecture:** Thin routing script (`solace_backup_cli.py`) that delegates to existing modules. GUI access uses PID/signal pattern from `backup_suite.pyw`. Vault commands import `ProjectVault` directly. Backup commands delegate to `backup_cli.py`.

### Legacy CLI (vault_cli.py)

Standalone command for creating Project Vault snapshots (superseded by `solace-backup vault commit`).

```bash
python vault_cli.py <project_path> <commit_message>
```

## Testing

No formal test framework. Manual test scripts exist for vault features:
```bash
python debug_branch_delete.py
python debug_linked_branches.py
python debug_linked_branches_v2.py
```

These create `test_data_*` directories with isolated vault instances for verification.

## Architecture

### Module Structure

- **`backup_suite.pyw`** (~2100 lines) — Main entry point. Contains all GUI code (BackupApp), backup execution logic, job scheduling, theme management, system tray integration, and the VaultMergeWindow.
- **`project_vault.py`** (~500 lines) — ProjectVault backend class. Handles all vault operations: commit (zip snapshots), branching, switching, merging, diffing, restore, delete. Includes per-branch manifest system for commit metadata. Stores data in `.solace_vault/` inside the target project.
- **`branch_visualizer.py`** (~227 lines) — BranchTreeVisualizer, a custom tkinter Canvas widget that draws an interactive branch/commit graph.
- **`solace_backup_cli.py`** (~680 lines) — Unified CLI entry point. Routes `solace-backup` commands to GUI show/start, vault operations (ProjectVault), and backup job management (backup_cli.py). Installed as `~/.local/bin/solace-backup`.
- **`vault_cli.py`** (~60 lines) — Legacy CLI for creating snapshots. Superseded by `solace-backup vault commit`.

### Key Classes

- **`BackupApp`** — Main application class. Manages the tabbed ttk.Notebook interface, theming (Light/Dark/System), system tray via pystray, and all UI state. Referenced globally via `main_app_ref`.
- **`ProjectVault`** — Backend for snapshot-based version control. Each commit is a full ZIP of the project (excluding `.solace_vault/`). Metadata lives in `.solace_vault/meta.json`. Branch relationships tracked via `branch_origins`.
- **`BranchTreeVisualizer`** — Canvas-based commit graph. Draws branch lines, commit nodes, and origin connections. Supports click callbacks for commit/branch selection.
- **`BackupQueueManager`** — Thread-safe sequential job queue with locking to prevent concurrent backups.
- **`VaultMergeWindow`** — Modal dialog for file-level merge conflict resolution (whole-file "take theirs/ours" approach, not line-level).

### Vault Storage Layout

```
/TargetProject/.solace_vault/
    meta.json                    # current_branch, branches[], branch_origins{}, head_commit
    branches/
        main/
            commits.json         # Per-branch manifest: commit metadata (message, timestamp, parent, tags)
            YYYY-MM-DD_HH-MM-SS_hash.zip
        feature/
            commits.json
            YYYY-MM-DD_HH-MM-SS_hash.zip
```

Snapshots are full ZIP archives named with timestamp + 8-char SHA256 hash. Commit metadata (original message, ISO timestamp, parent chain, tags) is stored in per-branch `commits.json` manifests — not in filenames. Legacy vaults with `YYYY-MM-DD_HH-MM-SS__Message.zip` filenames are auto-migrated on load. Branch inheritance: child branches see parent commits via `get_commits(include_inherited=True)`. The `_commit_index_map` in BackupApp maps UI listbox indices back to actual commit filenames.

### Threading Model

- Backup operations run on background threads
- GUI updates from threads go through `root.after()` and a log queue (`process_log_queue`)
- BackupQueueManager uses threading.Lock for job queue safety

### Platform Differences

- **Windows**: `winreg` for autostart (registry), robocopy for file operations
- **Linux**: `.desktop` files in `~/.config/autostart`, rsync/cp fallback, GTK theme detection for System theme
- Platform detected via `IS_WINDOWS = platform.system() == "Windows"` at module level

### Configuration

- **`Settings/backup_config.json`** — Stores global settings (theme, default retention) and all backup job definitions
- **`Debug/backup_suite_debug.log`** — Runtime log (overwritten each launch), DEBUG level

### Theme System

Two built-in themes (Light/Dark) defined as color dictionaries in `THEMES` dict at top of `backup_suite.pyw`. "System" option auto-detects OS theme. Theme colors propagate to BranchTreeVisualizer via `set_colors()`.

## Design Principles

- **Snapshot-centric**: No staging area, no line-by-line diffs, no detached heads. Files are atomic units.
- **Binary-friendly**: Designed to handle creative assets (images, 3D models) where diff-based VCS doesn't work well.
- **Simple merge**: File-level conflict resolution ("take entire file"), not line-based.
- **Graceful degradation**: APScheduler and pystray are optional — app works without them with reduced functionality.
