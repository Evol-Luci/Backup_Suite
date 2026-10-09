#!/usr/bin/env python3
"""
Solace Backup / Project Vault CLI
Command-line interface for managing project snapshots, branches, and merges.

Usage:
  vault_cli.py commit  <project_path> <message>    Create a snapshot
  vault_cli.py branch  <project_path> <name>       Create a new branch
  vault_cli.py switch  <project_path> <name>       Switch active branch
  vault_cli.py branches <project_path>             List all branches
  vault_cli.py log     <project_path>              Show commit history
  vault_cli.py merge   <project_path> <snapshot>   Merge snapshot into working tree
  vault_cli.py delete-branch <project_path> <name> Delete a branch
"""

import sys
import os
import argparse
import json

# Add parent directory to path to import project_vault
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from project_vault import ProjectVault


# ==============================================================================
# Output Helpers
# ==============================================================================

class Colors:
    """Terminal colors - auto-disabled if not a tty."""
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    CYAN    = "\033[96m"
    RED     = "\033[91m"
    BOLD    = "\033[1m"
    END     = "\033[0m"

    @classmethod
    def disable(cls):
        cls.GREEN = cls.YELLOW = cls.CYAN = cls.RED = cls.BOLD = cls.END = ""


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
    """Simple progress callback that prints to stdout."""
    print(f"  {Colors.YELLOW}>{Colors.END} {msg}")


def resolve_path(path):
    """Resolve and validate a project path."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        error(f"Project path does not exist: {path}")
        sys.exit(1)
    return path


# ==============================================================================
# Command Handlers
# ==============================================================================

def cmd_commit(args):
    """Create a snapshot of the project."""
    project_path = resolve_path(args.project_path)
    message = args.message

    print(f"Creating snapshot for: {Colors.CYAN}{project_path}{Colors.END}")
    print(f"Message: {message}\n")

    vault = ProjectVault(project_path)
    try:
        snapshot_name = vault.commit(message, progress_callback=print_callback)
        meta = vault._load_meta()
        success(f"Snapshot created: {snapshot_name}")
        info(f"Branch: {meta['current_branch']}")
        if args.json:
            print(json.dumps({"snapshot": snapshot_name, "branch": meta["current_branch"]}))
        return 0
    except Exception as e:
        error(f"Failed to create snapshot: {e}")
        return 1


def cmd_branch(args):
    """Create a new branch."""
    project_path = resolve_path(args.project_path)
    branch_name = args.name

    vault = ProjectVault(project_path)
    try:
        vault.create_branch(branch_name, linked=not args.unlinked)
        meta = vault._load_meta()
        success(f"Branch '{branch_name}' created from '{meta['current_branch']}'")
        info("Use 'switch' to move to the new branch.")
        if args.json:
            print(json.dumps({"branch": branch_name, "parent": meta["current_branch"]}))
        return 0
    except ValueError as e:
        error(str(e))
        return 1
    except Exception as e:
        error(f"Failed to create branch: {e}")
        return 1


def cmd_switch(args):
    """Switch the active branch."""
    project_path = resolve_path(args.project_path)
    branch_name = args.name

    vault = ProjectVault(project_path)
    try:
        old_branch = vault._load_meta()["current_branch"]
        vault.switch_branch(branch_name)
        success(f"Switched from '{old_branch}' to '{branch_name}'")
        info("Note: files are NOT restored automatically. Use 'merge' or restore a snapshot to update your working tree.")
        if args.json:
            print(json.dumps({"from": old_branch, "to": branch_name}))
        return 0
    except ValueError as e:
        error(str(e))
        return 1
    except Exception as e:
        error(f"Failed to switch branch: {e}")
        return 1


def cmd_branches(args):
    """List all branches."""
    project_path = resolve_path(args.project_path)

    vault = ProjectVault(project_path)
    meta = vault._load_meta()
    current = meta.get("current_branch", "main")
    branches = meta.get("branches", [])
    origins = meta.get("branch_origins", {})

    if args.json:
        print(json.dumps({
            "current": current,
            "branches": [
                {"name": b, "parent": origins.get(b, {}).get("parent")}
                for b in branches
            ]
        }))
        return 0

    header("Branches")
    for b in branches:
        marker = f"{Colors.GREEN}*{Colors.END}" if b == current else " "
        parent_info = ""
        if b in origins:
            parent_info = f"  {Colors.YELLOW}← {origins[b].get('parent', '?')}{Colors.END}"
        print(f"  {marker} {Colors.BOLD if b == current else ''}{b}{Colors.END if b == current else ''}{parent_info}")

    print(f"\nTotal: {len(branches)} branch(es). Current: {Colors.GREEN}{current}{Colors.END}")
    return 0


def cmd_log(args):
    """Show commit history for a branch."""
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

    header(f"Commit Log — branch: {target_branch}" + (" (local only)" if args.local_only else ""))

    if not commits:
        info("No commits found.")
        return 0

    limit = args.limit or len(commits)
    for i, c in enumerate(commits[:limit]):
        display = vault.get_commit_display_name(c, target_branch)
        # Mark which branch the commit lives in
        origins = meta.get("branch_origins", {})
        branch_path_main = os.path.join(vault.branches_dir, target_branch, c)
        tag = ""
        if not os.path.exists(branch_path_main):
            # Commit is inherited from parent
            tag = f"  {Colors.YELLOW}(inherited){Colors.END}"
        print(f"  {Colors.CYAN}#{i+1:02d}{Colors.END}  {display}{tag}")

    if len(commits) > limit:
        info(f"... and {len(commits) - limit} more. Use --limit to see more.")

    return 0


def cmd_merge(args):
    """Merge a specific snapshot into the current working tree."""
    project_path = resolve_path(args.project_path)
    snapshot_name = args.snapshot

    vault = ProjectVault(project_path)
    meta = vault._load_meta()
    current_branch = meta.get("current_branch", "main")

    print(f"Comparing snapshot {Colors.CYAN}{snapshot_name}{Colors.END} against working tree...\n")

    try:
        diffs, temp_dir = vault.compare_snapshot(snapshot_name, progress_callback=print_callback)
    except FileNotFoundError:
        error(f"Snapshot not found: {snapshot_name}")
        error(f"Run 'log' to see available snapshots.")
        return 1
    except Exception as e:
        error(f"Comparison failed: {e}")
        return 1

    if not diffs:
        success("No differences found — working tree already matches the snapshot.")
        import shutil
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        return 0

    # Show summary
    new_count      = sum(1 for d in diffs if d["status"] == "NEW")
    modified_count = sum(1 for d in diffs if d["status"] == "MODIFIED")
    missing_count  = sum(1 for d in diffs if d["status"] == "MISSING")

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
            status_color = {
                "NEW":      Colors.GREEN,
                "MODIFIED": Colors.YELLOW,
                "MISSING":  Colors.RED,
            }.get(d["status"], "")
            print(f"  {status_color}{d['status']:8s}{Colors.END}  {d['file']}")
        print()

    # Confirmation
    if not args.yes:
        try:
            resp = input("Apply merge? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            info("Cancelled.")
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return 0
        if resp not in ("y", "yes"):
            info("Cancelled.")
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return 0

    try:
        vault.apply_merge(diffs, temp_dir)  # apply_merge also cleans up temp_dir
        success(f"Merge applied: {new_count} added, {modified_count} updated, {missing_count} removed.")
        info("Tip: run 'commit' to snapshot the merged state.")
        if args.json:
            print(json.dumps({
                "added": new_count,
                "modified": modified_count,
                "removed": missing_count,
            }))
        return 0
    except Exception as e:
        error(f"Merge failed: {e}")
        return 1


def cmd_delete_branch(args):
    """Delete a branch."""
    project_path = resolve_path(args.project_path)
    branch_name = args.name

    vault = ProjectVault(project_path)

    if not args.yes:
        try:
            resp = input(f"Delete branch '{branch_name}'? This cannot be undone. [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            info("Cancelled.")
            return 0
        if resp not in ("y", "yes"):
            info("Cancelled.")
            return 0

    try:
        vault.delete_branch(branch_name)
        success(f"Branch '{branch_name}' deleted.")
        if args.json:
            print(json.dumps({"deleted": branch_name}))
        return 0
    except ValueError as e:
        error(str(e))
        return 1
    except Exception as e:
        error(f"Failed to delete branch: {e}")
        return 1


# ==============================================================================
# Main
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="vault_cli.py",
        description="Solace Project Vault CLI — snapshot, branch, and merge your project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  vault_cli.py commit  /path/to/project "Add login page"
  vault_cli.py branch  /path/to/project feature-auth
  vault_cli.py switch  /path/to/project feature-auth
  vault_cli.py commit  /path/to/project "WIP: auth module"
  vault_cli.py log     /path/to/project
  vault_cli.py switch  /path/to/project main
  vault_cli.py merge   /path/to/project 2026-01-01_12-00-00_abc123.zip --yes
  vault_cli.py delete-branch /path/to/project feature-auth
        """,
    )

    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    subparsers = parser.add_subparsers(dest="command", help="Subcommands")

    # --- commit ---
    p_commit = subparsers.add_parser("commit", help="Create a snapshot of the project")
    p_commit.add_argument("project_path", help="Path to the project directory")
    p_commit.add_argument("message", help="Commit message")

    # --- branch ---
    p_branch = subparsers.add_parser("branch", help="Create a new branch")
    p_branch.add_argument("project_path", help="Path to the project directory")
    p_branch.add_argument("name", help="New branch name")
    p_branch.add_argument("--unlinked", action="store_true",
                           help="Create a standalone branch with no link to parent commit history")

    # --- switch ---
    p_switch = subparsers.add_parser("switch", help="Switch the active branch (does not restore files)")
    p_switch.add_argument("project_path", help="Path to the project directory")
    p_switch.add_argument("name", help="Branch name to switch to")

    # --- branches ---
    p_branches = subparsers.add_parser("branches", help="List all branches")
    p_branches.add_argument("project_path", help="Path to the project directory")

    # --- log ---
    p_log = subparsers.add_parser("log", help="Show commit history")
    p_log.add_argument("project_path", help="Path to the project directory")
    p_log.add_argument("--branch", "-b", help="Branch to show (default: current branch)")
    p_log.add_argument("--local-only", action="store_true",
                        help="Show only commits made directly on this branch (no inherited commits)")
    p_log.add_argument("--limit", "-n", type=int, help="Limit number of commits shown")

    # --- merge ---
    p_merge = subparsers.add_parser("merge", help="Merge a snapshot into the working tree")
    p_merge.add_argument("project_path", help="Path to the project directory")
    p_merge.add_argument("snapshot", help="Snapshot filename to merge from (e.g. 2026-01-01_12-00-00_abc123.zip)")
    p_merge.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    p_merge.add_argument("--verbose", "-v", action="store_true", help="Show per-file diff details")

    # --- delete-branch ---
    p_del = subparsers.add_parser("delete-branch", help="Delete a branch")
    p_del.add_argument("project_path", help="Path to the project directory")
    p_del.add_argument("name", help="Branch name to delete")
    p_del.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    # Propagate --json to all subcommands
    if not hasattr(args, "json"):
        args.json = False

    if not args.command:
        parser.print_help()
        return 2

    commands = {
        "commit":        cmd_commit,
        "branch":        cmd_branch,
        "switch":        cmd_switch,
        "branches":      cmd_branches,
        "log":           cmd_log,
        "merge":         cmd_merge,
        "delete-branch": cmd_delete_branch,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
