# Solace Backup — Quick Reference

## GUI

```bash
solace-backup              # Open window (or bring to front if running)
solace-backup show         # Same thing
```

## Status

```bash
solace-backup --status     # Running state, backup jobs summary
```

## Project Vault

Snapshot-based version control for any project folder.

```bash
# See what's going on
solace-backup vault info /path/to/project       # Branch, commits, vault size
solace-backup vault diff /path/to/project       # Changes since last snapshot
solace-backup vault diff /path/to/project -v    # With per-file details

# Snapshots
solace-backup vault commit /path/to/project "Save before refactor"
solace-backup vault log /path/to/project        # History
solace-backup vault log /path/to/project -n 5   # Last 5 only

# Branches
solace-backup vault branches /path/to/project          # List all
solace-backup vault branch /path/to/project feature-x   # Create
solace-backup vault switch /path/to/project feature-x    # Switch
solace-backup vault delete-branch /path/to/project old-branch -y

# Restore
solace-backup vault merge /path/to/project SNAPSHOT_FILE    # Preview + apply
solace-backup vault merge /path/to/project SNAPSHOT_FILE -y # Skip confirmation
```

## Backup Jobs

```bash
solace-backup backup list                  # List configured jobs
solace-backup backup run my_job --blocking # Run a job and wait
```

## JSON Output

Add `--json` for machine-readable output (useful for scripts):

```bash
solace-backup --status --json
solace-backup vault info /path/to/project --json
solace-backup vault diff /path/to/project --json
```
