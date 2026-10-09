# Solace Backup Suite - API Integration Guide

> **Version:** 1.0.0  
> **Last Updated:** 2026-02-17  
> **For:** Good Vibes Cosmetic and external project integrations

---

## Table of Contents

1. [Overview](#overview)
2. [Python API Integration](#python-api-integration)
3. [CLI Integration](#cli-integration)
4. [HTTP REST API Integration](#http-rest-api-integration)
5. [Schedule Format Reference](#schedule-format-reference)
6. [Configuration File Format](#configuration-file-format)
7. [Good Vibes Cosmetic Integration Example](#good-vibes-cosmetic-integration-example)
8. [Troubleshooting](#troubleshooting)
9. [Systemd Integration](#systemd-integration)

---

## Overview

The Solace Backup Suite provides three integration options for external projects:

### Integration Options

| Method | Best For | Latency | Complexity |
|--------|----------|---------|------------|
| **Python API** (`backup_api.py`) | Python applications, automation scripts | In-process | Low |
| **CLI** (`backup_cli.py`) | Shell scripts, CI/CD pipelines, cron jobs | Sub-second | Low |
| **HTTP REST API** (`backup_server.py`) | Multi-language apps, microservices, remote access | Network (~5-50ms) | Medium |

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Solace Backup Suite                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐                   │
│  │   Python API │    │  CLI Tool    │    │  HTTP REST   │                   │
│  │ backup_api.py│    │backup_cli.py │    │backup_server │                   │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘                   │
│         │                   │                   │                           │
│         └───────────────────┼───────────────────┘                           │
│                             │                                               │
│                    ┌────────▼────────┐                                      │
│                    │  BackupManager  │                                      │
│                    │   (Core API)    │                                      │
│                    └────────┬────────┘                                      │
│                             │                                               │
│         ┌───────────────────┼───────────────────┐                          │
│         │                   │                   │                          │
│  ┌──────▼──────┐   ┌────────▼────────┐  ┌──────▼──────┐                   │
│  │  Job Queue  │   │   Scheduler     │  │   Config    │                   │
│  │  Manager    │   │  (APScheduler)  │  │   Storage   │                   │
│  └──────┬──────┘   └────────┬────────┘  └─────────────┘                   │
│         │                   │                                               │
│  ┌──────▼───────────────────▼──────┐                                       │
│  │      Backup Execution Engine     │                                       │
│  │  ┌─────────┐  ┌─────────────┐   │                                       │
│  │  │  rsync  │  │   robocopy  │   │  Platform-specific copy tools         │
│  │  │ (Linux) │  │  (Windows)  │   │                                       │
│  │  └────┬────┘  └──────┬──────┘   │                                       │
│  │       └──────────────┘          │                                       │
│  │              │                   │                                       │
│  │       ┌──────▼──────┐           │                                       │
│  │       │ ZIP Archive │           │                                       │
│  │       │  Creation   │           │                                       │
│  │       └──────┬──────┘           │                                       │
│  │              │                   │                                       │
│  │       ┌──────▼──────┐           │                                       │
│  │       │  Retention  │           │                                       │
│  │       │   Cleanup   │           │                                       │
│  │       └─────────────┘           │                                       │
│  └─────────────────────────────────┘                                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │ Writes to
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Backup Storage                                       │
│  /mnt/backups/goodvibes/                                                    │
│  ├── good_vibes_db_2026-02-17_02-00-00.zip                                  │
│  ├── good_vibes_db_2026-02-16_02-00-00.zip                                  │
│  └── good_vibes_assets_2026-02-17_03-00-00.zip                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### When to Use Each Option

#### Python API (`backup_api.py`)
- Your project is written in Python
- You need tight integration with your application
- You want to handle events/callbacks in real-time
- You need programmatic control over backup lifecycle

#### CLI (`backup_cli.py`)
- You're writing shell scripts or Bash automation
- You're integrating with CI/CD pipelines (GitHub Actions, GitLab CI, Jenkins)
- You want simple cron-based scheduling
- You prefer command-line interfaces for system administration

#### HTTP REST API (`backup_server.py`)
- Your project uses a different programming language
- You need remote backup management across a network
- You're building a microservices architecture
- You want to integrate with monitoring tools like Grafana or custom dashboards

---

## Python API Integration

### Installation/Import

The Python API is a standalone module with no GUI dependencies. Simply import it:

```python
from backup_api import BackupManager
```

**Requirements:**
- Python 3.8+
- `apscheduler` (optional, for scheduling): `pip install apscheduler`

### BackupManager Methods

#### Initialization

```python
# Default initialization
bm = BackupManager()

# Custom configuration directory
bm = BackupManager(config_dir="/etc/goodvibes/backup-config")

# Custom status file for monitoring
bm = BackupManager(
    config_dir="/etc/goodvibes/backup-config",
    status_file="/var/run/goodvibes_backup_status.json"
)
```

#### Job Management

##### `create_job()`

Create a new backup job.

```python
result = bm.create_job(
    name="good_vibes_database",           # Unique job identifier
    source_dir="/var/lib/goodvibes/db",   # Source directory to backup
    destination_base="/mnt/backups/goodvibes",  # Where to store backups
    schedule="daily@02:00",               # Schedule format (see reference)
    enabled=True,                         # Enable immediately
    exclusions=["*.tmp", "*.log", "cache/*"],  # Patterns to exclude
    volumes_to_keep=30                    # Retention policy (overrides global)
)

# Result format
{
    "success": True,
    "data": {
        "name": "good_vibes_database",
        "source_dir": "/var/lib/goodvibes/db",
        "destination_base": "/mnt/backups/goodvibes",
        "schedule": "daily@02:00",
        "enabled": True,
        "exclusions": ["*.tmp", "*.log", "cache/*"],
        "volumes_to_keep_override": 30,
        "last_run": None,
        "last_status": None
    },
    "message": "Job 'good_vibes_database' created successfully"
}
```

##### `list_jobs()`

List all configured backup jobs.

```python
jobs = bm.list_jobs()
# Returns: List[Dict[str, Any]]

# Example output
[
    {
        "name": "good_vibes_database",
        "source_dir": "/var/lib/goodvibes/db",
        "destination_base": "/mnt/backups/goodvibes",
        "schedule": "daily@02:00",
        "enabled": True,
        "exclusions": ["*.tmp"],
        "volumes_to_keep_override": 30,
        "last_run": "2026-02-17T02:15:30",
        "last_status": "success"
    }
]
```

##### `get_job()`

Get details of a specific job.

```python
job = bm.get_job("good_vibes_database")
# Returns: Dict[str, Any] or None
```

##### `update_job()`

Update job configuration.

```python
result = bm.update_job(
    "good_vibes_database",
    schedule="daily@03:00",           # Change schedule
    volumes_to_keep=60,               # Change retention
    enabled=False                     # Disable job
)

# Available fields: name, source_dir, destination_base, schedule, 
#                   enabled, exclusions, volumes_to_keep
```

##### `delete_job()`

Remove a backup job.

```python
result = bm.delete_job("good_vibes_database")
# Result: {"success": True, "message": "Job 'good_vibes_database' deleted successfully"}
```

##### `enable_job()` / `disable_job()`

Enable or disable a job.

```python
bm.enable_job("good_vibes_database")
bm.disable_job("good_vibes_database")
```

#### Backup Execution

##### `run_job()`

Execute a backup job immediately.

```python
# Non-blocking (returns immediately, runs in background)
result = bm.run_job("good_vibes_database", blocking=False)
# Result: {"success": True, "message": "Job 'good_vibes_database' queued for execution", "job_id": "good_vibes_database"}

# Blocking (waits for completion)
result = bm.run_job("good_vibes_database", blocking=True)
# Result:
{
    "success": True,
    "data": {
        "success": True,
        "job_id": "good_vibes_database",
        "duration": 45.2,
        "message": "Backup completed successfully",
        "zip_file": "/mnt/backups/goodvibes/good_vibes_database_2026-02-17_02-00-00.zip"
    },
    "message": "Backup completed"
}
```

##### `run_all_enabled_jobs()`

Run all enabled jobs sequentially.

```python
result = bm.run_all_enabled_jobs()
# Result: {"success": True, "message": "Queued 3 jobs for execution", "queued_jobs": ["job1", "job2", "job3"]}
```

#### Job Status

##### `get_job_status()`

Get current status of a job.

```python
status = bm.get_job_status("good_vibes_database")
# Result:
{
    "success": True,
    "data": {
        "job_id": "good_vibes_database",
        "enabled": True,
        "is_running": False,
        "last_run": "2026-02-17T02:15:30",
        "last_status": "success",
        "next_run": "2026-02-18T02:00:00",
        "schedule": "daily@02:00"
    }
}
```

##### `is_job_running()` / `get_current_job()`

Check if any job is running.

```python
if bm.is_job_running():
    current = bm.get_current_job()
    print(f"Backup in progress: {current}")
```

#### Scheduler Management

##### `start_scheduler()` / `stop_scheduler()`

Control the background scheduler.

```python
# Start scheduler for automated backups
result = bm.start_scheduler()
# Result: {"success": True, "message": "Scheduler started successfully"}

# Stop scheduler
result = bm.stop_scheduler()
```

##### `get_scheduler_status()`

Get scheduler status.

```python
status = bm.get_scheduler_status()
# Result:
{
    "success": True,
    "data": {
        "running": True,
        "jobs": [
            {
                "id": "good_vibes_database",
                "name": "good_vibes_database",
                "next_run": "2026-02-18T02:00:00+00:00",
                "trigger": "cron[hour='2', minute='0']"
            }
        ]
    }
}
```

##### `reload_scheduler()`

Reload scheduler configuration from file.

```python
bm.reload_scheduler()
```

#### Logging & Monitoring

##### `get_logs()`

Retrieve recent log entries.

```python
# Get all recent logs
logs = bm.get_logs(lines=100)

# Get logs for specific job
logs = bm.get_logs(job_id="good_vibes_database", lines=50)
```

##### `export_status_file()`

Export current status to JSON file.

```python
bm.export_status_file()
# Creates/updates /tmp/solace_backup_status.json
```

### Error Handling Patterns

All methods return a result dictionary with a `success` key. Always check this:

```python
result = bm.create_job(...)

if result["success"]:
    print(f"Job created: {result['data']['id']}")
else:
    error_code = result.get("code", "UNKNOWN")
    error_message = result.get("error", "Unknown error")
    print(f"Error ({error_code}): {error_message}")
    
    # Handle specific error codes
    if error_code == "DUPLICATE_JOB":
        print("A job with this name already exists")
    elif error_code == "INVALID_SOURCE":
        print("Source directory does not exist")
    elif error_code == "INVALID_SCHEDULE":
        print("Invalid schedule format")
```

**Common Error Codes:**

| Code | Description | Resolution |
|------|-------------|------------|
| `INVALID_NAME` | Job name is empty | Provide a valid name |
| `DUPLICATE_JOB` | Job name already exists | Use a different name or delete the old job |
| `INVALID_SOURCE` | Source directory doesn't exist | Check the path |
| `INVALID_SCHEDULE` | Bad schedule format | Use format reference |
| `NOT_FOUND` | Job not found | Check job name |
| `JOB_DISABLED` | Job is disabled | Enable job first |
| `SCHEDULER_UNAVAILABLE` | APScheduler not installed | `pip install apscheduler` |
| `SAVE_ERROR` | Failed to save config | Check file permissions |

### Event Callbacks

#### Log Callbacks

Receive log messages in real-time:

```python
def log_handler(message: str):
    # Send to your logging system
    print(f"[BACKUP] {message}")
    # Or: logger.info(message)
    # Or: send_to_slack(message)

bm.add_log_callback(log_handler)
```

#### Status Callbacks

Receive structured status updates:

```python
def status_handler(job_name: str, step: int, total: int, message: str):
    percent = (step / total) * 100
    print(f"[{job_name}] {percent:.0f}% - {message}")
    # Update progress bar, send to dashboard, etc.

bm.add_status_callback(status_handler)

# When backup runs:
# [good_vibes_database] 25% - Starting...
# [good_vibes_database] 50% - Copying files...
# [good_vibes_database] 75% - Zipping files...
# [good_vibes_database] 100% - Finished Successfully!
```

### Complete Working Example for Good Vibes Cosmetic

```python
#!/usr/bin/env python3
"""
Good Vibes Cosmetic - Backup Integration Example

This script sets up automated backups for a production application.
"""

import sys
import time
from backup_api import BackupManager

# Configuration
CONFIG_DIR = "/etc/goodvibes/backup-config"
STATUS_FILE = "/var/run/goodvibes_backup_status.json"

# Progress callback for monitoring
def on_status_update(job_name: str, step: int, total: int, message: str):
    """Called during backup progress"""
    percent = int((step / total) * 100)
    print(f"\r[{job_name}] {percent}% - {message}", end="", flush=True)
    if step == total:
        print()  # New line on completion

def on_log_message(message: str):
    """Called for each log line"""
    # Could send to centralized logging (ELK, Splunk, etc.)
    if "ERROR" in message or "CRITICAL" in message:
        print(f"[ALERT] {message}")
        # send_alert_to_pagerduty(message)

def setup_backups():
    """Initialize backup configuration for Good Vibes Cosmetic"""
    
    # Initialize manager
    bm = BackupManager(
        config_dir=CONFIG_DIR,
        status_file=STATUS_FILE
    )
    
    # Add monitoring callbacks
    bm.add_status_callback(on_status_update)
    bm.add_log_callback(on_log_message)
    
    print("Setting up Good Vibes Cosmetic backup jobs...")
    
    # 1. Database backup - Daily at 2 AM, keep 30 days
    db_result = bm.create_job(
        name="good_vibes_database",
        source_dir="/var/lib/goodvibes/database",
        destination_base="/mnt/backups/goodvibes",
        schedule="daily@02:00",
        volumes_to_keep=30,
        exclusions=["*.tmp", "*.log", "wal_archive/*"]
    )
    
    if db_result["success"]:
        print(f"✓ Database backup job created")
    else:
        if db_result.get("code") == "DUPLICATE_JOB":
            print(f"✓ Database backup job already exists")
        else:
            print(f"✗ Failed to create database backup: {db_result['error']}")
            return False
    
    # 2. Assets backup - Daily at 3 AM, keep 14 days
    assets_result = bm.create_job(
        name="good_vibes_assets",
        source_dir="/var/www/goodvibes/uploads",
        destination_base="/mnt/backups/goodvibes-assets",
        schedule="daily@03:00",
        volumes_to_keep=14,
        exclusions=["*.tmp", "thumbnails/cache/*"]
    )
    
    if assets_result["success"]:
        print(f"✓ Assets backup job created")
    else:
        if assets_result.get("code") == "DUPLICATE_JOB":
            print(f"✓ Assets backup job already exists")
        else:
            print(f"✗ Failed to create assets backup: {assets_result['error']}")
    
    # 3. Configuration backup - Weekly on Sunday at 4 AM
    config_result = bm.create_job(
        name="good_vibes_config",
        source_dir="/etc/goodvibes",
        destination_base="/mnt/backups/goodvibes-config",
        schedule="weekly@sun:04:00",
        volumes_to_keep=8,
        enabled=True
    )
    
    if config_result["success"]:
        print(f"✓ Config backup job created")
    
    # Start scheduler for automated backups
    sched_result = bm.start_scheduler()
    if sched_result["success"]:
        print(f"✓ Scheduler started")
    else:
        print(f"✗ Failed to start scheduler: {sched_result.get('error')}")
    
    # Export status for monitoring
    bm.export_status_file()
    print(f"✓ Status exported to {STATUS_FILE}")
    
    return bm

def run_pre_deployment_backup(bm: BackupManager):
    """Run immediate backup before deployment"""
    print("\nRunning pre-deployment backup...")
    
    result = bm.run_job("good_vibes_database", blocking=True)
    
    if result["success"] and result["data"].get("success"):
        duration = result["data"]["duration"]
        print(f"✓ Backup completed in {duration:.1f} seconds")
        return True
    else:
        print(f"✗ Backup failed!")
        # Could abort deployment here
        return False

def get_backup_status(bm: BackupManager):
    """Print current backup status"""
    print("\n=== Backup Status ===")
    
    for job_name in ["good_vibes_database", "good_vibes_assets", "good_vibes_config"]:
        status = bm.get_job_status(job_name)
        if status["success"]:
            data = status["data"]
            state = "●" if data["enabled"] else "○"
            print(f"{state} {job_name}")
            print(f"   Last run: {data['last_run'] or 'Never'}")
            print(f"   Status: {data['last_status'] or 'N/A'}")
            if data["next_run"]:
                print(f"   Next run: {data['next_run']}")

if __name__ == "__main__":
    # Setup backups
    bm = setup_backups()
    
    # Show status
    get_backup_status(bm)
    
    # If called with --pre-deploy flag, run immediate backup
    if "--pre-deploy" in sys.argv:
        success = run_pre_deployment_backup(bm)
        sys.exit(0 if success else 1)
```

---

## CLI Integration

### Installation

The CLI is a standalone Python script:

```bash
# Make executable
chmod +x backup_cli.py

# Run directly
python backup_cli.py --help

# Or add to PATH
sudo cp backup_cli.py /usr/local/bin/solace-backup
solace-backup --help
```

### Job Management Commands

#### Create Job

```bash
# Basic job
python backup_cli.py create good_vibes_db \
  --source /var/lib/goodvibes/database \
  --dest /mnt/backups/goodvibes

# With schedule and retention
python backup_cli.py create good_vibes_db \
  --source /var/lib/goodvibes/database \
  --dest /mnt/backups/goodvibes \
  --schedule "daily@02:00" \
  --keep 30 \
  --exclude "*.tmp" \
  --exclude "*.log" \
  --exclude "cache/*"

# Create disabled job
python backup_cli.py create test_backup \
  --source /tmp/test \
  --dest /tmp/backups \
  --disabled
```

#### List Jobs

```bash
# Human-readable table
python backup_cli.py list

# Output:
# Name           | Source                               | Schedule     | Status
# ---------------|--------------------------------------|--------------|--------
# good_vibes_db  | /var/lib/goodvibes/database          | daily@02:00  | enabled
# good_vibes_as… | /var/www/goodvibes/uploads           | daily@03:00  | enabled

# JSON output (for scripting)
python backup_cli.py list --json
```

#### Show Job Details

```bash
python backup_cli.py show good_vibes_db

# Output:
# Job: good_vibes_db
# ==================================================
#   Source: /var/lib/goodvibes/database
#   Destination: /mnt/backups/goodvibes
#   Schedule: daily@02:00
#   Enabled: Yes
#   Retention: 30 backups (job-specific)
#   Exclusions:
#     - *.tmp
#     - *.log
#
#   Existing Backups (15):
#     - good_vibes_db_2026-02-17_02-00-00.zip (245.67 MB)
#     - good_vibes_db_2026-02-16_02-00-00.zip (243.21 MB)
#     ... and 13 more
```

#### Update Job

```bash
# Update schedule
python backup_cli.py update good_vibes_db \
  --field schedule=daily@03:00

# Update retention
python backup_cli.py update good_vibes_db \
  --field keep=60

# Add exclusion
python backup_cli.py update good_vibes_db \
  --field exclude=temp/*

# Multiple updates
python backup_cli.py update good_vibes_db \
  --field schedule=daily@04:00 \
  --field keep=90 \
  --field enabled=true
```

#### Delete Job

```bash
# Interactive (asks for confirmation in TTY)
python backup_cli.py delete good_vibes_db

# Force (no confirmation)
python backup_cli.py delete good_vibes_db --force
```

#### Enable/Disable Jobs

```bash
python backup_cli.py enable good_vibes_db
python backup_cli.py disable good_vibes_db
```

### Backup Execution Commands

#### Run Single Job

```bash
# Non-blocking (returns immediately)
python backup_cli.py run good_vibes_db

# Blocking (waits for completion with progress output)
python backup_cli.py run good_vibes_db --blocking --verbose

# Useful for pre-deployment hooks
python backup_cli.py run good_vibes_db --blocking && echo "Backup OK, proceeding with deployment"
```

#### Run All Enabled Jobs

```bash
python backup_cli.py run-all --verbose
```

### Scheduler Daemon Commands

#### Start Scheduler

```bash
# Foreground (good for testing)
python backup_cli.py scheduler start
# Press Ctrl+C to stop

# Daemon mode (background)
python backup_cli.py scheduler start --daemon
```

#### Stop Scheduler

```bash
python backup_cli.py scheduler stop
```

#### Check Scheduler Status

```bash
# Human-readable
python backup_cli.py scheduler status

# JSON for scripts
python backup_cli.py scheduler status --json

# Output:
# ● Scheduler is running
#   PID: 12345
#
# Scheduled Jobs:
#   [✓] good_vibes_db (daily@02:00)
#   [✓] good_vibes_assets (daily@03:00)
```

#### Reload Configuration

```bash
# Reload jobs without restarting scheduler
python backup_cli.py scheduler reload
```

### Log & Status Commands

#### View Logs

```bash
# Last 50 lines
python backup_cli.py logs

# Last 100 lines
python backup_cli.py logs --lines 100

# Follow (like tail -f)
python backup_cli.py logs --tail

# Job-specific logs
python backup_cli.py logs good_vibes_db --lines 50
```

#### Check Status

```bash
# All jobs
python backup_cli.py status

# Specific job
python backup_cli.py status good_vibes_db

# JSON output
python backup_cli.py status --json

# Output:
# Name              | Status  | Backups | Latest
# ------------------|---------|---------|------------------
# good_vibes_db     | enabled | 15      | 2026-02-17 02:00
# good_vibes_assets | enabled | 14      | 2026-02-17 03:00
```

### Exit Codes Reference

| Code | Meaning | When to Use |
|------|---------|-------------|
| `0` | Success | Command executed successfully |
| `1` | General error | Unexpected error occurred |
| `2` | Invalid arguments | Wrong command-line arguments |
| `3` | Job not found | Specified job doesn't exist |
| `4` | Backup execution failed | Backup process failed |
| `5` | Scheduler error | Scheduler couldn't start/stop |

### Bash Script Integration Examples

#### Setup Script

```bash
#!/bin/bash
# Good Vibes Cosmetic Backup Setup Script
# Run once to configure backups

set -e  # Exit on error

# Configuration
BACKUP_CONFIG_DIR="/etc/goodvibes/backup-config"
BACKUP_CLI="python /opt/solace-backup/backup_cli.py"

# Create config directory
mkdir -p "$BACKUP_CONFIG_DIR"

echo "=== Good Vibes Cosmetic Backup Setup ==="

# Create database backup job
echo "Creating database backup job..."
$BACKUP_CLI create good_vibes_db \
  --source /var/lib/goodvibes/database \
  --dest /mnt/backups/goodvibes \
  --schedule "daily@02:00" \
  --keep 30 \
  --exclude "*.tmp" \
  --exclude "*.log"

# Create assets backup job  
echo "Creating assets backup job..."
$BACKUP_CLI create good_vibes_assets \
  --source /var/www/goodvibes/uploads \
  --dest /mnt/backups/goodvibes-assets \
  --schedule "daily@03:00" \
  --keep 14

# Create config backup job
echo "Creating config backup job..."
$BACKUP_CLI create good_vibes_config \
  --source /etc/goodvibes \
  --dest /mnt/backups/goodvibes-config \
  --schedule "weekly@sun:04:00" \
  --keep 8

# Start scheduler daemon
echo "Starting scheduler daemon..."
$BACKUP_CLI scheduler start --daemon

echo ""
echo "=== Backup configuration complete! ==="
$BACKUP_CLI scheduler status
```

#### Pre-Deployment Hook

```bash
#!/bin/bash
# Pre-deployment backup hook
# Add to deployment pipeline before applying changes

BACKUP_CLI="python /opt/solace-backup/backup_cli.py"
DEPLOYMENT_ID="${1:-$(date +%s)}"

echo "=== Pre-Deployment Backup ==="
echo "Deployment ID: $DEPLOYMENT_ID"

# Run database backup with blocking
if $BACKUP_CLI run good_vibes_db --blocking --verbose; then
    echo "✓ Backup successful, proceeding with deployment"
    
    # Tag the backup with deployment ID for easy rollback
    LATEST_BACKUP=$(ls -t /mnt/backups/goodvibes/good_vibes_db_*.zip | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
        TAG_FILE="${LATEST_BACKUP%.zip}_${DEPLOYMENT_ID}.tag"
        echo "Deployment: $DEPLOYMENT_ID" > "$TAG_FILE"
        echo "✓ Tagged backup: $TAG_FILE"
    fi
    
    exit 0
else
    echo "✗ Backup failed! Aborting deployment."
    # Send alert
    curl -X POST "$SLACK_WEBHOOK" \
      -H 'Content-type: application/json' \
      --data '{"text":"🚨 Backup failed before deployment!"}'
    exit 1
fi
```

#### Health Check for Monitoring

```bash
#!/bin/bash
# Backup health check for Nagios/Zabbix/etc

BACKUP_CLI="python /opt/solace-backup/backup_cli.py"
STATUS_FILE="/tmp/backup_health.json"

# Get status
$BACKUP_CLI status --json > "$STATUS_FILE"

# Check each enabled job
WARNINGS=0
ERRORS=0

for job in good_vibes_db good_vibes_assets; do
    LATEST=$(python3 -c "
import json, sys
data = json.load(open('$STATUS_FILE'))
for j in data['jobs']:
    if j['name'] == '$job':
        print(j.get('latest_backup', 'N/A'))
        sys.exit(0)
print('N/A')
")
    
    if [ "$LATEST" = "N/A" ]; then
        echo "CRITICAL: $job has no backups!"
        ERRORS=$((ERRORS + 1))
    else
        # Check if backup is recent (within 25 hours)
        LATEST_TS=$(date -d "$LATEST" +%s 2>/dev/null || echo 0)
        NOW_TS=$(date +%s)
        AGE_HOURS=$(( (NOW_TS - LATEST_TS) / 3600 ))
        
        if [ $AGE_HOURS -gt 25 ]; then
            echo "WARNING: $job backup is ${AGE_HOURS} hours old"
            WARNINGS=$((WARNINGS + 1))
        else
            echo "OK: $job backup is ${AGE_HOURS} hours old"
        fi
    fi
done

# Exit with appropriate code for monitoring systems
if [ $ERRORS -gt 0 ]; then
    exit 2  # CRITICAL
elif [ $WARNINGS -gt 0 ]; then
    exit 1  # WARNING
else
    exit 0  # OK
fi
```

---

## HTTP REST API Integration

### Server Startup Options

```bash
# Default (localhost:8777)
python backup_server.py

# Custom host/port
python backup_server.py --host 0.0.0.0 --port 8080

# With custom config directory
python backup_server.py --config-dir /etc/goodvibes/backup-config

# Daemon mode
python backup_server.py --daemon

# Stop daemon
python backup_server.py --stop

# Check status
python backup_server.py --status
```

### Authentication

Set API key via environment variable:

```bash
export SOLACE_BACKUP_API_KEY="your-secret-api-key-here"
python backup_server.py
```

Then include in requests:

```bash
curl -H "X-API-Key: your-secret-api-key-here" http://localhost:8777/api/jobs
```

### API Endpoints

#### Health Check

```bash
GET /api/health
```

Response:
```json
{
  "success": true,
  "status": "healthy",
  "timestamp": "2026-02-17T10:30:00Z",
  "version": "1.0.0"
}
```

#### Job Management

##### List Jobs

```bash
GET /api/jobs
```

Response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": [
    {
      "name": "good_vibes_db",
      "source_dir": "/var/lib/goodvibes/database",
      "destination_base": "/mnt/backups/goodvibes",
      "schedule": "daily@02:00",
      "enabled": true,
      "exclusions": ["*.tmp", "*.log"],
      "volumes_to_keep_override": 30,
      "status": {
        "job_id": "good_vibes_db",
        "enabled": true,
        "is_running": false,
        "last_run": "2026-02-17T02:15:30",
        "last_status": "success",
        "next_run": "2026-02-18T02:00:00",
        "schedule": "daily@02:00"
      }
    }
  ],
  "count": 1
}
```

##### Get Job

```bash
GET /api/jobs/{job_id}
```

Response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": {
    "name": "good_vibes_db",
    "source_dir": "/var/lib/goodvibes/database",
    "destination_base": "/mnt/backups/goodvibes",
    "schedule": "daily@02:00",
    "enabled": true,
    "exclusions": ["*.tmp", "*.log"],
    "volumes_to_keep_override": 30,
    "status": {
      "job_id": "good_vibes_db",
      "enabled": true,
      "is_running": false,
      "last_run": "2026-02-17T02:15:30",
      "last_status": "success",
      "next_run": "2026-02-18T02:00:00",
      "schedule": "daily@02:00"
    }
  }
}
```

##### Create Job

```bash
POST /api/jobs
Content-Type: application/json

{
  "name": "good_vibes_db",
  "source_dir": "/var/lib/goodvibes/database",
  "destination_base": "/mnt/backups/goodvibes",
  "schedule": "daily@02:00",
  "enabled": true,
  "exclusions": ["*.tmp", "*.log"],
  "volumes_to_keep": 30
}
```

Response (201 Created):
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": {
    "name": "good_vibes_db",
    "source_dir": "/var/lib/goodvibes/database",
    "destination_base": "/mnt/backups/goodvibes",
    "schedule": "daily@02:00",
    "enabled": true,
    "exclusions": ["*.tmp", "*.log"],
    "volumes_to_keep_override": 30
  },
  "message": "Job 'good_vibes_db' created successfully"
}
```

##### Update Job

```bash
PUT /api/jobs/{job_id}
Content-Type: application/json

{
  "schedule": "daily@03:00",
  "volumes_to_keep": 60,
  "enabled": false
}
```

##### Delete Job

```bash
DELETE /api/jobs/{job_id}
```

Response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "message": "Job 'good_vibes_db' deleted successfully"
}
```

##### Enable/Disable Job

```bash
POST /api/jobs/{job_id}/enable
POST /api/jobs/{job_id}/disable
```

#### Job Execution

##### Run Job

```bash
POST /api/jobs/{job_id}/run
Content-Type: application/json

{
  "blocking": false  // or true
}
```

Non-blocking response (202 Accepted):
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": {
    "job_id": "good_vibes_db",
    "status": "started",
    "message": "Backup job started in background"
  }
}
```

Blocking response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": {
    "job_id": "good_vibes_db",
    "status": "completed",
    "duration": 45.2,
    "files_processed": 15420,
    "backup_size": 268435456,
    "zip_file": "/mnt/backups/goodvibes/good_vibes_db_2026-02-17_10-30-00.zip"
  }
}
```

#### Scheduler Management

##### Get Scheduler Status

```bash
GET /api/scheduler/status
```

Response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": {
    "running": true,
    "jobs": [
      {
        "id": "good_vibes_db",
        "name": "good_vibes_db",
        "next_run": "2026-02-18T02:00:00+00:00",
        "trigger": "cron[hour='2', minute='0']"
      }
    ],
    "jobs_scheduled": 1,
    "uptime": "2 hours, 15 minutes"
  }
}
```

##### Start/Stop/Reload Scheduler

```bash
POST /api/scheduler/start
POST /api/scheduler/stop
POST /api/scheduler/reload
```

#### Logs

```bash
GET /api/logs?lines=100
GET /api/logs/{job_id}?lines=50
```

Response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": [
    "[2026-02-17 02:00:00] [good_vibes_db] Starting backup...",
    "[2026-02-17 02:00:15] [good_vibes_db] Copying files...",
    "..."
  ],
  "count": 100
}
```

#### System Status

```bash
GET /api/status
```

Response:
```json
{
  "success": true,
  "timestamp": "2026-02-17T10:30:00",
  "data": {
    "status": "idle",
    "current_job": null,
    "job_count": 3,
    "enabled_jobs": 3,
    "scheduler_running": true,
    "apscheduler_available": true,
    "version": "1.0.0"
  }
}
```

### Integration Examples

#### cURL Examples

```bash
# Create job
curl -X POST http://localhost:8777/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "good_vibes_db",
    "source_dir": "/var/lib/goodvibes/database",
    "destination_base": "/mnt/backups/goodvibes",
    "schedule": "daily@02:00",
    "volumes_to_keep": 30
  }'

# List jobs
curl http://localhost:8777/api/jobs

# Run job (blocking)
curl -X POST http://localhost:8777/api/jobs/good_vibes_db/run \
  -H "Content-Type: application/json" \
  -d '{"blocking": true}'

# Check status
curl http://localhost:8777/api/status

# Get logs
curl "http://localhost:8777/api/logs/good_vibes_db?lines=50"
```

#### Python (requests)

```python
import requests
import json

BASE_URL = "http://localhost:8777/api"
API_KEY = "your-api-key"  # Optional

headers = {"Content-Type": "application/json"}
if API_KEY:
    headers["X-API-Key"] = API_KEY

def create_backup_job(name, source, dest, schedule, keep=30):
    """Create a backup job via REST API"""
    response = requests.post(
        f"{BASE_URL}/jobs",
        headers=headers,
        json={
            "name": name,
            "source_dir": source,
            "destination_base": dest,
            "schedule": schedule,
            "volumes_to_keep": keep
        }
    )
    return response.json()

def run_backup_job(job_id, blocking=False):
    """Execute a backup job"""
    response = requests.post(
        f"{BASE_URL}/jobs/{job_id}/run",
        headers=headers,
        json={"blocking": blocking}
    )
    return response.json()

def get_job_status(job_id):
    """Get job status"""
    response = requests.get(
        f"{BASE_URL}/jobs/{job_id}",
        headers=headers
    )
    return response.json()

# Example usage
if __name__ == "__main__":
    # Create job
    result = create_backup_job(
        "good_vibes_db",
        "/var/lib/goodvibes/database",
        "/mnt/backups/goodvibes",
        "daily@02:00",
        30
    )
    print(f"Create result: {result}")
    
    # Run backup
    result = run_backup_job("good_vibes_db", blocking=True)
    print(f"Run result: {result}")
```

#### JavaScript (fetch)

```javascript
const BASE_URL = 'http://localhost:8777/api';
const API_KEY = 'your-api-key'; // Optional

const headers = {
  'Content-Type': 'application/json',
  ...(API_KEY && { 'X-API-Key': API_KEY })
};

async function createBackupJob(name, source, dest, schedule, keep = 30) {
  const response = await fetch(`${BASE_URL}/jobs`, {
    method: 'POST',
    headers,
    body: JSON.stringify({
      name,
      source_dir: source,
      destination_base: dest,
      schedule,
      volumes_to_keep: keep
    })
  });
  return response.json();
}

async function runBackupJob(jobId, blocking = false) {
  const response = await fetch(`${BASE_URL}/jobs/${jobId}/run`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ blocking })
  });
  return response.json();
}

async function getJobStatus(jobId) {
  const response = await fetch(`${BASE_URL}/jobs/${jobId}`, { headers });
  return response.json();
}

// Example usage
async function setupBackups() {
  try {
    const result = await createBackupJob(
      'good_vibes_db',
      '/var/lib/goodvibes/database',
      '/mnt/backups/goodvibes',
      'daily@02:00',
      30
    );
    console.log('Job created:', result);
    
    const status = await getJobStatus('good_vibes_db');
    console.log('Status:', status);
  } catch (error) {
    console.error('Error:', error);
  }
}

setupBackups();
```

#### Go

```go
package main

import (
    "bytes"
    "encoding/json"
    "fmt"
    "net/http"
)

const baseURL = "http://localhost:8777/api"

type JobRequest struct {
    Name            string   `json:"name"`
    SourceDir       string   `json:"source_dir"`
    DestinationBase string   `json:"destination_base"`
    Schedule        string   `json:"schedule"`
    VolumesToKeep   int      `json:"volumes_to_keep"`
}

type APIResponse struct {
    Success   bool        `json:"success"`
    Timestamp string      `json:"timestamp"`
    Data      interface{} `json:"data,omitempty"`
    Error     string      `json:"error,omitempty"`
}

func createJob(job JobRequest) (*APIResponse, error) {
    body, _ := json.Marshal(job)
    resp, err := http.Post(
        baseURL+"/jobs",
        "application/json",
        bytes.NewBuffer(body),
    )
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    
    var result APIResponse
    json.NewDecoder(resp.Body).Decode(&result)
    return &result, nil
}

func runJob(jobID string, blocking bool) (*APIResponse, error) {
    body, _ := json.Marshal(map[string]bool{"blocking": blocking})
    resp, err := http.Post(
        fmt.Sprintf("%s/jobs/%s/run", baseURL, jobID),
        "application/json",
        bytes.NewBuffer(body),
    )
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    
    var result APIResponse
    json.NewDecoder(resp.Body).Decode(&result)
    return &result, nil
}

func main() {
    result, err := createJob(JobRequest{
        Name:            "good_vibes_db",
        SourceDir:       "/var/lib/goodvibes/database",
        DestinationBase: "/mnt/backups/goodvibes",
        Schedule:        "daily@02:00",
        VolumesToKeep:   30,
    })
    if err != nil {
        panic(err)
    }
    fmt.Printf("Result: %+v\n", result)
}
```

---

## Schedule Format Reference

The scheduler supports several schedule formats:

| Format | Description | Example |
|--------|-------------|---------|
| `manual` | No automatic scheduling | `"manual"` |
| `daily@HH:MM` | Daily at specific time | `"daily@02:00"` - Every day at 2:00 AM |
| `interval@MINUTES` | Interval in minutes | `"interval@60"` - Every hour |
| `weekly@DAY:HH:MM` | Weekly on specific day | `"weekly@sun:03:00"` - Every Sunday at 3:00 AM |

### Day Abbreviations (for weekly)

- `mon` - Monday
- `tue` - Tuesday
- `wed` - Wednesday
- `thu` - Thursday
- `fri` - Friday
- `sat` - Saturday
- `sun` - Sunday

### Examples

```python
# Daily at 2 AM
schedule="daily@02:00"

# Every 6 hours
schedule="interval@360"

# Every Monday at 4:30 AM
schedule="weekly@mon:04:30"

# Every Friday at 11 PM
schedule="weekly@fri:23:00"
```

---

## Configuration File Format

The configuration is stored as JSON:

```json
{
  "global_settings": {
    "default_volumes_to_keep": 3,
    "default_backup_base_name": "Backups_Py",
    "theme": "Light (Default)"
  },
  "backup_jobs": [
    {
      "name": "good_vibes_database",
      "source_dir": "/var/lib/goodvibes/database",
      "destination_base": "/mnt/backups/goodvibes",
      "schedule": "daily@02:00",
      "enabled": true,
      "exclusions": ["*.tmp", "*.log", "cache/*"],
      "volumes_to_keep_override": 30,
      "last_run": "2026-02-17T02:15:30",
      "last_status": "success"
    },
    {
      "name": "good_vibes_assets",
      "source_dir": "/var/www/goodvibes/uploads",
      "destination_base": "/mnt/backups/goodvibes-assets",
      "schedule": "daily@03:00",
      "enabled": true,
      "exclusions": ["*.tmp"],
      "volumes_to_keep_override": 14
    }
  ]
}
```

### Field Descriptions

#### Global Settings

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_volumes_to_keep` | int | 3 | Number of backups to retain by default |
| `default_backup_base_name` | string | "Backups_Py" | Default prefix for backup files |
| `theme` | string | "Light (Default)" | UI theme (GUI only) |

#### Backup Jobs

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Unique job identifier |
| `source_dir` | string | Yes | Directory to backup |
| `destination_base` | string | Yes | Where to store backup archives |
| `schedule` | string | No | Schedule expression (default: "manual") |
| `enabled` | boolean | No | Whether job is enabled (default: true) |
| `exclusions` | array | No | List of patterns to exclude |
| `volumes_to_keep_override` | int | No | Job-specific retention (overrides global) |
| `last_run` | string | Auto | ISO timestamp of last execution |
| `last_status` | string | Auto | "success" or "failed" |

---

## Good Vibes Cosmetic Integration Example

This section provides a complete, production-ready integration for the Good Vibes Cosmetic application.

### Directory Structure

```
/opt/goodvibes-backup/
├── backup-suite/              # Solace Backup Suite source
│   ├── backup_api.py
│   ├── backup_cli.py
│   └── backup_server.py
├── config/                    # Configuration
│   └── backup_config.json
├── scripts/                   # Integration scripts
│   ├── setup.sh
│   ├── pre-deploy.sh
│   └── health-check.sh
├── systemd/                   # Service files
│   └── goodvibes-backup.service
└── logs/                      # Log files
    └── backup.log
```

### Setup Script

```bash
#!/bin/bash
# /opt/goodvibes-backup/scripts/setup.sh

set -e

BACKUP_ROOT="/opt/goodvibes-backup"
CONFIG_DIR="$BACKUP_ROOT/config"
BACKUP_CLI="python3 $BACKUP_ROOT/backup-suite/backup_cli.py"

echo "=== Good Vibes Cosmetic Backup Setup ==="

# Create directories
mkdir -p "$CONFIG_DIR"
mkdir -p /mnt/backups/goodvibes
mkdir -p /mnt/backups/goodvibes-assets
mkdir -p /mnt/backups/goodvibes-config

# Ensure backup directories are owned by backup user
chown -R goodvibes:goodvibes /mnt/backups/

echo "Creating backup jobs..."

# Database backup - Daily at 2 AM, 30 day retention
$BACKUP_CLI create good_vibes_db \
  --source /var/lib/goodvibes/database \
  --dest /mnt/backups/goodvibes \
  --schedule "daily@02:00" \
  --keep 30 \
  --exclude "*.tmp" \
  --exclude "*.log" \
  --exclude "wal_archive/*" \
  || echo "Job may already exist, continuing..."

# Assets backup - Daily at 3 AM, 14 day retention
$BACKUP_CLI create good_vibes_assets \
  --source /var/www/goodvibes/uploads \
  --dest /mnt/backups/goodvibes-assets \
  --schedule "daily@03:00" \
  --keep 14 \
  --exclude "*.tmp" \
  --exclude "thumbnails/cache/*" \
  || echo "Job may already exist, continuing..."

# Config backup - Weekly on Sunday at 4 AM, 8 week retention
$BACKUP_CLI create good_vibes_config \
  --source /etc/goodvibes \
  --dest /mnt/backups/goodvibes-config \
  --schedule "weekly@sun:04:00" \
  --keep 8 \
  || echo "Job may already exist, continuing..."

echo ""
echo "=== Setup Complete ==="
$BACKUP_CLI list
```

### Pre-Deployment Hook

```bash
#!/bin/bash
# /opt/goodvibes-backup/scripts/pre-deploy.sh

set -e

BACKUP_ROOT="/opt/goodvibes-backup"
BACKUP_CLI="python3 $BACKUP_ROOT/backup-suite/backup_cli.py"
DEPLOYMENT_ID="${1:-$(date +%s)}"
SLACK_WEBHOOK="${SLACK_WEBHOOK_URL:-}"

echo "=== Pre-Deployment Backup ==="
echo "Deployment ID: $DEPLOYMENT_ID"
echo "Timestamp: $(date)"

# Run database backup with blocking
if $BACKUP_CLI run good_vibes_db --blocking --verbose; then
    echo "✓ Database backup successful"
    
    # Tag the backup
    LATEST_BACKUP=$(ls -t /mnt/backups/goodvibes/good_vibes_db_*.zip 2>/dev/null | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
        echo "Deployment: $DEPLOYMENT_ID" > "${LATEST_BACKUP%.zip}_${DEPLOYMENT_ID}.tag"
        echo "Backup tagged: $DEPLOYMENT_ID"
    fi
    
    # Log success
    logger -t goodvibes-backup "Pre-deployment backup successful: $DEPLOYMENT_ID"
    
    # Notify Slack if configured
    if [ -n "$SLACK_WEBHOOK" ]; then
        curl -s -X POST "$SLACK_WEBHOOK" \
          -H 'Content-type: application/json' \
          --data "{\"text\":\"✅ Pre-deployment backup successful\\nDeployment: $DEPLOYMENT_ID\\nBackup: $(basename $LATEST_BACKUP)\"}" \
          > /dev/null || true
    fi
    
    exit 0
else
    echo "✗ Database backup failed!"
    logger -t goodvibes-backup "ERROR: Pre-deployment backup failed: $DEPLOYMENT_ID"
    
    # Alert on failure
    if [ -n "$SLACK_WEBHOOK" ]; then
        curl -s -X POST "$SLACK_WEBHOOK" \
          -H 'Content-type: application/json' \
          --data "{\"text\":\"🚨 CRITICAL: Pre-deployment backup failed!\\nDeployment: $DEPLOYMENT_ID\\nDeployment aborted.\"}" \
          > /dev/null || true
    fi
    
    exit 1
fi
```

### Monitoring/Alerting Integration

```python
#!/usr/bin/env python3
# /opt/goodvibes-backup/scripts/monitor.py

"""
Backup monitoring script for Good Vibes Cosmetic.
Integrates with Slack and Prometheus.
"""

import json
import sys
import os
from datetime import datetime, timedelta
from backup_api import BackupManager

# Configuration
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL")
PROMETHEUS_FILE = "/var/lib/prometheus/node_exporter/textfile/goodvibes_backup.prom"

ALERT_THRESHOLDS = {
    "good_vibes_db": 25,      # Hours since last backup
    "good_vibes_assets": 25,
    "good_vibes_config": 169,  # 1 week + 1 hour
}

def send_slack_alert(message, severity="warning"):
    """Send alert to Slack"""
    if not SLACK_WEBHOOK:
        return
    
    import urllib.request
    
    emoji = "🚨" if severity == "critical" else "⚠️"
    payload = json.dumps({
        "text": f"{emoji} Backup Alert\n{message}"
    }).encode()
    
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=payload,
        headers={"Content-type": "application/json"}
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Failed to send Slack alert: {e}")

def write_prometheus_metrics(jobs_status):
    """Write metrics for Prometheus node_exporter"""
    os.makedirs(os.path.dirname(PROMETHEUS_FILE), exist_ok=True)
    
    with open(PROMETHEUS_FILE, "w") as f:
        f.write("# HELP goodvibes_backup_last_success_timestamp Last successful backup timestamp\n")
        f.write("# TYPE goodvibes_backup_last_success_timestamp gauge\n")
        
        for job_name, status in jobs_status.items():
            if status["last_run"]:
                ts = datetime.fromisoformat(status["last_run"]).timestamp()
                f.write(f'goodvibes_backup_last_success_timestamp{{job="{job_name}"}} {ts}\n')
            else:
                f.write(f'goodvibes_backup_last_success_timestamp{{job="{job_name}"}} 0\n')
        
        f.write("# HELP goodvibes_backup_enabled Is backup job enabled\n")
        f.write("# TYPE goodvibes_backup_enabled gauge\n")
        
        for job_name, status in jobs_status.items():
            enabled = 1 if status["enabled"] else 0
            f.write(f'goodvibes_backup_enabled{{job="{job_name}"}} {enabled}\n')

def check_backups():
    """Check backup status and alert if needed"""
    bm = BackupManager(config_dir="/opt/goodvibes-backup/config")
    
    alerts = []
    jobs_status = {}
    
    for job_name in ALERT_THRESHOLDS:
        status = bm.get_job_status(job_name)
        if not status["success"]:
            alerts.append(f"❌ Cannot get status for {job_name}")
            continue
        
        data = status["data"]
        jobs_status[job_name] = data
        
        if not data["enabled"]:
            alerts.append(f"⚠️ {job_name} is disabled")
            continue
        
        if not data["last_run"]:
            alerts.append(f"🚨 {job_name} has never been backed up!")
            continue
        
        last_run = datetime.fromisoformat(data["last_run"])
        hours_since = (datetime.now() - last_run).total_seconds() / 3600
        threshold = ALERT_THRESHOLDS[job_name]
        
        if hours_since > threshold:
            alerts.append(
                f"🚨 {job_name} backup is {hours_since:.1f} hours old "
                f"(threshold: {threshold}h)"
            )
        elif hours_since > threshold * 0.8:
            alerts.append(
                f"⚠️ {job_name} backup is {hours_since:.1f} hours old "
                f"(approaching threshold: {threshold}h)"
            )
    
    # Write Prometheus metrics
    write_prometheus_metrics(jobs_status)
    
    # Send alerts if any
    if alerts:
        message = "\n".join(alerts)
        print(message)
        send_slack_alert(message, severity="critical" if "🚨" in message else "warning")
        return 1
    
    print("✓ All backups healthy")
    return 0

if __name__ == "__main__":
    sys.exit(check_backups())
```

### Log Rotation

```bash
# /etc/logrotate.d/goodvibes-backup
/opt/goodvibes-backup/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 goodvibes goodvibes
    sharedscripts
    postrotate
        # Signal the backup server to reopen logs (if running)
        killall -HUP backup_server.py 2>/dev/null || true
    endscript
}
```

---

## Troubleshooting

### Permission Errors

**Symptom:** `Permission denied` when creating backups

**Solutions:**
1. Ensure backup user has read access to source directories:
   ```bash
   sudo setfacl -R -m u:goodvibes:rx /var/lib/goodvibes/database
   ```

2. Ensure backup user has write access to destination:
   ```bash
   sudo chown -R goodvibes:goodvibes /mnt/backups
   ```

3. Check SELinux (if enabled):
   ```bash
   sudo setsebool -P rsync_full_access 1
   ```

### Schedule Parsing Errors

**Symptom:** `Invalid schedule format` error

**Solutions:**
1. Verify format matches one of:
   - `manual`
   - `daily@HH:MM` (e.g., `daily@02:00`)
   - `interval@MINUTES` (e.g., `interval@60`)
   - `weekly@DAY:HH:MM` (e.g., `weekly@sun:03:00`)

2. Check for 24-hour format (not AM/PM)

3. Verify day abbreviation is 3 letters (`mon`, `tue`, etc.)

### Scheduler Not Starting

**Symptom:** `APScheduler not installed` or scheduler fails to start

**Solutions:**
1. Install APScheduler:
   ```bash
   pip install apscheduler
   ```

2. Check if another scheduler is already running:
   ```bash
   ps aux | grep backup
   cat /tmp/solace_backup_scheduler.pid
   ```

3. Check for port conflicts (HTTP server):
   ```bash
   lsof -i :8777
   ```

### Backup Failures

**Symptom:** Backup fails with copy or zip errors

**Solutions:**
1. Check disk space:
   ```bash
   df -h /mnt/backups
   ```

2. Verify source directory exists:
   ```bash
   python backup_cli.py show <job_name>
   ```

3. Check logs for specific errors:
   ```bash
   python backup_cli.py logs <job_name> --lines 100
   ```

4. Test rsync/robocopy manually:
   ```bash
   # Linux
   rsync -av /source/ /dest/test/
   
   # Windows
   robocopy C:\source C:\dest /E
   ```

### Log Locations

| Component | Log Location |
|-----------|--------------|
| Python API | `Debug/backup_api_debug.log` |
| CLI | `Debug/backup_cli.log` |
| HTTP Server | `/tmp/solace_backup_server.log` |
| Status File | `/tmp/solace_backup_status.json` |

---

## Systemd Integration

### Service File

Create `/etc/systemd/system/goodvibes-backup.service`:

```ini
[Unit]
Description=Good Vibes Cosmetic Backup Scheduler
After=network.target

[Service]
Type=simple
User=goodvibes
Group=goodvibes
WorkingDirectory=/opt/goodvibes-backup
Environment=PYTHONPATH=/opt/goodvibes-backup/backup-suite
Environment=SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# Create backup directories if they don't exist
ExecStartPre=/bin/mkdir -p /mnt/backups/goodvibes
ExecStartPre=/bin/mkdir -p /mnt/backups/goodvibes-assets
ExecStartPre=/bin/mkdir -p /mnt/backups/goodvibes-config

# Start the scheduler daemon
ExecStart=/opt/goodvibes-backup/backup-suite/venv/bin/python \
    /opt/goodvibes-backup/backup-suite/backup_cli.py scheduler start

# Stop the scheduler
ExecStop=/opt/goodvibes-backup/backup-suite/venv/bin/python \
    /opt/goodvibes-backup/backup-suite/backup_cli.py scheduler stop

# Restart policy
Restart=on-failure
RestartSec=5

# Resource limits
LimitNOFILE=65536
MemoryMax=512M

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/mnt/backups /opt/goodvibes-backup/config /tmp

[Install]
WantedBy=multi-user.target
```

### HTTP Server Service

Create `/etc/systemd/system/goodvibes-backup-api.service`:

```ini
[Unit]
Description=Good Vibes Cosmetic Backup API Server
After=network.target

[Service]
Type=simple
User=goodvibes
Group=goodvibes
WorkingDirectory=/opt/goodvibes-backup
Environment=PYTHONPATH=/opt/goodvibes-backup/backup-suite
Environment=SOLACE_BACKUP_API_KEY=your-secret-api-key

ExecStart=/opt/goodvibes-backup/backup-suite/venv/bin/python \
    /opt/goodvibes-backup/backup-suite/backup_server.py \
    --host 127.0.0.1 --port 8777

Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Monitoring Timer

Create `/etc/systemd/system/goodvibes-backup-monitor.service`:

```ini
[Unit]
Description=Good Vibes Cosmetic Backup Health Check

[Service]
Type=oneshot
User=goodvibes
WorkingDirectory=/opt/goodvibes-backup
Environment=SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
ExecStart=/opt/goodvibes-backup/backup-suite/venv/bin/python \
    /opt/goodvibes-backup/scripts/monitor.py
```

Create `/etc/systemd/system/goodvibes-backup-monitor.timer`:

```ini
[Unit]
Description=Run backup health check every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
```

### Commands

```bash
# Reload systemd
sudo systemctl daemon-reload

# Start scheduler
sudo systemctl start goodvibes-backup

# Enable auto-start
sudo systemctl enable goodvibes-backup

# Check status
sudo systemctl status goodvibes-backup

# View logs
sudo journalctl -u goodvibes-backup -f

# Start HTTP API server
sudo systemctl start goodvibes-backup-api
sudo systemctl enable goodvibes-backup-api

# Start monitoring timer
sudo systemctl start goodvibes-backup-monitor.timer
sudo systemctl enable goodvibes-backup-monitor.timer

# Check timer status
sudo systemctl list-timers goodvibes-backup-monitor.timer
```

---

## Quick Reference Card

### Python API Quick Reference

```python
from backup_api import BackupManager

bm = BackupManager()

# Jobs
bm.create_job(name, source, dest, schedule="manual")
bm.list_jobs()
bm.get_job(name)
bm.update_job(name, **fields)
bm.delete_job(name)
bm.enable_job(name) / bm.disable_job(name)

# Execution
bm.run_job(name, blocking=True)
bm.run_all_enabled_jobs()

# Scheduler
bm.start_scheduler()
bm.stop_scheduler()
bm.get_scheduler_status()

# Monitoring
bm.get_job_status(name)
bm.get_logs(job_id=name, lines=100)
```

### CLI Quick Reference

```bash
# Jobs
backup_cli.py create NAME --source DIR --dest DIR [--schedule SPEC] [--keep N]
backup_cli.py list [--json]
backup_cli.py show NAME
backup_cli.py update NAME --field KEY=VALUE
backup_cli.py delete NAME [--force]
backup_cli.py enable NAME / backup_cli.py disable NAME

# Execution
backup_cli.py run NAME [--blocking] [--verbose]
backup_cli.py run-all [--verbose]

# Scheduler
backup_cli.py scheduler start [--daemon]
backup_cli.py scheduler stop
backup_cli.py scheduler status [--json]
backup_cli.py scheduler reload

# Monitoring
backup_cli.py status [NAME] [--json]
backup_cli.py logs [NAME] [--lines N] [--tail]
```

### HTTP API Quick Reference

```bash
# Jobs
GET    /api/jobs
GET    /api/jobs/{id}
POST   /api/jobs                    # Create
PUT    /api/jobs/{id}               # Update
DELETE /api/jobs/{id}
POST   /api/jobs/{id}/run           # Execute
POST   /api/jobs/{id}/enable
POST   /api/jobs/{id}/disable

# Scheduler
GET    /api/scheduler/status
POST   /api/scheduler/start
POST   /api/scheduler/stop
POST   /api/scheduler/reload

# System
GET    /api/status
GET    /api/logs?lines=100
GET    /api/logs/{job_id}?lines=50
GET    /api/health
```

---

## Support

For issues, questions, or contributions related to the Solace Backup Suite API:

1. Check this documentation first
2. Review logs in `Debug/` directory
3. Check the status file at `/tmp/solace_backup_status.json`
4. Run the module test: `python backup_api.py`

---

*Document Version: 1.0.0*  
*Last Updated: 2026-02-17*
