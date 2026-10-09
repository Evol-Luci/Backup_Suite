#!/usr/bin/env python3
"""
backup_cli.py - Command-line interface for Solace Backup Suite

A comprehensive CLI tool for managing backup jobs from shell scripts and terminal.
Designed for integration with external projects like Good Vibes Cosmetic.

Example Integration Script:
    #!/bin/bash
    # Good Vibes Cosmetic backup integration

    # Setup daily database backup
    python backup_cli.py create db_backup \
      --source /var/lib/goodvibes/database \
      --dest /mnt/backups/goodvibes \
      --schedule "daily@02:00" \
      --keep 30

    # Setup hourly config backup
    python backup_cli.py create config_backup \
      --source /etc/goodvibes \
      --dest /mnt/backups/goodvibes-config \
      --schedule "interval@60" \
      --keep 24

    # Start the scheduler daemon
    python backup_cli.py scheduler start --daemon

    # Or run immediate backup (e.g., before deployment)
    python backup_cli.py run db_backup --blocking --verbose

Exit Codes:
    0: Success
    1: General error
    2: Invalid arguments
    3: Job not found
    4: Backup execution failed
    5: Scheduler error
"""

import argparse
import json
import os
import sys
import subprocess
import platform
import signal
import time
import tempfile
import shutil
import glob
import zipfile
import io
import threading
import queue
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

# ==============================================================================
# Configuration
# ==============================================================================
IS_WINDOWS = platform.system() == "Windows"
CONFIG_DIR = "Settings"
CONFIG_FILE = "backup_config.json"
CONFIG_PATH = os.path.join(CONFIG_DIR, CONFIG_FILE)
LOG_DIR = "Debug"
LOG_FILE = os.path.join(LOG_DIR, "backup_cli.log")
PID_FILE = "/tmp/solace_backup_scheduler.pid"
STATUS_FILE = "/tmp/solace_backup_status.json"


# ==============================================================================
# Colors for terminal output
# ==============================================================================
class Colors:
    """Terminal colors - disabled if not a tty or --quiet is used"""

    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"

    @classmethod
    def disable(cls):
        cls.HEADER = ""
        cls.BLUE = ""
        cls.CYAN = ""
        cls.GREEN = ""
        cls.YELLOW = ""
        cls.RED = ""
        cls.BOLD = ""
        cls.UNDERLINE = ""
        cls.END = ""


# ==============================================================================
# Output helpers
# ==============================================================================
class Output:
    """Handles output formatting with support for JSON and quiet modes"""

    def __init__(self, json_mode: bool = False, quiet: bool = False):
        self.json_mode = json_mode
        self.quiet = quiet
        self.json_data = {}

        if quiet or not sys.stdout.isatty():
            Colors.disable()

    def print(self, message: str, color: str = "", end: str = "\n"):
        """Print a message unless in quiet mode"""
        if not self.quiet and not self.json_mode:
            if color:
                print(f"{color}{message}{Colors.END}", end=end)
            else:
                print(message, end=end)
        sys.stdout.flush()

    def error(self, message: str):
        """Print error to stderr"""
        if not self.quiet:
            if self.json_mode:
                self.json_data["error"] = message
            else:
                print(f"{Colors.RED}Error: {message}{Colors.END}", file=sys.stderr)
        sys.stderr.flush()

    def success(self, message: str):
        """Print success message"""
        self.print(message, Colors.GREEN)

    def warning(self, message: str):
        """Print warning message"""
        self.print(f"Warning: {message}", Colors.YELLOW)

    def info(self, message: str):
        """Print info message"""
        self.print(message, Colors.BLUE)

    def set_json_data(self, data: Dict[str, Any]):
        """Set JSON output data"""
        self.json_data = data

    def add_json(self, key: str, value: Any):
        """Add to JSON output data"""
        self.json_data[key] = value

    def output_json(self):
        """Output JSON data"""
        if self.json_mode:
            print(json.dumps(self.json_data, indent=2))

    def table(self, headers: List[str], rows: List[List[str]]):
        """Print a formatted table"""
        if self.quiet or self.json_mode:
            return

        # Calculate column widths
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))

        # Print header
        header_line = " | ".join(h.ljust(w) for h, w in zip(headers, widths))
        self.print(header_line, Colors.BOLD + Colors.CYAN)
        self.print("-" * len(header_line))

        # Print rows
        for row in rows:
            self.print(" | ".join(str(cell).ljust(w) for cell, w in zip(row, widths)))


# ==============================================================================
# Configuration Management
# ==============================================================================
def get_default_config() -> Dict[str, Any]:
    """Get default configuration"""
    return {
        "global_settings": {
            "default_volumes_to_keep": 3,
            "default_backup_base_name": "Backups_Py",
            "theme": "Light (Default)",
        },
        "backup_jobs": [],
    }


def load_config() -> Dict[str, Any]:
    """Load configuration from file"""
    if not os.path.exists(CONFIG_PATH):
        return get_default_config()

    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        # Backup corrupted config
        if os.path.exists(CONFIG_PATH):
            bak_path = f"{CONFIG_PATH}.bak_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            try:
                os.rename(CONFIG_PATH, bak_path)
            except OSError:
                pass
        return get_default_config()


def save_config(config_data: Dict[str, Any]) -> bool:
    """Save configuration to file"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(config_data, f, indent=4)
        return True
    except Exception:
        return False


def get_job(config: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """Get a job by name from config"""
    for job in config.get("backup_jobs", []):
        if job.get("name") == name:
            return job
    return None


def job_exists(config: Dict[str, Any], name: str) -> bool:
    """Check if a job exists"""
    return get_job(config, name) is not None


def update_job(config: Dict[str, Any], name: str, updates: Dict[str, Any]) -> bool:
    """Update a job with new values"""
    for i, job in enumerate(config.get("backup_jobs", [])):
        if job.get("name") == name:
            config["backup_jobs"][i].update(updates)
            return True
    return False


def delete_job(config: Dict[str, Any], name: str) -> bool:
    """Delete a job from config"""
    jobs = config.get("backup_jobs", [])
    for i, job in enumerate(jobs):
        if job.get("name") == name:
            del jobs[i]
            return True
    return False


# ==============================================================================
# Backup Execution
# ==============================================================================
def get_subprocess_flags():
    """Returns platform-specific creation flags for subprocesses"""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


class BackupRunner:
    """Handles backup execution with progress reporting"""

    def __init__(self, output: Output):
        self.output = output
        self.cancelled = False

    def log(self, message: str, job_name: str = ""):
        """Log a message"""
        prefix = f"[{job_name}] " if job_name else ""
        if not self.output.quiet:
            self.output.print(f"{prefix}{message}")

    def run_file_copy(self, job_details: Dict[str, Any], temp_dest_dir: str) -> int:
        """Handle file copying with platform-specific tools"""
        source_dir = job_details["source_dir"]
        job_name = job_details["name"]
        exclusions = job_details.get("exclusions", [])

        # Linux Native (Rsync)
        if not IS_WINDOWS:
            self.log("Starting Rsync (Linux)...", job_name)
            src_path = (
                source_dir if source_dir.endswith(os.sep) else source_dir + os.sep
            )
            command = ["rsync", "-av", "--delete", src_path, temp_dest_dir]
            for exclusion in exclusions:
                command.append(f"--exclude={exclusion}")

            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, **get_subprocess_flags()
                )
                if result.stdout and not self.output.quiet:
                    for line in result.stdout.strip().split("\n")[
                        -10:
                    ]:  # Show last 10 lines
                        self.log(f"  {line}", job_name)
                return result.returncode
            except FileNotFoundError:
                self.log(
                    "CRITICAL ERROR: rsync not found. Please install rsync.", job_name
                )
                return -1
            except Exception as e:
                self.log(f"CRITICAL ERROR during rsync: {e}", job_name)
                return -2

        # WSL Path Detection
        is_wsl_path = source_dir.lower().startswith("\\\\wsl")

        if is_wsl_path:
            self.log("WSL path detected. Using rsync via wsl.exe...", job_name)
            try:
                path_parts = source_dir.lower().split("\\\\")
                if len(path_parts) < 4:
                    raise ValueError("WSL path is not in the expected format.")
                distro_name = path_parts[3]
                wsl_prefix = f"\\\\{path_parts[2]}\\{distro_name}"

                source_linux = source_dir[len(wsl_prefix) :].replace("\\", "/")
                temp_dest_linux = temp_dest_dir[len(wsl_prefix) :].replace("\\", "/")
            except (ValueError, IndexError) as e:
                self.log(
                    f"CRITICAL ERROR: Could not parse WSL path. Error: {e}", job_name
                )
                return -2

            if not source_linux.endswith("/"):
                source_linux += "/"

            command = [
                "wsl",
                "-d",
                distro_name,
                "rsync",
                "-av",
                source_linux,
                temp_dest_linux,
            ]
            for exclusion in exclusions:
                command.append(f"--exclude={exclusion}")

            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, **get_subprocess_flags()
                )
                return result.returncode
            except FileNotFoundError:
                self.log("CRITICAL ERROR: wsl.exe not found.", job_name)
                return -1
            except Exception as e:
                self.log(f"CRITICAL ERROR during rsync: {e}", job_name)
                return -2

        else:  # Standard Windows Path
            self.log("Starting Robocopy...", job_name)
            command = [
                "robocopy",
                source_dir,
                temp_dest_dir,
                "/E",
                "/COPY:DAT",
                "/R:1",
                "/W:1",
                "/BYTES",
                "/NJH",
                "/NJS",
                "/NDL",
                "/NP",
            ]
            for exclusion in exclusions:
                command.append("/XD")
                command.append(exclusion)

            try:
                result = subprocess.run(
                    command, capture_output=True, **get_subprocess_flags()
                )
                return result.returncode
            except FileNotFoundError:
                self.log("CRITICAL ERROR: robocopy.exe not found.", job_name)
                return -1
            except Exception as e:
                self.log(f"CRITICAL ERROR during Robocopy: {e}", job_name)
                return -2

    def create_zip_archive(
        self, job_details: Dict[str, Any], source_dir: str, zip_file_path: str
    ) -> bool:
        """Create a zip archive from the copied files"""
        job_name = job_details["name"]
        self.log(f"Creating zip archive: {zip_file_path}", job_name)

        try:
            file_count = 0
            with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(source_dir):
                    for file in files:
                        if self.cancelled:
                            return False
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, source_dir)
                        try:
                            zipf.write(file_path, arcname)
                            file_count += 1
                            if file_count % 100 == 0:
                                self.log(f"  Zipped {file_count} files...", job_name)
                        except (FileNotFoundError, OSError) as e:
                            self.log(f"  Warning: Skipped file: {file} ({e})", job_name)

            self.log(f"Zip archive created successfully ({file_count} files)", job_name)
            return True
        except Exception as e:
            self.log(f"CRITICAL ERROR during Zipping: {e}", job_name)
            return False

    def cleanup_temp_dir(self, job_details: Dict[str, Any], temp_dir: str) -> bool:
        """Clean up temporary directory"""
        job_name = job_details["name"]

        if not os.path.exists(temp_dir):
            return True

        is_wsl_path = temp_dir.lower().startswith("\\\\wsl")

        if is_wsl_path:
            try:
                path_parts = temp_dir.lower().split("\\\\")
                if len(path_parts) < 4:
                    return False
                distro_name = path_parts[3]
                wsl_prefix = f"\\\\{path_parts[2]}\\{distro_name}"
                temp_dir_linux = temp_dir[len(wsl_prefix) :].replace("\\", "/")

                command = ["wsl", "-d", distro_name, "rm", "-rf", temp_dir_linux]
                subprocess.run(command, capture_output=True, **get_subprocess_flags())
                return True
            except Exception:
                return False
        else:
            try:
                shutil.rmtree(temp_dir)
                return True
            except Exception:
                return False

    def perform_cleanup(self, job_details: Dict[str, Any], volumes_to_keep: int):
        """Clean up old backups based on retention policy"""
        job_name = job_details["name"]
        backup_folder = job_details["destination_base"]

        search_pattern = os.path.join(backup_folder, f"{job_name}_*.zip")
        try:
            backup_files = sorted(glob.glob(search_pattern))
            backup_count = len(backup_files)

            if backup_count > volumes_to_keep:
                delete_count = backup_count - volumes_to_keep
                self.log(f"Cleaning up {delete_count} old backup(s)...", job_name)
                for file_path in backup_files[:delete_count]:
                    try:
                        os.remove(file_path)
                        self.log(f"  Deleted: {os.path.basename(file_path)}", job_name)
                    except OSError as e:
                        self.log(f"  Warning: Delete failed: {e}", job_name)
        except Exception as e:
            self.log(f"Error during cleanup: {e}", job_name)

    def run_backup(
        self, job_details: Dict[str, Any], global_settings: Dict[str, Any]
    ) -> bool:
        """Run a complete backup job"""
        job_name = job_details["name"]

        if not job_details.get("enabled", False):
            self.log("SKIPPED: Job is disabled.", job_name)
            return True

        # Determine retention
        job_volumes = job_details.get("volumes_to_keep_override")
        if job_volumes is not None and job_volumes > 0:
            volumes_to_keep = job_volumes
        else:
            volumes_to_keep = global_settings.get("default_volumes_to_keep", 3)

        backup_folder = job_details["destination_base"]
        os.makedirs(backup_folder, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        temp_copy_dir = os.path.join(backup_folder, f"Temp_{job_name}_{timestamp}")
        zip_file = os.path.join(backup_folder, f"{job_name}_{timestamp}.zip")

        self.log(f"Starting backup: {job_name}", job_name)
        self.log(f"  Source: {job_details['source_dir']}", job_name)
        self.log(f"  Destination: {zip_file}", job_name)

        # Step 1: Copy files
        self.log("Step 1/4: Copying files...", job_name)
        copy_exit_code = self.run_file_copy(job_details, temp_copy_dir)

        # Check success
        if not IS_WINDOWS:
            copy_ok = copy_exit_code == 0
        else:
            is_wsl = job_details["source_dir"].lower().startswith("\\\\wsl")
            if is_wsl:
                copy_ok = copy_exit_code == 0
            else:
                copy_ok = copy_exit_code < 8

        if not copy_ok:
            self.log(
                f"ERROR: File copy failed with exit code {copy_exit_code}", job_name
            )
            self.cleanup_temp_dir(job_details, temp_copy_dir)
            return False

        # Step 2: Create zip
        self.log("Step 2/4: Creating zip archive...", job_name)
        zip_ok = self.create_zip_archive(job_details, temp_copy_dir, zip_file)

        if not zip_ok:
            self.cleanup_temp_dir(job_details, temp_copy_dir)
            return False

        # Step 3: Cleanup temp
        self.log("Step 3/4: Cleaning up temporary files...", job_name)
        self.cleanup_temp_dir(job_details, temp_copy_dir)

        # Step 4: Rotate old backups
        self.log("Step 4/4: Rotating old backups...", job_name)
        self.perform_cleanup(job_details, volumes_to_keep)

        # Get final file size
        try:
            size_mb = os.path.getsize(zip_file) / (1024 * 1024)
            self.log(f"Backup completed successfully ({size_mb:.2f} MB)", job_name)
        except OSError:
            self.log("Backup completed successfully", job_name)

        return True


# ==============================================================================
# Scheduler Daemon
# ==============================================================================
class SchedulerDaemon:
    """Manages the background scheduler daemon"""

    def __init__(self, output: Output):
        self.output = output
        self.running = False
        self.scheduler = None

    def is_running(self) -> bool:
        """Check if scheduler daemon is running"""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r") as f:
                    pid = int(f.read().strip())
                # Check if process exists
                if IS_WINDOWS:
                    # Windows check
                    result = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}"],
                        capture_output=True,
                        text=True,
                    )
                    return str(pid) in result.stdout
                else:
                    # Unix check
                    os.kill(pid, 0)
                    return True
            except (ValueError, OSError, ProcessLookupError):
                # Stale PID file
                try:
                    os.remove(PID_FILE)
                except OSError:
                    pass
        return False

    def get_pid(self) -> Optional[int]:
        """Get the scheduler PID if running"""
        if os.path.exists(PID_FILE):
            try:
                with open(PID_FILE, "r") as f:
                    return int(f.read().strip())
            except ValueError:
                pass
        return None

    def start(self, daemonize: bool = False) -> bool:
        """Start the scheduler daemon"""
        if self.is_running():
            self.output.error("Scheduler is already running")
            return False

        if daemonize:
            # Double-fork daemon
            self._daemonize()

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        # Write PID file
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        # Initialize scheduler
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger

            self.scheduler = BackgroundScheduler()
            self._load_jobs()
            self.scheduler.start()
            self.running = True

            self.output.success("Scheduler started successfully")
            self._write_status("running", "Scheduler daemon running")

            # Keep running
            while self.running:
                time.sleep(1)

            return True

        except ImportError:
            self.output.error("APScheduler not installed. Run: pip install apscheduler")
            return False
        except Exception as e:
            self.output.error(f"Failed to start scheduler: {e}")
            return False

    def _daemonize(self):
        """Double-fork to daemonize process"""
        if IS_WINDOWS:
            return  # Windows doesn't support fork

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)  # Exit parent
        except OSError as e:
            self.output.error(f"Fork failed: {e}")
            sys.exit(1)

        os.chdir("/")
        os.setsid()
        os.umask(0)

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)  # Exit second parent
        except OSError as e:
            self.output.error(f"Second fork failed: {e}")
            sys.exit(1)

        # Redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        with open("/dev/null", "r") as f:
            os.dup2(f.fileno(), sys.stdin.fileno())
        with open("/dev/null", "a+") as f:
            os.dup2(f.fileno(), sys.stdout.fileno())
            os.dup2(f.fileno(), sys.stderr.fileno())

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        self.output.info("Shutting down scheduler...")
        self.running = False
        if self.scheduler:
            self.scheduler.shutdown()
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        self._write_status("stopped", "Scheduler daemon stopped")
        sys.exit(0)

    def _load_jobs(self):
        """Load jobs into scheduler"""
        try:
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger

            config = load_config()

            for job in config.get("backup_jobs", []):
                if not job.get("enabled", False):
                    continue

                schedule_str = job.get("schedule", "manual").lower().strip()
                if schedule_str == "manual":
                    continue

                trigger = None
                try:
                    if schedule_str.startswith("daily@"):
                        h, m = map(int, schedule_str.split("@")[1].split(":"))
                        trigger = CronTrigger(hour=h, minute=m)
                    elif schedule_str.startswith("interval@"):
                        minutes = int(schedule_str.split("@")[1])
                        trigger = IntervalTrigger(minutes=minutes)
                    elif schedule_str.startswith("weekly@"):
                        parts = schedule_str.split("@")[1].split(":")
                        day = parts[0][:3].lower()
                        hour = int(parts[1])
                        minute = int(parts[2])
                        trigger = CronTrigger(day_of_week=day, hour=hour, minute=minute)
                except Exception:
                    continue

                if trigger:
                    self.scheduler.add_job(
                        self._run_scheduled_backup,
                        trigger,
                        id=job["name"],
                        args=[job, config["global_settings"]],
                        replace_existing=True,
                    )
        except Exception as e:
            self.output.error(f"Error loading jobs: {e}")

    def _run_scheduled_backup(
        self, job_details: Dict[str, Any], global_settings: Dict[str, Any]
    ):
        """Run a scheduled backup job"""
        output = Output(quiet=True)  # Silent output for scheduled jobs
        runner = BackupRunner(output)
        success = runner.run_backup(job_details, global_settings)

        status = "success" if success else "failed"
        self._write_status(status, f"Scheduled backup: {job_details['name']} {status}")

    def _write_status(self, status: str, message: str):
        """Write status to status file"""
        try:
            with open(STATUS_FILE, "w") as f:
                json.dump(
                    {
                        "status": status,
                        "message": message,
                        "timestamp": datetime.now().isoformat(),
                    },
                    f,
                )
        except Exception:
            pass

    def stop(self) -> bool:
        """Stop the scheduler daemon"""
        pid = self.get_pid()
        if pid is None:
            self.output.error("Scheduler is not running")
            return False

        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"], capture_output=True
                )
            else:
                os.kill(pid, signal.SIGTERM)

            # Wait for process to stop
            for _ in range(10):
                if not self.is_running():
                    break
                time.sleep(0.5)

            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)

            self.output.success("Scheduler stopped")
            return True

        except Exception as e:
            self.output.error(f"Failed to stop scheduler: {e}")
            return False

    def status(self) -> Dict[str, Any]:
        """Get scheduler status"""
        is_running = self.is_running()
        pid = self.get_pid() if is_running else None

        # Load job info
        config = load_config()
        jobs = []
        for job in config.get("backup_jobs", []):
            schedule = job.get("schedule", "manual")
            if schedule != "manual":
                jobs.append(
                    {
                        "name": job["name"],
                        "schedule": schedule,
                        "enabled": job.get("enabled", False),
                    }
                )

        return {"running": is_running, "pid": pid, "scheduled_jobs": jobs}

    def reload(self) -> bool:
        """Reload scheduler configuration"""
        if not self.is_running():
            self.output.error("Scheduler is not running")
            return False

        # Send SIGHUP to reload (or just restart)
        self.stop()
        time.sleep(1)
        return self.start(daemonize=True)


# ==============================================================================
# Command Handlers
# ==============================================================================
def cmd_create(args, output: Output) -> int:
    """Create a new backup job"""
    config = load_config()

    # Validate required fields
    if not args.source or not args.dest:
        output.error("--source and --dest are required")
        return 2

    if job_exists(config, args.name):
        output.error(f"Job '{args.name}' already exists")
        return 3

    # Validate source exists
    if not os.path.exists(args.source):
        output.error(f"Source directory does not exist: {args.source}")
        return 2

    # Create job
    job = {
        "name": args.name,
        "source_dir": os.path.abspath(args.source),
        "destination_base": os.path.abspath(args.dest),
        "enabled": args.enabled,
        "schedule": args.schedule or "manual",
        "exclusions": args.exclude or [],
    }

    if args.keep is not None:
        job["volumes_to_keep_override"] = args.keep

    config["backup_jobs"].append(job)

    if save_config(config):
        output.success(f"Created backup job: {args.name}")
        output.info(f"  Source: {job['source_dir']}")
        output.info(f"  Destination: {job['destination_base']}")
        output.info(f"  Schedule: {job['schedule']}")
        output.info(f"  Enabled: {job['enabled']}")
        return 0
    else:
        output.error("Failed to save configuration")
        return 1


def cmd_list(args, output: Output) -> int:
    """List all backup jobs"""
    config = load_config()
    jobs = config.get("backup_jobs", [])

    if args.json:
        output.set_json_data({"jobs": jobs})
        output.output_json()
        return 0

    if not jobs:
        output.info("No backup jobs configured")
        return 0

    headers = ["Name", "Source", "Schedule", "Status"]
    rows = []

    for job in jobs:
        status = "enabled" if job.get("enabled", False) else "disabled"
        schedule = job.get("schedule", "manual")
        source = job.get("source_dir", "")
        # Truncate long paths
        if len(source) > 40:
            source = "..." + source[-37:]
        rows.append([job["name"], source, schedule, status])

    output.table(headers, rows)
    output.print(f"\nTotal: {len(jobs)} job(s)")
    return 0


def cmd_show(args, output: Output) -> int:
    """Show details of a specific job"""
    config = load_config()
    job = get_job(config, args.name)

    if not job:
        output.error(f"Job not found: {args.name}")
        return 3

    if args.json:
        output.set_json_data(job)
        output.output_json()
        return 0

    output.print(f"{Colors.BOLD}{Colors.CYAN}Job: {job['name']}{Colors.END}")
    output.print(f"{'=' * 50}")
    output.print(f"  Source: {Colors.YELLOW}{job.get('source_dir', 'N/A')}{Colors.END}")
    output.print(
        f"  Destination: {Colors.YELLOW}{job.get('destination_base', 'N/A')}{Colors.END}"
    )
    output.print(f"  Schedule: {job.get('schedule', 'manual')}")
    output.print(f"  Enabled: {'Yes' if job.get('enabled', False) else 'No'}")

    retention = job.get("volumes_to_keep_override")
    if retention:
        output.print(f"  Retention: {retention} backups (job-specific)")
    else:
        global_retention = config.get("global_settings", {}).get(
            "default_volumes_to_keep", 3
        )
        output.print(f"  Retention: {global_retention} backups (global default)")

    exclusions = job.get("exclusions", [])
    if exclusions:
        output.print(f"  Exclusions:")
        for exc in exclusions:
            output.print(f"    - {exc}")

    # Show existing backups
    backup_folder = job.get("destination_base", "")
    if backup_folder and os.path.exists(backup_folder):
        pattern = os.path.join(backup_folder, f"{job['name']}_*.zip")
        backups = sorted(glob.glob(pattern))
        if backups:
            output.print(
                f"\n  {Colors.CYAN}Existing Backups ({len(backups)}):{Colors.END}"
            )
            for backup in backups[-5:]:  # Show last 5
                size = os.path.getsize(backup) / (1024 * 1024)
                output.print(f"    - {os.path.basename(backup)} ({size:.2f} MB)")
            if len(backups) > 5:
                output.print(f"    ... and {len(backups) - 5} more")

    return 0


def cmd_update(args, output: Output) -> int:
    """Update a backup job"""
    config = load_config()

    if not job_exists(config, args.name):
        output.error(f"Job not found: {args.name}")
        return 3

    updates = {}

    # Parse field updates from args
    for update in args.field or []:
        if "=" not in update:
            output.error(f"Invalid update format: {update}. Use 'field=value'")
            return 2
        field, value = update.split("=", 1)

        if field == "source":
            updates["source_dir"] = os.path.abspath(value)
        elif field == "dest":
            updates["destination_base"] = os.path.abspath(value)
        elif field == "schedule":
            updates["schedule"] = value
        elif field == "keep":
            updates["volumes_to_keep_override"] = int(value)
        elif field == "exclude":
            if "exclusions" not in updates:
                updates["exclusions"] = []
            updates["exclusions"].append(value)
        elif field == "enabled":
            updates["enabled"] = value.lower() in ("true", "1", "yes", "on")
        else:
            output.warning(f"Unknown field: {field}")

    if update_job(config, args.name, updates):
        if save_config(config):
            output.success(f"Updated job: {args.name}")
            return 0

    output.error("Failed to update job")
    return 1


def cmd_delete(args, output: Output) -> int:
    """Delete a backup job"""
    config = load_config()

    if not job_exists(config, args.name):
        output.error(f"Job not found: {args.name}")
        return 3

    if not args.force:
        # In interactive mode, ask for confirmation
        if sys.stdin.isatty():
            output.print(
                f"Are you sure you want to delete job '{args.name}'? [y/N] ", end=""
            )
            response = input().strip().lower()
            if response not in ("y", "yes"):
                output.info("Cancelled")
                return 0

    if delete_job(config, args.name):
        if save_config(config):
            output.success(f"Deleted job: {args.name}")
            return 0

    output.error("Failed to delete job")
    return 1


def cmd_enable(args, output: Output) -> int:
    """Enable a backup job"""
    config = load_config()

    if not job_exists(config, args.name):
        output.error(f"Job not found: {args.name}")
        return 3

    if update_job(config, args.name, {"enabled": True}):
        if save_config(config):
            output.success(f"Enabled job: {args.name}")
            return 0

    output.error("Failed to enable job")
    return 1


def cmd_disable(args, output: Output) -> int:
    """Disable a backup job"""
    config = load_config()

    if not job_exists(config, args.name):
        output.error(f"Job not found: {args.name}")
        return 3

    if update_job(config, args.name, {"enabled": False}):
        if save_config(config):
            output.success(f"Disabled job: {args.name}")
            return 0

    output.error("Failed to disable job")
    return 1


def cmd_run(args, output: Output) -> int:
    """Run a backup job"""
    config = load_config()
    job = get_job(config, args.name)

    if not job:
        output.error(f"Job not found: {args.name}")
        return 3

    runner = BackupRunner(output)

    if args.blocking:
        output.info(f"Running backup job: {args.name}")
        success = runner.run_backup(job, config.get("global_settings", {}))
        if success:
            output.success(f"Backup completed: {args.name}")
            return 0
        else:
            output.error(f"Backup failed: {args.name}")
            return 4
    else:
        # Run in background thread
        def run_async():
            runner.run_backup(job, config.get("global_settings", {}))

        thread = threading.Thread(target=run_async, daemon=True)
        thread.start()
        output.success(f"Backup queued: {args.name}")
        return 0


def cmd_run_all(args, output: Output) -> int:
    """Run all enabled backup jobs"""
    config = load_config()
    jobs = [j for j in config.get("backup_jobs", []) if j.get("enabled", False)]

    if not jobs:
        output.info("No enabled jobs to run")
        return 0

    output.info(f"Running {len(jobs)} enabled backup job(s)...")

    failed = []
    for job in jobs:
        runner = BackupRunner(output)
        success = runner.run_backup(job, config.get("global_settings", {}))
        if not success:
            failed.append(job["name"])

    if failed:
        output.error(f"Failed backups: {', '.join(failed)}")
        return 4

    output.success("All backups completed successfully")
    return 0


def cmd_scheduler_start(args, output: Output) -> int:
    """Start the scheduler daemon"""
    scheduler = SchedulerDaemon(output)

    if scheduler.is_running():
        output.warning("Scheduler is already running")
        return 0

    if args.daemon:
        output.info("Starting scheduler daemon...")
        if scheduler.start(daemonize=True):
            return 0
        return 5
    else:
        output.info("Starting scheduler (foreground mode)...")
        output.info("Press Ctrl+C to stop")
        try:
            scheduler.start(daemonize=False)
        except KeyboardInterrupt:
            output.info("\nStopping scheduler...")
        return 0


def cmd_scheduler_stop(args, output: Output) -> int:
    """Stop the scheduler daemon"""
    scheduler = SchedulerDaemon(output)

    if scheduler.stop():
        return 0
    return 5


def cmd_scheduler_status(args, output: Output) -> int:
    """Show scheduler status"""
    scheduler = SchedulerDaemon(output)
    status = scheduler.status()

    if args.json:
        output.set_json_data(status)
        output.output_json()
        return 0

    if status["running"]:
        output.print(f"{Colors.GREEN}● Scheduler is running{Colors.END}")
        output.print(f"  PID: {status['pid']}")
    else:
        output.print(f"{Colors.RED}○ Scheduler is not running{Colors.END}")

    if status["scheduled_jobs"]:
        output.print(f"\n{Colors.CYAN}Scheduled Jobs:{Colors.END}")
        for job in status["scheduled_jobs"]:
            status_icon = "✓" if job["enabled"] else "✗"
            output.print(f"  [{status_icon}] {job['name']} ({job['schedule']})")

    return 0


def cmd_scheduler_reload(args, output: Output) -> int:
    """Reload scheduler configuration"""
    scheduler = SchedulerDaemon(output)

    if scheduler.reload():
        output.success("Scheduler reloaded")
        return 0
    return 5


def cmd_logs(args, output: Output) -> int:
    """View backup logs"""
    lines = args.lines or 50

    # Check for job-specific logs
    if args.name:
        config = load_config()
        job = get_job(config, args.name)
        if not job:
            output.error(f"Job not found: {args.name}")
            return 3
        # Job-specific logs would be in a separate file
        # For now, show global log filtered by job name
        log_file = LOG_FILE
    else:
        log_file = LOG_FILE

    if not os.path.exists(log_file):
        output.info("No log file found")
        return 0

    if args.tail:
        # Follow log
        try:
            with open(log_file, "r") as f:
                # Seek to end
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(0.1)
        except KeyboardInterrupt:
            return 0
    else:
        # Show last N lines
        try:
            with open(log_file, "r") as f:
                all_lines = f.readlines()
                for line in all_lines[-lines:]:
                    print(line, end="")
        except Exception as e:
            output.error(f"Failed to read log: {e}")
            return 1

    return 0


def cmd_status(args, output: Output) -> int:
    """Get backup status"""
    config = load_config()

    if args.name:
        job = get_job(config, args.name)
        if not job:
            output.error(f"Job not found: {args.name}")
            return 3

        backup_folder = job.get("destination_base", "")
        pattern = os.path.join(backup_folder, f"{job['name']}_*.zip")
        backups = sorted(glob.glob(pattern))

        status_data = {
            "name": job["name"],
            "enabled": job.get("enabled", False),
            "source": job.get("source_dir"),
            "destination": job.get("destination_base"),
            "backup_count": len(backups),
            "latest_backup": None,
        }

        if backups:
            latest = backups[-1]
            status_data["latest_backup"] = {
                "file": os.path.basename(latest),
                "path": latest,
                "size_mb": round(os.path.getsize(latest) / (1024 * 1024), 2),
                "timestamp": datetime.fromtimestamp(
                    os.path.getmtime(latest)
                ).isoformat(),
            }

        if args.json:
            output.set_json_data(status_data)
            output.output_json()
        else:
            output.print(f"{Colors.BOLD}{Colors.CYAN}Status: {job['name']}{Colors.END}")
            output.print(
                f"  Status: {'Enabled' if status_data['enabled'] else 'Disabled'}"
            )
            output.print(f"  Total Backups: {status_data['backup_count']}")
            if status_data["latest_backup"]:
                latest = status_data["latest_backup"]
                output.print(f"  Latest: {latest['file']}")
                output.print(f"    Size: {latest['size_mb']:.2f} MB")
                output.print(f"    Date: {latest['timestamp']}")

        return 0
    else:
        # Show all jobs status
        all_status = []
        for job in config.get("backup_jobs", []):
            backup_folder = job.get("destination_base", "")
            pattern = os.path.join(backup_folder, f"{job['name']}_*.zip")
            backups = sorted(glob.glob(pattern))

            job_status = {
                "name": job["name"],
                "enabled": job.get("enabled", False),
                "backup_count": len(backups),
            }

            if backups:
                latest = backups[-1]
                job_status["latest_backup"] = datetime.fromtimestamp(
                    os.path.getmtime(latest)
                ).strftime("%Y-%m-%d %H:%M")

            all_status.append(job_status)

        if args.json:
            output.set_json_data({"jobs": all_status})
            output.output_json()
        else:
            headers = ["Name", "Status", "Backups", "Latest"]
            rows = []
            for s in all_status:
                status = "enabled" if s["enabled"] else "disabled"
                latest = s.get("latest_backup", "N/A")
                rows.append([s["name"], status, s["backup_count"], latest])

            output.table(headers, rows)

        return 0


# ==============================================================================
# Main CLI
# ==============================================================================
def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        prog="backup_cli.py",
        description="Solace Backup Suite CLI - Manage backup jobs from the terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a new backup job
  python backup_cli.py create daily_backup --source /home/user/docs --dest /backup/docs

  # List all jobs
  python backup_cli.py list

  # Run a backup immediately
  python backup_cli.py run daily_backup --blocking --verbose

  # Start scheduler daemon
  python backup_cli.py scheduler start --daemon

For more information, see the integration examples in the script header.
        """,
    )

    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress non-error output"
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # create
    create_parser = subparsers.add_parser("create", help="Create a new backup job")
    create_parser.add_argument("name", help="Job name")
    create_parser.add_argument("--source", "-s", required=True, help="Source directory")
    create_parser.add_argument(
        "--dest", "-d", required=True, help="Destination directory"
    )
    create_parser.add_argument(
        "--schedule", help="Schedule expression (e.g., daily@02:00, interval@60)"
    )
    create_parser.add_argument("--keep", type=int, help="Number of backups to retain")
    create_parser.add_argument(
        "--exclude",
        "-e",
        action="append",
        help="Exclusion pattern (can be used multiple times)",
    )
    create_parser.add_argument(
        "--enabled",
        dest="enabled",
        action="store_true",
        default=True,
        help="Enable the job (default)",
    )
    create_parser.add_argument(
        "--disabled", dest="enabled", action="store_false", help="Disable the job"
    )

    # list
    list_parser = subparsers.add_parser("list", help="List all backup jobs")

    # show
    show_parser = subparsers.add_parser("show", help="Show job details")
    show_parser.add_argument("name", help="Job name")

    # update
    update_parser = subparsers.add_parser("update", help="Update job fields")
    update_parser.add_argument("name", help="Job name")
    update_parser.add_argument(
        "--field", action="append", help="Field update in format field=value"
    )

    # delete
    delete_parser = subparsers.add_parser("delete", help="Delete a backup job")
    delete_parser.add_argument("name", help="Job name")
    delete_parser.add_argument(
        "--force", action="store_true", help="Force deletion without confirmation"
    )

    # enable
    enable_parser = subparsers.add_parser("enable", help="Enable a backup job")
    enable_parser.add_argument("name", help="Job name")

    # disable
    disable_parser = subparsers.add_parser("disable", help="Disable a backup job")
    disable_parser.add_argument("name", help="Job name")

    # run
    run_parser = subparsers.add_parser("run", help="Execute backup immediately")
    run_parser.add_argument("name", help="Job name")
    run_parser.add_argument(
        "--blocking", action="store_true", help="Wait for backup to complete"
    )
    run_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )

    # run-all
    run_all_parser = subparsers.add_parser("run-all", help="Run all enabled jobs")
    run_all_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose output"
    )

    # scheduler
    scheduler_parser = subparsers.add_parser(
        "scheduler", help="Manage scheduler daemon"
    )
    scheduler_subparsers = scheduler_parser.add_subparsers(dest="scheduler_command")

    scheduler_start = scheduler_subparsers.add_parser("start", help="Start scheduler")
    scheduler_start.add_argument(
        "--daemon", "-d", action="store_true", help="Run as daemon"
    )

    scheduler_subparsers.add_parser("stop", help="Stop scheduler")
    scheduler_subparsers.add_parser("status", help="Show scheduler status")
    scheduler_subparsers.add_parser("reload", help="Reload scheduler configuration")

    # logs
    logs_parser = subparsers.add_parser("logs", help="View backup logs")
    logs_parser.add_argument("name", nargs="?", help="Job name (optional)")
    logs_parser.add_argument("--lines", "-n", type=int, help="Number of lines to show")
    logs_parser.add_argument(
        "--tail", "-f", action="store_true", help="Follow log output"
    )

    # status
    status_parser = subparsers.add_parser("status", help="Get backup status")
    status_parser.add_argument("name", nargs="?", help="Job name (optional)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 2

    # Initialize output handler
    output = Output(json_mode=args.json, quiet=args.quiet)

    # Dispatch command
    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "show": cmd_show,
        "update": cmd_update,
        "delete": cmd_delete,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "run": cmd_run,
        "run-all": cmd_run_all,
        "logs": cmd_logs,
        "status": cmd_status,
    }

    if args.command == "scheduler":
        if not args.scheduler_command:
            scheduler_parser.print_help()
            return 2
        scheduler_commands = {
            "start": cmd_scheduler_start,
            "stop": cmd_scheduler_stop,
            "status": cmd_scheduler_status,
            "reload": cmd_scheduler_reload,
        }
        return scheduler_commands[args.scheduler_command](args, output)

    return commands[args.command](args, output)


if __name__ == "__main__":
    sys.exit(main())
