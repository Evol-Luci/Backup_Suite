# Solace Backup Suite: Unified CLI Design

**Date:** 2026-02-23
**Status:** Approved

## Problem

The backup suite runs in the system tray at startup but had two issues:
1. The venv was missing `pystray`/`Pillow`, so the tray icon never appeared
2. No unified CLI command to access the GUI or vault operations — users had to know about `backup_suite.pyw` and `vault_cli.py` separately

## Solution

### Already Applied (Pre-Design Fixes)

- Installed `pystray`, `Pillow`, `apscheduler` in the venv
- Fixed autostart script to use venv Python + `--tray` flag
- Fixed singleton logic to always signal existing instance on CLI invocation

### New: Unified `solace-backup` CLI

Single entry point at `backup-suite/solace_backup_cli.py`, symlinked to `~/.local/bin/solace-backup`.

#### Command Structure

```
solace-backup                              Show GUI (signal running instance or start new)
solace-backup show                         Same as no args
solace-backup --status                     Print backup suite status + job info

solace-backup vault commit <path> <msg>    Create snapshot
solace-backup vault log <path>             Show commit history
solace-backup vault branches <path>        List branches
solace-backup vault branch <path> <name>   Create branch
solace-backup vault switch <path> <name>   Switch branch
solace-backup vault merge <path> <snap>    Merge snapshot into working tree
solace-backup vault delete-branch <path> <name>  Delete a branch
solace-backup vault info <path>            Show vault metadata
solace-backup vault diff <path>            Show changes since last snapshot
```

#### GUI Show/Start Logic

1. Read `/tmp/solace_backup_status.json` for running PID
2. If running: send SIGUSR1 to show window, exit
3. If not running: exec `backup_suite.pyw` via venv Python

#### `--status` Output

Reads status file + `Settings/backup_config.json` to display:
- App state (IDLE/RUNNING/disabled), PID
- Configured backup jobs
- Next scheduled run

#### `vault info <path>` Output

- Current branch name
- Total commits on current branch
- Last commit message + timestamp
- Total branches
- Vault disk usage

#### `vault diff <path>` Output

Compare working tree against last snapshot. Show added/modified/deleted files with counts.

#### Vault Subcommands

All existing `vault_cli.py` commands preserved with same flags:
- `--json` for machine-readable output
- `--yes`/`-y` to skip confirmation prompts
- `--verbose`/`-v` for detailed output
- `--unlinked` for standalone branches
- `--limit`/`-n` for log pagination
- `--branch`/`-b` for targeting specific branch
- `--local-only` for excluding inherited commits

## Implementation Notes

- Import `ProjectVault` from `project_vault.py` directly (same approach as `vault_cli.py`)
- Reuse color/output helpers from `vault_cli.py`
- Symlink installed to `~/.local/bin/solace-backup` with executable permissions
- Shebang uses venv Python for consistent dependencies
