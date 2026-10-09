#!/home/lucievol/.local/share/solace/backup-suite/venv/bin/python3
"""
solace-backup — Unified CLI for Solace Backup Suite

Usage:
  solace-backup                    Show GUI window (or start if not running)
  solace-backup show               Same as above
  solace-backup --status           Print backup suite status
  solace-backup vault <command>    Project Vault operations
  solace-backup backup <command>   Backup job management
"""

import sys
import os
import json
import signal
import argparse
import subprocess
from datetime import datetime

# Ensure backup-suite directory is in path for imports
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)

STATUS_FILE = "/tmp/solace_backup_status.json"
CONFIG_PATH = os.path.join(SCRIPT_DIR, "Settings", "backup_config.json")
VENV_PYTHON = os.path.join(SCRIPT_DIR, "venv", "bin", "python3")
GUI_SCRIPT = os.path.join(SCRIPT_DIR, "backup_suite.pyw")
QUICK_REF = os.path.join(SCRIPT_DIR, "USER_QUICK_REF.md")


# ==============================================================================
# Output Helpers
# ==============================================================================
class Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    END = "\033[0m"

    @classmethod
    def disable(cls):
        for attr in ("GREEN", "YELLOW", "CYAN", "RED", "BOLD", "DIM", "END"):
            setattr(cls, attr, "")


if not sys.stdout.isatty():
    Colors.disable()


def success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")

def error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.END}", file=sys.stderr)

def info(msg):
    print(f"  {msg}")

def header(msg):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{msg}{Colors.END}")

def print_callback(msg):
    print(f"  {Colors.YELLOW}>{Colors.END} {msg}")


# ==============================================================================
# GUI Show/Start
# ==============================================================================
def get_running_pid():
    """Check if backup suite is running. Returns PID or None."""
    if not os.path.exists(STATUS_FILE):
        return None
    try:
        with open(STATUS_FILE, "r") as f:
            data = json.load(f)
        pid = data.get("pid")
        if pid is None:
            return None
        os.kill(pid, 0)  # Check if alive
        return pid
    except (json.JSONDecodeError, IOError, OSError):
        return None


def cmd_uses():
    """Display the quick reference guide."""
    if not os.path.exists(QUICK_REF):
        error(f"Quick reference not found: {QUICK_REF}")
        return 1
    with open(QUICK_REF, "r") as f:
        print(f.read())
    return 0


def cmd_show(args=None):
    """Show the GUI window, or start the app if not running."""
    pid = get_running_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
            success(f"Sent show signal to running instance (PID {pid})")
        except OSError as e:
            error(f"Failed to signal running instance: {e}")
            return 1
    else:
        info("Backup Suite is not running. Starting...")
        # Set Tcl/Tk library paths for mise-managed Python (baked-in paths point to build machine)
        python_install = os.path.dirname(os.path.dirname(os.path.realpath(VENV_PYTHON)))
        for var, subdir in [("TCL_LIBRARY", "lib/tcl8.6"), ("TK_LIBRARY", "lib/tk8.6")]:
            path = os.path.join(python_install, subdir)
            if os.path.exists(path) and var not in os.environ:
                os.environ[var] = path
        os.execv(VENV_PYTHON, [VENV_PYTHON, GUI_SCRIPT])
    return 0


def cmd_status(args=None):
    """Print backup suite status."""
    pid = get_running_pid()

    header("Solace Backup Suite Status")

    # App status
    if pid:
        try:
            with open(STATUS_FILE, "r") as f:
                data = json.load(f)
            state = data.get("status", "UNKNOWN")
            msg = data.get("message", "")
            ts = data.get("timestamp", "")
            print(f"  {Colors.GREEN}● Running{Colors.END}  PID: {pid}")
            print(f"  State: {state}  {Colors.DIM}{msg}{Colors.END}")
            if ts:
                print(f"  Last update: {ts}")
        except Exception:
            print(f"  {Colors.GREEN}● Running{Colors.END}  PID: {pid}")
    else:
        print(f"  {Colors.RED}○ Not running{Colors.END}")

    # Backup jobs
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
            jobs = config.get("backup_jobs", [])
            if jobs:
                header("Backup Jobs")
                for job in jobs:
                    status_icon = f"{Colors.GREEN}✓{Colors.END}" if job.get("enabled") else f"{Colors.RED}✗{Colors.END}"
                    schedule = job.get("schedule", "manual")
                    print(f"  [{status_icon}] {Colors.BOLD}{job['name']}{Colors.END}  {Colors.DIM}({schedule}){Colors.END}")
                    print(f"      {job.get('source_dir', 'N/A')} → {job.get('destination_base', 'N/A')}")
            else:
                info("No backup jobs configured.")
        except Exception as e:
            error(f"Could not read config: {e}")
    else:
        info("No configuration file found.")

    return 0


# ==============================================================================
# Vault Commands
# ==============================================================================
def resolve_path(path):
    path = os.path.abspath(path)
    if not os.path.exists(path):
        error(f"Project path does not exist: {path}")
        sys.exit(1)
    return path


def cmd_vault_commit(args):
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    print(f"Creating snapshot for: {Colors.CYAN}{project_path}{Colors.END}")
    print(f"Message: {args.message}\n")
    vault = ProjectVault(project_path)
    try:
        snapshot_name = vault.commit(args.message, progress_callback=print_callback)
        meta = vault._load_meta()
        success(f"Snapshot created: {snapshot_name}")
        info(f"Branch: {meta['current_branch']}")
        if args.json:
            print(json.dumps({"snapshot": snapshot_name, "branch": meta["current_branch"]}))
        return 0
    except Exception as e:
        error(f"Failed to create snapshot: {e}")
        return 1


def cmd_vault_branch(args):
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    try:
        vault.create_branch(args.name, linked=not args.unlinked)
        meta = vault._load_meta()
        success(f"Branch '{args.name}' created from '{meta['current_branch']}'")
        info("Use 'vault switch' to move to the new branch.")
        if args.json:
            print(json.dumps({"branch": args.name, "parent": meta["current_branch"]}))
        return 0
    except ValueError as e:
        error(str(e))
        return 1
    except Exception as e:
        error(f"Failed to create branch: {e}")
        return 1


def cmd_vault_switch(args):
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    try:
        old_branch = vault._load_meta()["current_branch"]
        vault.switch_branch(args.name)
        success(f"Switched from '{old_branch}' to '{args.name}'")
        info("Note: files are NOT restored automatically. Use 'vault merge' or restore a snapshot.")
        if args.json:
            print(json.dumps({"from": old_branch, "to": args.name}))
        return 0
    except ValueError as e:
        error(str(e))
        return 1
    except Exception as e:
        error(f"Failed to switch branch: {e}")
        return 1


def cmd_vault_branches(args):
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    meta = vault._load_meta()
    current = meta.get("current_branch", "main")
    branches = meta.get("branches", [])
    origins = meta.get("branch_origins", {})

    if args.json:
        print(json.dumps({
            "current": current,
            "branches": [{"name": b, "parent": origins.get(b, {}).get("parent")} for b in branches]
        }))
        return 0

    header("Branches")
    for b in branches:
        marker = f"{Colors.GREEN}*{Colors.END}" if b == current else " "
        parent_info = ""
        if b in origins:
            parent_info = f"  {Colors.YELLOW}← {origins[b].get('parent', '?')}{Colors.END}"
        bold = Colors.BOLD if b == current else ""
        end = Colors.END if b == current else ""
        print(f"  {marker} {bold}{b}{end}{parent_info}")

    print(f"\nTotal: {len(branches)} branch(es). Current: {Colors.GREEN}{current}{Colors.END}")
    return 0


def cmd_vault_log(args):
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    meta = vault._load_meta()
    target_branch = args.branch or meta.get("current_branch", "main")
    include_inherited = not args.local_only
    commits = vault.get_commits(target_branch, include_inherited=include_inherited)

    if args.json:
        entries = []
        for c in commits:
            md = vault.get_commit_metadata(c, target_branch)
            entries.append({
                "filename": c,
                "message": md.get("message") if md else None,
                "timestamp": md.get("timestamp") if md else None,
            })
        print(json.dumps({"branch": target_branch, "commits": entries}))
        return 0

    suffix = " (local only)" if args.local_only else ""
    header(f"Commit Log — branch: {target_branch}{suffix}")

    if not commits:
        info("No commits found.")
        return 0

    limit = args.limit or len(commits)
    for i, c in enumerate(commits[:limit]):
        display = vault.get_commit_display_name(c, target_branch)
        branch_path_main = os.path.join(vault.branches_dir, target_branch, c)
        tag = ""
        if not os.path.exists(branch_path_main):
            tag = f"  {Colors.YELLOW}(inherited){Colors.END}"
        print(f"  {Colors.CYAN}#{i+1:02d}{Colors.END}  {display}{tag}")

    if len(commits) > limit:
        info(f"... and {len(commits) - limit} more. Use --limit to see more.")
    return 0


def cmd_vault_merge(args):
    import shutil
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    meta = vault._load_meta()

    print(f"Comparing snapshot {Colors.CYAN}{args.snapshot}{Colors.END} against working tree...\n")

    try:
        diffs, temp_dir = vault.compare_snapshot(args.snapshot, progress_callback=print_callback)
    except FileNotFoundError:
        error(f"Snapshot not found: {args.snapshot}")
        error("Run 'vault log' to see available snapshots.")
        return 1
    except Exception as e:
        error(f"Comparison failed: {e}")
        return 1

    if not diffs:
        success("No differences found — working tree already matches the snapshot.")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        return 0

    new_count = sum(1 for d in diffs if d["status"] == "NEW")
    modified_count = sum(1 for d in diffs if d["status"] == "MODIFIED")
    missing_count = sum(1 for d in diffs if d["status"] == "MISSING")

    header("Merge Preview")
    if new_count:
        print(f"  {Colors.GREEN}+ {new_count} file(s) to add{Colors.END}")
    if modified_count:
        print(f"  {Colors.YELLOW}~ {modified_count} file(s) to update{Colors.END}")
    if missing_count:
        print(f"  {Colors.RED}- {missing_count} file(s) to remove{Colors.END}")
    print()

    if args.verbose:
        for d in diffs:
            color = {"NEW": Colors.GREEN, "MODIFIED": Colors.YELLOW, "MISSING": Colors.RED}.get(d["status"], "")
            print(f"  {color}{d['status']:8s}{Colors.END}  {d['file']}")
        print()

    if not args.yes:
        try:
            resp = input("Apply merge? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            info("Cancelled.")
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return 0
        if resp not in ("y", "yes"):
            info("Cancelled.")
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return 0

    try:
        vault.apply_merge(diffs, temp_dir)
        success(f"Merge applied: {new_count} added, {modified_count} updated, {missing_count} removed.")
        info("Tip: run 'vault commit' to snapshot the merged state.")
        if args.json:
            print(json.dumps({"added": new_count, "modified": modified_count, "removed": missing_count}))
        return 0
    except Exception as e:
        error(f"Merge failed: {e}")
        return 1


def cmd_vault_delete_branch(args):
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)

    if not args.yes:
        try:
            resp = input(f"Delete branch '{args.name}'? This cannot be undone. [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            info("Cancelled.")
            return 0
        if resp not in ("y", "yes"):
            info("Cancelled.")
            return 0

    try:
        vault.delete_branch(args.name)
        success(f"Branch '{args.name}' deleted.")
        if args.json:
            print(json.dumps({"deleted": args.name}))
        return 0
    except ValueError as e:
        error(str(e))
        return 1
    except Exception as e:
        error(f"Failed to delete branch: {e}")
        return 1


def cmd_vault_info(args):
    """Show vault metadata for a project."""
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    meta = vault._load_meta()

    current_branch = meta.get("current_branch", "main")
    branches = meta.get("branches", [])
    commits = vault.get_commits(current_branch, include_inherited=True)
    local_commits = vault.get_commits(current_branch, include_inherited=False)

    # Last commit info
    last_commit_msg = None
    last_commit_ts = None
    if commits:
        md = vault.get_commit_metadata(commits[0], current_branch)
        if md:
            last_commit_msg = md.get("message", "")
            last_commit_ts = md.get("timestamp", "")

    # Vault disk usage
    vault_size_bytes = 0
    for root, dirs, files in os.walk(vault.vault_path):
        for f in files:
            try:
                vault_size_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass

    if vault_size_bytes < 1024 * 1024:
        size_str = f"{vault_size_bytes / 1024:.1f} KB"
    elif vault_size_bytes < 1024 * 1024 * 1024:
        size_str = f"{vault_size_bytes / (1024 * 1024):.1f} MB"
    else:
        size_str = f"{vault_size_bytes / (1024 * 1024 * 1024):.2f} GB"

    if args.json:
        print(json.dumps({
            "project_path": project_path,
            "current_branch": current_branch,
            "branch_count": len(branches),
            "total_commits": len(commits),
            "local_commits": len(local_commits),
            "last_commit_message": last_commit_msg,
            "last_commit_timestamp": last_commit_ts,
            "vault_size_bytes": vault_size_bytes,
        }))
        return 0

    header(f"Vault Info — {os.path.basename(project_path)}")
    print(f"  Path:     {Colors.CYAN}{project_path}{Colors.END}")
    print(f"  Branch:   {Colors.GREEN}{current_branch}{Colors.END}")
    print(f"  Branches: {len(branches)}")
    print(f"  Commits:  {len(commits)} total ({len(local_commits)} on this branch)")
    if last_commit_msg:
        print(f"  Last:     {Colors.YELLOW}{last_commit_msg}{Colors.END}")
    if last_commit_ts:
        print(f"  Date:     {last_commit_ts}")
    print(f"  Size:     {size_str}")
    return 0


def cmd_vault_diff(args):
    """Show what changed since the last snapshot."""
    import shutil
    from project_vault import ProjectVault
    project_path = resolve_path(args.project_path)
    vault = ProjectVault(project_path)
    meta = vault._load_meta()
    current_branch = meta.get("current_branch", "main")
    commits = vault.get_commits(current_branch, include_inherited=True)

    if not commits:
        info("No commits yet. Nothing to diff against.")
        return 0

    latest = commits[0]
    print(f"Comparing working tree against: {Colors.CYAN}{vault.get_commit_display_name(latest, current_branch)}{Colors.END}\n")

    try:
        diffs, temp_dir = vault.compare_snapshot(latest, progress_callback=print_callback)
    except Exception as e:
        error(f"Comparison failed: {e}")
        return 1

    if not diffs:
        success("Working tree matches the last snapshot. No changes.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 0

    new_count = sum(1 for d in diffs if d["status"] == "NEW")
    modified_count = sum(1 for d in diffs if d["status"] == "MODIFIED")
    missing_count = sum(1 for d in diffs if d["status"] == "MISSING")

    if args.json:
        print(json.dumps({
            "compared_to": latest,
            "added": new_count,
            "modified": modified_count,
            "deleted": missing_count,
            "files": [{"file": d["file"], "status": d["status"]} for d in diffs] if args.verbose else [],
        }))
        shutil.rmtree(temp_dir, ignore_errors=True)
        return 0

    header("Changes since last snapshot")
    if new_count:
        print(f"  {Colors.GREEN}+ {new_count} new file(s){Colors.END}")
    if modified_count:
        print(f"  {Colors.YELLOW}~ {modified_count} modified file(s){Colors.END}")
    if missing_count:
        print(f"  {Colors.RED}- {missing_count} deleted file(s){Colors.END}")
    print()

    if args.verbose:
        for d in diffs:
            color = {"NEW": Colors.GREEN, "MODIFIED": Colors.YELLOW, "MISSING": Colors.RED}.get(d["status"], "")
            print(f"  {color}{d['status']:8s}{Colors.END}  {d['file']}")

    shutil.rmtree(temp_dir, ignore_errors=True)
    return 0


# ==============================================================================
# Backup Commands (delegate to backup_cli)
# ==============================================================================
def cmd_backup(args):
    """Delegate to backup_cli.py with remaining args."""
    backup_cli = os.path.join(SCRIPT_DIR, "backup_cli.py")
    # Pass through all args after 'backup'
    remaining = sys.argv[sys.argv.index("backup") + 1:]
    result = subprocess.run([VENV_PYTHON, backup_cli] + remaining)
    return result.returncode


# ==============================================================================
# Main
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        prog="solace-backup",
        description="Solace Backup Suite — unified CLI for backups and project vault",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  solace-backup                              # Show GUI
  solace-backup --status                     # Print status summary
  solace-backup --uses                       # Show quick reference guide
  solace-backup vault info /path/to/project  # Show vault metadata
  solace-backup vault commit /path "Message" # Create snapshot
  solace-backup vault log /path              # Show commit history
  solace-backup vault diff /path             # Show changes since last snapshot
  solace-backup backup list                  # List backup jobs
  solace-backup backup run my_job --blocking # Run a backup job
        """,
    )

    parser.add_argument("--status", action="store_true", help="Print backup suite status")
    parser.add_argument("--uses", action="store_true", help="Show quick reference guide")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")

    subparsers = parser.add_subparsers(dest="command")

    # show
    subparsers.add_parser("show", help="Show GUI window (or start if not running)")

    # vault
    vault_parser = subparsers.add_parser("vault", help="Project Vault operations")
    vault_sub = vault_parser.add_subparsers(dest="vault_command")

    # vault commit
    p = vault_sub.add_parser("commit", help="Create a snapshot")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("message", help="Commit message")

    # vault branch
    p = vault_sub.add_parser("branch", help="Create a new branch")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("name", help="New branch name")
    p.add_argument("--unlinked", action="store_true", help="Create standalone branch")

    # vault switch
    p = vault_sub.add_parser("switch", help="Switch active branch")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("name", help="Branch name")

    # vault branches
    p = vault_sub.add_parser("branches", help="List all branches")
    p.add_argument("project_path", help="Path to the project directory")

    # vault log
    p = vault_sub.add_parser("log", help="Show commit history")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("--branch", "-b", help="Branch to show (default: current)")
    p.add_argument("--local-only", action="store_true", help="Exclude inherited commits")
    p.add_argument("--limit", "-n", type=int, help="Limit commits shown")

    # vault merge
    p = vault_sub.add_parser("merge", help="Merge snapshot into working tree")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("snapshot", help="Snapshot filename")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p.add_argument("--verbose", "-v", action="store_true", help="Show per-file details")

    # vault delete-branch
    p = vault_sub.add_parser("delete-branch", help="Delete a branch")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("name", help="Branch name")
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    # vault info (NEW)
    p = vault_sub.add_parser("info", help="Show vault metadata")
    p.add_argument("project_path", help="Path to the project directory")

    # vault diff (NEW)
    p = vault_sub.add_parser("diff", help="Show changes since last snapshot")
    p.add_argument("project_path", help="Path to the project directory")
    p.add_argument("--verbose", "-v", action="store_true", help="Show per-file details")

    # backup (delegates to backup_cli.py)
    subparsers.add_parser("backup", help="Backup job management (list, create, run, etc.)")

    args = parser.parse_args()

    # Propagate --json to vault subcommands
    if not hasattr(args, "json"):
        args.json = False

    # Route commands
    if args.uses:
        return cmd_uses()

    if args.status:
        return cmd_status(args)

    if args.command is None or args.command == "show":
        return cmd_show(args)

    if args.command == "backup":
        return cmd_backup(args)

    if args.command == "vault":
        if not args.vault_command:
            vault_parser.print_help()
            return 2

        vault_commands = {
            "commit": cmd_vault_commit,
            "branch": cmd_vault_branch,
            "switch": cmd_vault_switch,
            "branches": cmd_vault_branches,
            "log": cmd_vault_log,
            "merge": cmd_vault_merge,
            "delete-branch": cmd_vault_delete_branch,
            "info": cmd_vault_info,
            "diff": cmd_vault_diff,
        }
        return vault_commands[args.vault_command](args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
