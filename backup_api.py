#!/usr/bin/env python3
"""
Backup API - Headless Backup Manager for Solace Backup Suite

Provides a programmatic interface for managing backup jobs without requiring
the tkinter GUI. Can be imported and used by external projects.

Example Usage:
    from backup_api import BackupManager

    # Initialize
    bm = BackupManager()

    # Create a daily backup job
    job = bm.create_job(
        name="good_vibes_db_backup",
        source_dir="/var/lib/goodvibes/database",
        destination_base="/backups/goodvibes",
        schedule="daily@02:00",
        volumes_to_keep=7
    )

    # Start scheduler for automated backups
    bm.start_scheduler()

    # Or run manually
    result = bm.run_job("good_vibes_db_backup", blocking=True)
    print(result)  # {'success': True, 'job_id': '...', 'duration': 45.2}
"""

import json
import os
import logging
import shutil
import glob
import subprocess
import threading
import queue
import time
import io
import zipfile
import platform
from datetime import datetime
from typing import Optional, Dict, List, Any, Callable
from dataclasses import dataclass, field, asdict
from collections import deque

# Platform detection
IS_WINDOWS = platform.system() == "Windows"

# APScheduler imports (optional dependency)
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    APS_AVAILABLE = True
except ImportError:
    APS_AVAILABLE = False


# ==============================================================================
# LOGGING SETUP
# ==============================================================================
LOG_DIR = "Debug"
LOG_FILE = os.path.join(LOG_DIR, "backup_api_debug.log")

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Configure file logging
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s - %(threadName)s - %(levelname)s - %(message)s",
    filemode="a",
)

# Console handler for visibility
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
logger = logging.getLogger("backup_api")
logger.addHandler(console_handler)


# ==============================================================================
# DATA CLASSES
# ==============================================================================
@dataclass
class BackupJob:
    """Represents a backup job configuration."""

    name: str
    source_dir: str
    destination_base: str
    schedule: str = (
        "manual"  # "manual", "daily@HH:MM", "interval@MINUTES", "weekly@DAY:HH:MM"
    )
    enabled: bool = True
    exclusions: List[str] = field(default_factory=list)
    volumes_to_keep_override: Optional[int] = None
    last_run: Optional[str] = None
    last_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BackupJob":
        return cls(**data)


@dataclass
class GlobalSettings:
    """Global backup configuration settings."""

    default_volumes_to_keep: int = 3
    default_backup_base_name: str = "Backups_Py"
    theme: str = "Light (Default)"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GlobalSettings":
        return cls(**data)


# ==============================================================================
# CONFIGURATION MANAGEMENT
# ==============================================================================
def get_default_config() -> Dict[str, Any]:
    """Returns the default configuration structure."""
    return {"global_settings": GlobalSettings().to_dict(), "backup_jobs": []}


def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from JSON file with fallback to defaults."""
    logger.info(f"Loading configuration from: {config_path}")

    if not os.path.exists(config_path):
        logger.warning("Configuration file not found. Creating default.")
        config_data = get_default_config()
        save_config(config_path, config_data)
        return config_data

    try:
        with open(config_path, "r") as f:
            config_data = json.load(f)

        # Ensure required keys exist
        if "global_settings" not in config_data:
            config_data["global_settings"] = GlobalSettings().to_dict()
        if "backup_jobs" not in config_data:
            config_data["backup_jobs"] = []

        logger.info("Configuration loaded successfully.")
        return config_data

    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading config: {e}. Creating backup and default.")

        # Backup corrupted config
        if os.path.exists(config_path):
            try:
                bak_path = (
                    f"{config_path}.bak_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                )
                os.rename(config_path, bak_path)
                logger.info(f"Backed up corrupted config to {bak_path}")
            except OSError as e_rename:
                logger.error(f"Could not backup corrupted config: {e_rename}")

        config_data = get_default_config()
        save_config(config_path, config_data)
        return config_data


def save_config(config_path: str, config_data: Dict[str, Any]) -> bool:
    """Save configuration to JSON file atomically."""
    logger.info(f"Saving configuration to: {config_path}")

    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Write to temp file first for atomic operation
        temp_path = f"{config_path}.tmp"
        with open(temp_path, "w") as f:
            json.dump(config_data, f, indent=4)

        # Atomic rename
        os.replace(temp_path, config_path)
        logger.info("Configuration saved successfully.")
        return True

    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


# ==============================================================================
# LOG QUEUE IMPLEMENTATION (Thread-safe circular buffer)
# ==============================================================================
class LogBuffer:
    """Thread-safe circular buffer for log messages with callback support."""

    def __init__(self, max_lines: int = 1000):
        self._buffer: deque = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self._callbacks: List[Callable[[str], None]] = []
        self._status_callbacks: List[Callable[[str, int, int, str], None]] = []

    def put(self, message: Any) -> None:
        """Add a message to the log buffer."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Handle status tuples: ("status", job_name, step, total, message)
        if isinstance(message, tuple) and len(message) >= 2:
            if message[0] == "status":
                _, job_name, step, total, msg = message
                log_line = (
                    f"[{timestamp}] [STATUS] {job_name}: Step {step}/{total} - {msg}"
                )
                with self._lock:
                    self._buffer.append(log_line)

                # Notify status callbacks
                for callback in self._status_callbacks:
                    try:
                        callback(job_name, step, total, msg)
                    except Exception as e:
                        logger.error(f"Status callback error: {e}")
                return

            elif message[0] == "file_update":
                _, job_name, file_info = message
                log_line = f"[{timestamp}] [{job_name}] {file_info}"
                with self._lock:
                    self._buffer.append(log_line)

                for callback in self._callbacks:
                    try:
                        callback(log_line)
                    except Exception as e:
                        logger.error(f"Log callback error: {e}")
                return

        # Regular string message
        log_line = f"[{timestamp}] {message}"
        with self._lock:
            self._buffer.append(log_line)

        # Also log to Python logger
        logger.info(message)

        # Notify callbacks
        for callback in self._callbacks:
            try:
                callback(log_line)
            except Exception as e:
                logger.error(f"Log callback error: {e}")

    def get_lines(self, lines: int = 100) -> List[str]:
        """Get recent log lines."""
        with self._lock:
            return list(self._buffer)[-lines:]

    def add_callback(self, callback: Callable[[str], None]) -> None:
        """Add a callback to receive log messages."""
        self._callbacks.append(callback)

    def add_status_callback(
        self, callback: Callable[[str, int, int, str], None]
    ) -> None:
        """Add a callback to receive status updates."""
        self._status_callbacks.append(callback)

    def clear_callbacks(self) -> None:
        """Clear all callbacks."""
        self._callbacks.clear()
        self._status_callbacks.clear()


# ==============================================================================
# CORE BACKUP FUNCTIONS (Extraction from backup_suite.pyw)
# ==============================================================================
def get_subprocess_flags() -> Dict[str, Any]:
    """Returns platform-specific creation flags for subprocesses."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def read_subprocess_output(
    process: subprocess.Popen, log_queue: LogBuffer, job_name: str
) -> None:
    """Read subprocess output and log it."""
    try:
        with io.TextIOWrapper(
            process.stdout, encoding="utf-8", errors="replace"
        ) as stdout_reader:
            for line in iter(stdout_reader.readline, ""):
                line = line.strip()
                if line:
                    # Robocopy specific parsing
                    if IS_WINDOWS and line.startswith("\t"):
                        parts = line.split("\t")
                        file_info = next((part for part in parts if part.strip()), None)
                        if file_info:
                            log_queue.put(("file_update", job_name, file_info.strip()))
                    # Generic output for rsync/Linux
                    elif not IS_WINDOWS:
                        log_queue.put(("file_update", job_name, line))
    except Exception as e:
        log_queue.put(f"[{job_name}] ERROR reading process output: {e}")
    finally:
        log_queue.put(f"[{job_name}] Process output reader finished.")


def run_file_copy(
    job_details: Dict[str, Any], temp_dest_dir: str, log_queue: LogBuffer
) -> int:
    """
    Handles file copying, choosing between Robocopy (Windows) and rsync (Linux/WSL).
    Returns exit code from the copy operation.
    """
    source_dir = job_details["source_dir"]
    job_name = job_details["name"]
    exclusions = job_details.get("exclusions", [])

    # --- Linux Native (Rsync) ---
    if not IS_WINDOWS:
        log_queue.put(f"[{job_name}] Starting Rsync (Linux)...")

        src_path = source_dir if source_dir.endswith(os.sep) else source_dir + os.sep

        command = ["rsync", "-av", "--delete", src_path, temp_dest_dir]
        for exclusion in exclusions:
            command.append(f"--exclude={exclusion}")

        log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **get_subprocess_flags(),
            )

            reader_thread = threading.Thread(
                target=read_subprocess_output,
                args=(process, log_queue, job_name),
                daemon=True,
                name=f"RsyncRead-{job_name}",
            )
            reader_thread.start()
            process.wait()
            reader_thread.join(timeout=5)

            return_code = process.returncode
            log_queue.put(
                f"[{job_name}]   Rsync finished with Exit Code: {return_code}"
            )
            return return_code

        except FileNotFoundError:
            log_queue.put(
                f"[{job_name}] CRITICAL ERROR: rsync not found. Please install rsync."
            )
            return -1
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during rsync: {e}")
            return -2

    # --- WSL Path Detection ---
    is_wsl_path = source_dir.lower().startswith("\\wsl")

    if is_wsl_path:
        log_queue.put(f"[{job_name}] WSL path detected. Using rsync via wsl.exe...")

        try:
            path_parts = source_dir.lower().split("\\")
            if len(path_parts) < 4:
                raise ValueError("WSL path is not in the expected format.")
            distro_name = path_parts[3]
            wsl_prefix = f"\\\\{path_parts[2]}\\{distro_name}"

            source_linux = source_dir[len(wsl_prefix) :].replace("\\", "/")
            temp_dest_linux = temp_dest_dir[len(wsl_prefix) :].replace("\\", "/")
        except (ValueError, IndexError) as e:
            log_queue.put(
                f"[{job_name}] CRITICAL ERROR: Could not parse WSL path. Error: {e}"
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

        log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                **get_subprocess_flags(),
            )
            return_code = process.returncode
            log_queue.put(
                f"[{job_name}]   rsync finished with Exit Code: {return_code}"
            )
            if return_code != 0:
                log_queue.put(f"[{job_name}]   rsync stderr: {process.stderr.strip()}")
            return return_code

        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: wsl.exe not found.")
            return -1
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during rsync: {e}")
            return -2

    # --- Standard Windows Path (Robocopy) ---
    else:
        log_queue.put(f"[{job_name}] Starting Robocopy...")
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

        log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **get_subprocess_flags(),
            )

            reader_thread = threading.Thread(
                target=read_subprocess_output,
                args=(process, log_queue, job_name),
                daemon=True,
                name=f"RoboRead-{job_name}",
            )
            reader_thread.start()
            process.wait()
            reader_thread.join(timeout=5)

            return_code = process.returncode
            log_queue.put(
                f"[{job_name}]   Robocopy finished with Exit Code: {return_code}"
            )
            return return_code

        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: robocopy.exe not found.")
            return -1
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during Robocopy: {e}")
            return -2


def create_zip_archive(
    job_details: Dict[str, Any],
    source_dir: str,
    zip_file_path: str,
    log_queue: LogBuffer,
) -> bool:
    """Create a ZIP archive from the source directory."""
    job_name = job_details["name"]
    log_queue.put(f"[{job_name}] Starting Zipping Process...")
    log_queue.put(f"[{job_name}]   Source: {source_dir}")
    log_queue.put(f"[{job_name}]   Zip File: {zip_file_path}")

    try:
        with zipfile.ZipFile(zip_file_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, source_dir)
                    try:
                        zipf.write(file_path, arcname)
                    except (FileNotFoundError, OSError) as e:
                        log_queue.put(
                            f"[{job_name}] WARNING: Skipped missing file: {file} ({e})"
                        )

        if os.path.exists(zip_file_path) and os.path.getsize(zip_file_path) > 0:
            log_queue.put(f"[{job_name}] SUCCESS: Zip file created.")
            return True
        else:
            log_queue.put(f"[{job_name}] WARNING: Zip file created but is empty!")
            return False

    except Exception as e:
        log_queue.put(f"[{job_name}] CRITICAL ERROR during Zipping: {e}")
        return False


def cleanup_temp_dir(
    job_details: Dict[str, Any], temp_dir: str, log_queue: LogBuffer
) -> bool:
    """Clean up temporary directory after backup."""
    job_name = job_details["name"]
    log_queue.put(f"[{job_name}] Cleaning up temporary folder: {temp_dir}")

    if not os.path.exists(temp_dir):
        log_queue.put(f"[{job_name}]   Temp folder not found, skipping.")
        return True

    is_wsl_path = temp_dir.lower().startswith("\\wsl")

    if is_wsl_path:
        log_queue.put(f"[{job_name}]   WSL path detected. Using 'rm -rf' via wsl.exe.")

        try:
            path_parts = temp_dir.lower().split("\\")
            if len(path_parts) < 4:
                raise ValueError("WSL path is not in the expected format.")
            distro_name = path_parts[3]
            wsl_prefix = f"\\\\{path_parts[2]}\\{distro_name}"

            temp_dir_linux = temp_dir[len(wsl_prefix) :].replace("\\", "/")
        except (ValueError, IndexError) as e:
            log_queue.put(
                f"[{job_name}] CRITICAL ERROR: Could not parse WSL path. Error: {e}"
            )
            return False

        command = ["wsl", "-d", distro_name, "rm", "-rf", temp_dir_linux]

        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                **get_subprocess_flags(),
            )
            if process.returncode == 0:
                log_queue.put(f"[{job_name}]   Temp folder deleted via WSL.")
                return True
            else:
                log_queue.put(
                    f"[{job_name}] ERROR: WSL 'rm -rf' failed with code {process.returncode}"
                )
                log_queue.put(f"[{job_name}]   stderr: {process.stderr.strip()}")
                return False
        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: wsl.exe not found.")
            return False
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during WSL cleanup: {e}")
            return False

    else:
        log_queue.put(f"[{job_name}]   Using standard Python cleanup.")
        try:
            shutil.rmtree(temp_dir)
            log_queue.put(f"[{job_name}]   Temp folder deleted.")
            return True
        except Exception as e:
            log_queue.put(f"[{job_name}] ERROR: Failed to delete temp folder: {e}")
            return False


def perform_cleanup(
    job_details: Dict[str, Any], volumes_to_keep: int, log_queue: LogBuffer
) -> None:
    """Perform backup rotation - keep only N most recent backups."""
    job_name = job_details["name"]
    backup_folder = job_details["destination_base"]

    log_queue.put(f"[{job_name}] Starting Backup Rotation Check...")
    log_queue.put(f"[{job_name}]   Folder: {backup_folder}, Keep: {volumes_to_keep}")

    search_pattern = os.path.join(backup_folder, f"{job_name}_*.zip")
    log_queue.put(f"[{job_name}]   Searching with pattern: {search_pattern}")

    try:
        backup_files = sorted(glob.glob(search_pattern))
        backup_count = len(backup_files)
        log_queue.put(f"[{job_name}]   Found {backup_count} backups for this job.")

        if backup_count > volumes_to_keep:
            delete_count = backup_count - volumes_to_keep
            log_queue.put(f"[{job_name}]   Need to delete {delete_count} backups.")
            for file_path in backup_files[:delete_count]:
                log_queue.put(
                    f"[{job_name}]     Deleting: {os.path.basename(file_path)}"
                )
                try:
                    os.remove(file_path)
                except OSError as e:
                    log_queue.put(f"[{job_name}]     WARNING: Delete failed: {e}")
        else:
            log_queue.put(f"[{job_name}]   No cleanup needed for this job.")

    except Exception as e:
        log_queue.put(f"[{job_name}] ERROR during cleanup search: {e}")


def run_backup_job(
    job_details: Dict[str, Any], global_settings: Dict[str, Any], log_queue: LogBuffer
) -> Dict[str, Any]:
    """
    Execute a single backup job.

    Returns:
        Dict with 'success', 'job_id', 'duration', 'message' keys
    """
    job_name = job_details["name"]
    start_time = time.time()
    total_steps = 4

    def update_status(step: int, message: str = "") -> None:
        log_queue.put(("status", job_name, step, total_steps, message))

    update_status(0, "Starting...")

    # Check if job is enabled
    if not job_details.get("enabled", False):
        log_queue.put(f"[{job_name}] SKIPPED: Job is disabled.")
        update_status(0, "Skipped (Disabled)")
        return {
            "success": False,
            "job_id": job_name,
            "duration": 0.0,
            "message": "Job is disabled",
        }

    backup_folder = job_details["destination_base"]

    # Determine retention policy
    job_specific_volumes = job_details.get("volumes_to_keep_override")
    if job_specific_volumes is not None and job_specific_volumes > 0:
        volumes_to_keep = job_specific_volumes
        log_queue.put(
            f"[{job_name}] Using job-specific retention: {volumes_to_keep} backups."
        )
    else:
        volumes_to_keep = global_settings.get("default_volumes_to_keep", 3)
        log_queue.put(
            f"[{job_name}] Using global retention: {volumes_to_keep} backups."
        )

    # Setup paths
    os.makedirs(backup_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    temp_copy_dir = os.path.join(backup_folder, f"Temp_{job_name}_{timestamp}")
    zip_file = os.path.join(backup_folder, f"{job_name}_{timestamp}.zip")

    # Step 1: Copy files
    update_status(1, "Copying files...")
    copy_exit_code = run_file_copy(job_details, temp_copy_dir, log_queue)

    # Determine copy success
    if not IS_WINDOWS:
        copy_ok = copy_exit_code == 0
    else:
        is_wsl = job_details["source_dir"].lower().startswith("\\wsl")
        if is_wsl:
            copy_ok = copy_exit_code == 0
        else:
            copy_ok = copy_exit_code < 8

    # Log specific errors
    if not IS_WINDOWS and copy_exit_code != 0:
        log_queue.put(
            f"[{job_name}] FATAL ERROR: rsync failed with exit code {copy_exit_code}"
        )
    elif IS_WINDOWS:
        is_wsl = job_details["source_dir"].lower().startswith("\\wsl")
        if not is_wsl and copy_exit_code == 16:
            log_queue.put(
                f"[{job_name}] FATAL ERROR: Robocopy failed. Check paths and permissions."
            )
        elif is_wsl and not copy_ok:
            log_queue.put(
                f"[{job_name}] FATAL ERROR: rsync failed. Check WSL permissions."
            )

    # Step 2: Create ZIP
    zip_ok = False
    if copy_ok:
        update_status(2, "Zipping files...")
        zip_ok = create_zip_archive(job_details, temp_copy_dir, zip_file, log_queue)
    else:
        log_queue.put(f"[{job_name}] Skipping zip due to file copy failure.")
        update_status(2, "Skipping zip...")

    # Step 3: Cleanup temp
    update_status(3, "Cleaning temp files...")
    if os.path.exists(temp_copy_dir):
        cleanup_temp_dir(job_details, temp_copy_dir, log_queue)
    else:
        log_queue.put(f"[{job_name}] Temp dir doesn't exist.")

    # Step 4: Rotate old backups
    update_status(4, "Cleaning old backups...")
    if zip_ok:
        perform_cleanup(job_details, volumes_to_keep, log_queue)
    else:
        log_queue.put(f"[{job_name}] Skipping rotation due to zip failure.")

    duration = time.time() - start_time

    # Final status
    if copy_ok and zip_ok:
        log_queue.put(f"--- Job: {job_name} COMPLETED SUCCESSFULLY ---")
        update_status(0, "Finished Successfully!")
        return {
            "success": True,
            "job_id": job_name,
            "duration": round(duration, 2),
            "message": "Backup completed successfully",
            "zip_file": zip_file,
        }
    else:
        log_queue.put(f"--- Job: {job_name} FAILED ---")
        update_status(0, "Finished with Errors!")
        return {
            "success": False,
            "job_id": job_name,
            "duration": round(duration, 2),
            "message": "Backup failed - see logs for details",
            "copy_ok": copy_ok,
            "zip_ok": zip_ok,
        }


# ==============================================================================
# BACKUP QUEUE MANAGER
# ==============================================================================
class BackupQueueManager:
    """Thread-safe sequential job queue for backup execution."""

    def __init__(self):
        self._job_queue: queue.Queue = queue.Queue()
        self._is_running = False
        self._lock = threading.Lock()
        self._current_job: Optional[str] = None
        self._job_results: Dict[str, Dict[str, Any]] = {}

    def add_job(
        self,
        job_details: Dict[str, Any],
        global_settings: Dict[str, Any],
        log_queue: LogBuffer,
        result_callback: Optional[Callable] = None,
    ) -> None:
        """Add a job to the queue."""
        job_name = job_details["name"]
        self._job_queue.put((job_details, global_settings, log_queue, result_callback))
        log_queue.put(f"Queue Manager: Job '{job_name}' added to queue.")
        self._process_queue()

    def _process_queue(self) -> None:
        """Process the next job in the queue."""
        with self._lock:
            if self._is_running:
                return
            if self._job_queue.empty():
                return
            self._is_running = True

        try:
            job_data = self._job_queue.get_nowait()
        except queue.Empty:
            with self._lock:
                self._is_running = False
            return

        threading.Thread(
            target=self._run_job_wrapper, args=job_data, daemon=True
        ).start()

    def _run_job_wrapper(
        self,
        job_details: Dict[str, Any],
        global_settings: Dict[str, Any],
        log_queue: LogBuffer,
        result_callback: Optional[Callable] = None,
    ) -> None:
        """Wrapper to run job and handle completion."""
        job_name = job_details["name"]
        self._current_job = job_name

        try:
            result = run_backup_job(job_details, global_settings, log_queue)
            self._job_results[job_name] = result

            if result_callback:
                try:
                    result_callback(result)
                except Exception as e:
                    logger.error(f"Result callback error: {e}")

        except Exception as e:
            logger.error(f"Queue Manager Error running job {job_name}: {e}")
            log_queue.put(f"Queue Manager Error: {e}")
            self._job_results[job_name] = {
                "success": False,
                "job_id": job_name,
                "error": str(e),
            }
        finally:
            with self._lock:
                self._is_running = False
                self._current_job = None
            time.sleep(0.5)  # Brief pause before next job
            self._process_queue()

    def is_running(self) -> bool:
        """Check if a job is currently running."""
        with self._lock:
            return self._is_running

    def get_current_job(self) -> Optional[str]:
        """Get the name of the currently running job."""
        return self._current_job

    def get_job_result(self, job_name: str) -> Optional[Dict[str, Any]]:
        """Get the result of a completed job."""
        return self._job_results.get(job_name)

    def clear_results(self) -> None:
        """Clear stored job results."""
        self._job_results.clear()


# ==============================================================================
# SCHEDULER MANAGEMENT
# ==============================================================================
class SchedulerManager:
    """Manages APScheduler for automated backups."""

    def __init__(self, backup_manager: "BackupManager"):
        self.backup_manager = backup_manager
        self.scheduler: Optional[BackgroundScheduler] = None
        self._log_queue: Optional[LogBuffer] = None

        if not APS_AVAILABLE:
            logger.warning("APScheduler not available. Scheduling disabled.")

    def start(self, log_queue: LogBuffer) -> bool:
        """Start the scheduler daemon."""
        if not APS_AVAILABLE:
            logger.error("Cannot start scheduler: APScheduler not installed")
            return False

        if self.scheduler and self.scheduler.running:
            logger.info("Scheduler already running")
            return True

        try:
            self._log_queue = log_queue
            self.scheduler = BackgroundScheduler(daemon=True)
            self.scheduler.start()
            logger.info("Scheduler started successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")
            return False

    def stop(self) -> bool:
        """Stop the scheduler gracefully."""
        if not self.scheduler or not self.scheduler.running:
            logger.info("Scheduler not running")
            return True

        try:
            self.scheduler.shutdown(wait=True)
            logger.info("Scheduler stopped")
            return True
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")
            return False

    def reload_jobs(self, config: Dict[str, Any]) -> None:
        """Reload all jobs from configuration."""
        if not self.scheduler:
            return

        logger.info("Reloading scheduler jobs...")

        # Remove all existing jobs
        for job in self.scheduler.get_jobs():
            try:
                job.remove()
            except Exception as e:
                logger.warning(f"Could not remove job {job.id}: {e}")

        # Add jobs from config
        if "backup_jobs" in config and "global_settings" in config:
            for job in config["backup_jobs"]:
                self._parse_and_add_job(job, config["global_settings"])

        logger.info("Scheduler jobs reloaded")

    def _parse_and_add_job(
        self, job_details: Dict[str, Any], global_settings: Dict[str, Any]
    ) -> None:
        """Parse schedule string and add job to scheduler."""
        if not self.scheduler:
            return

        job_id = job_details["name"]
        schedule_str = job_details.get("schedule", "manual").lower().strip()
        is_enabled = job_details.get("enabled", False)

        # Remove existing job
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        if not is_enabled or schedule_str == "manual":
            logger.info(f"Job '{job_id}' disabled/manual, not scheduling.")
            return

        trigger = None
        try:
            if schedule_str.startswith("daily@"):
                h, m = map(int, schedule_str.split("@")[1].split(":"))
                trigger = CronTrigger(hour=h, minute=m)
            elif schedule_str.startswith("interval@"):
                m = int(schedule_str.split("@")[1])
                trigger = IntervalTrigger(minutes=m)
            elif schedule_str.startswith("weekly@"):
                parts = schedule_str.split("@")[1].split(":")
                d = parts[0][:3].lower()
                h = int(parts[1])
                m = int(parts[2])
                trigger = CronTrigger(day_of_week=d, hour=h, minute=m)
            else:
                logger.warning(f"Bad schedule format: '{schedule_str}' for '{job_id}'")
        except Exception as e:
            logger.error(f"Error parsing schedule '{schedule_str}' for '{job_id}': {e}")

        if trigger:
            self.scheduler.add_job(
                self._trigger_backup,
                trigger,
                id=job_id,
                name=job_id,
                args=[job_details, global_settings],
                replace_existing=True,
                misfire_grace_time=3600,
            )
            logger.info(f"Scheduled '{job_id}': {trigger}")
            if self._log_queue:
                self._log_queue.put(
                    f"Job '{job_id}' scheduled with trigger: {schedule_str}"
                )

    def _trigger_backup(
        self, job_details: Dict[str, Any], global_settings: Dict[str, Any]
    ) -> None:
        """Called by scheduler to trigger a backup."""
        job_name = job_details["name"]
        logger.info(f"SCHEDULER: Triggered backup for {job_name}")

        if self._log_queue:
            self._log_queue.put(f"SCHEDULER: Triggered backup for {job_name}")

        # Queue the job for execution
        self.backup_manager._queue_job_internal(job_details)

    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self.scheduler is not None and self.scheduler.running


# ==============================================================================
# MAIN BACKUP MANAGER CLASS
# ==============================================================================
class BackupManager:
    """
    Headless Backup Manager for Solace Backup Suite.

    Provides programmatic interface for creating, managing, and executing
    backup jobs without requiring the GUI.

    Attributes:
        config_dir: Directory containing configuration files
        config_path: Full path to the configuration file
        status_file: Path to status JSON file for external monitoring

    Example:
        >>> bm = BackupManager()
        >>> job = bm.create_job(
        ...     name="my_backup",
        ...     source_dir="/home/user/docs",
        ...     destination_base="/backups/docs",
        ...     schedule="daily@02:00"
        ... )
        >>> bm.start_scheduler()
        >>> result = bm.run_job("my_backup", blocking=True)
    """

    def __init__(
        self,
        config_dir: str = "Settings",
        status_file: str = "/tmp/solace_backup_status.json",
    ):
        """
        Initialize BackupManager.

        Args:
            config_dir: Directory for configuration files (default: "Settings")
            status_file: Path to status export file (default: "/tmp/solace_backup_status.json")
        """
        self.config_dir = config_dir
        self.config_path = os.path.join(config_dir, "backup_config.json")
        self.status_file = status_file

        # Initialize components
        self._config: Dict[str, Any] = {}
        self._global_settings: GlobalSettings = GlobalSettings()
        self._jobs: Dict[str, BackupJob] = {}
        self._log_buffer = LogBuffer(max_lines=1000)
        self._queue_manager = BackupQueueManager()
        self._scheduler_manager = SchedulerManager(self)
        self._config_lock = threading.Lock()

        # Load configuration
        self._load_config()

        logger.info("BackupManager initialized")

    def _load_config(self) -> None:
        """Load configuration from file."""
        self._config = load_config(self.config_path)
        self._global_settings = GlobalSettings.from_dict(
            self._config.get("global_settings", {})
        )

        # Load jobs into dict for O(1) access
        self._jobs = {}
        for job_data in self._config.get("backup_jobs", []):
            job = BackupJob.from_dict(job_data)
            self._jobs[job.name] = job

    def _save_config(self) -> bool:
        """Save configuration to file."""
        with self._config_lock:
            self._config["global_settings"] = self._global_settings.to_dict()
            self._config["backup_jobs"] = [job.to_dict() for job in self._jobs.values()]
            return save_config(self.config_path, self._config)

    def _queue_job_internal(self, job_details: Dict[str, Any]) -> None:
        """Internal method to queue a job for execution."""
        self._queue_manager.add_job(
            job_details,
            self._global_settings.to_dict(),
            self._log_buffer,
            result_callback=self._on_job_complete,
        )

    def _on_job_complete(self, result: Dict[str, Any]) -> None:
        """Callback when a job completes."""
        job_id = result.get("job_id")
        success = result.get("success", False)

        # Update job status in config
        if job_id in self._jobs:
            self._jobs[job_id].last_run = datetime.now().isoformat()
            self._jobs[job_id].last_status = "success" if success else "failed"
            self._save_config()

        # Update status file
        self._update_status_file(
            "IDLE" if success else "ERROR",
            f"Job {job_id}: {'completed' if success else 'failed'}",
        )

    def _update_status_file(self, status: str, message: str) -> None:
        """Write current status to status file."""
        try:
            data = {
                "status": status,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                "pid": os.getpid(),
                "current_job": self._queue_manager.get_current_job(),
                "scheduler_running": self._scheduler_manager.is_running(),
            }
            with open(self.status_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"Could not write status file: {e}")

    # ==========================================================================
    # JOB MANAGEMENT API
    # ==========================================================================

    def create_job(
        self,
        name: str,
        source_dir: str,
        destination_base: str,
        schedule: str = "manual",
        enabled: bool = True,
        exclusions: Optional[List[str]] = None,
        volumes_to_keep: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Create a new backup job.

        Args:
            name: Unique job identifier
            source_dir: Directory to backup
            destination_base: Directory to store backup archives
            schedule: Schedule format: "manual", "daily@HH:MM", "interval@MINUTES", "weekly@DAY:HH:MM"
            enabled: Whether job is enabled
            exclusions: List of directory/file patterns to exclude
            volumes_to_keep: Number of backups to retain (None = use global default)

        Returns:
            Dict with 'success', 'data' (job dict), 'message'
        """
        # Validate inputs
        if not name or not name.strip():
            return {
                "success": False,
                "error": "Job name is required",
                "code": "INVALID_NAME",
            }

        name = name.strip()

        # Check for duplicate
        if name in self._jobs:
            return {
                "success": False,
                "error": f"Job '{name}' already exists",
                "code": "DUPLICATE_JOB",
            }

        # Validate source directory
        if not os.path.exists(source_dir):
            return {
                "success": False,
                "error": f"Source directory does not exist: {source_dir}",
                "code": "INVALID_SOURCE",
            }

        # Validate schedule format
        valid_schedules = ["manual"]
        if schedule != "manual":
            if schedule.startswith("daily@"):
                try:
                    h, m = map(int, schedule.split("@")[1].split(":"))
                    if not (0 <= h <= 23 and 0 <= m <= 59):
                        raise ValueError()
                except (ValueError, IndexError):
                    return {
                        "success": False,
                        "error": "Invalid daily schedule format. Use: daily@HH:MM",
                        "code": "INVALID_SCHEDULE",
                    }
            elif schedule.startswith("interval@"):
                try:
                    m = int(schedule.split("@")[1])
                    if m < 1:
                        raise ValueError()
                except (ValueError, IndexError):
                    return {
                        "success": False,
                        "error": "Invalid interval schedule format. Use: interval@MINUTES",
                        "code": "INVALID_SCHEDULE",
                    }
            elif schedule.startswith("weekly@"):
                try:
                    parts = schedule.split("@")[1].split(":")
                    d = parts[0][:3].lower()
                    h = int(parts[1])
                    m = int(parts[2])
                    if d not in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]:
                        raise ValueError()
                    if not (0 <= h <= 23 and 0 <= m <= 59):
                        raise ValueError()
                except (ValueError, IndexError):
                    return {
                        "success": False,
                        "error": "Invalid weekly schedule format. Use: weekly@DAY:HH:MM",
                        "code": "INVALID_SCHEDULE",
                    }
            else:
                return {
                    "success": False,
                    "error": f"Unknown schedule format: {schedule}",
                    "code": "INVALID_SCHEDULE",
                }

        # Create job
        job = BackupJob(
            name=name,
            source_dir=os.path.abspath(source_dir),
            destination_base=os.path.abspath(destination_base),
            schedule=schedule,
            enabled=enabled,
            exclusions=exclusions or [],
            volumes_to_keep_override=volumes_to_keep,
        )

        # Save
        self._jobs[name] = job
        if self._save_config():
            # Update scheduler if running
            if self._scheduler_manager.is_running():
                self._scheduler_manager.reload_jobs(self._config)

            logger.info(f"Created backup job: {name}")
            return {
                "success": True,
                "data": job.to_dict(),
                "message": f"Job '{name}' created successfully",
            }
        else:
            del self._jobs[name]
            return {
                "success": False,
                "error": "Failed to save configuration",
                "code": "SAVE_ERROR",
            }

    def list_jobs(self) -> List[Dict[str, Any]]:
        """
        List all backup jobs.

        Returns:
            List of job configuration dicts
        """
        return [job.to_dict() for job in self._jobs.values()]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single job by ID.

        Args:
            job_id: Job name/identifier

        Returns:
            Job configuration dict or None if not found
        """
        job = self._jobs.get(job_id)
        return job.to_dict() if job else None

    def update_job(self, job_id: str, **kwargs) -> Dict[str, Any]:
        """
        Update job fields.

        Args:
            job_id: Job name/identifier
            **kwargs: Fields to update (name, source_dir, destination_base, schedule,
                     enabled, exclusions, volumes_to_keep)

        Returns:
            Dict with 'success', 'data' (updated job), 'message'
        """
        if job_id not in self._jobs:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found",
                "code": "NOT_FOUND",
            }

        job = self._jobs[job_id]

        # Update allowed fields
        allowed_fields = {
            "name",
            "source_dir",
            "destination_base",
            "schedule",
            "enabled",
            "exclusions",
            "volumes_to_keep",
        }

        for key, value in kwargs.items():
            if key not in allowed_fields:
                return {
                    "success": False,
                    "error": f"Cannot update field: {key}",
                    "code": "INVALID_FIELD",
                }

            if key == "volumes_to_keep":
                job.volumes_to_keep_override = value
            elif key == "name" and value != job_id:
                # Handle rename
                if value in self._jobs:
                    return {
                        "success": False,
                        "error": f"Job name '{value}' already exists",
                        "code": "DUPLICATE_NAME",
                    }
                job.name = value
                del self._jobs[job_id]
                self._jobs[value] = job
                job_id = value  # Update for return
            else:
                setattr(job, key, value)

        if self._save_config():
            # Update scheduler if running
            if self._scheduler_manager.is_running():
                self._scheduler_manager.reload_jobs(self._config)

            return {
                "success": True,
                "data": job.to_dict(),
                "message": f"Job '{job_id}' updated successfully",
            }
        else:
            return {
                "success": False,
                "error": "Failed to save configuration",
                "code": "SAVE_ERROR",
            }

    def delete_job(self, job_id: str) -> Dict[str, Any]:
        """
        Delete a backup job.

        Args:
            job_id: Job name/identifier

        Returns:
            Dict with 'success', 'message'
        """
        if job_id not in self._jobs:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found",
                "code": "NOT_FOUND",
            }

        del self._jobs[job_id]

        if self._save_config():
            # Update scheduler if running
            if self._scheduler_manager.is_running():
                self._scheduler_manager.reload_jobs(self._config)

            logger.info(f"Deleted backup job: {job_id}")
            return {"success": True, "message": f"Job '{job_id}' deleted successfully"}
        else:
            return {
                "success": False,
                "error": "Failed to save configuration",
                "code": "SAVE_ERROR",
            }

    def enable_job(self, job_id: str) -> Dict[str, Any]:
        """Enable a backup job."""
        return self.update_job(job_id, enabled=True)

    def disable_job(self, job_id: str) -> Dict[str, Any]:
        """Disable a backup job."""
        return self.update_job(job_id, enabled=False)

    # ==========================================================================
    # BACKUP EXECUTION API
    # ==========================================================================

    def run_job(self, job_id: str, blocking: bool = False) -> Dict[str, Any]:
        """
        Trigger immediate backup execution.

        Args:
            job_id: Job name/identifier
            blocking: If True, wait for completion and return result.
                     If False, queue job and return immediately.

        Returns:
            Dict with 'success', 'message', and optionally 'data' (result dict)
        """
        if job_id not in self._jobs:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found",
                "code": "NOT_FOUND",
            }

        job = self._jobs[job_id]

        if not job.enabled:
            return {
                "success": False,
                "error": f"Job '{job_id}' is disabled",
                "code": "JOB_DISABLED",
            }

        self._update_status_file("RUNNING", f"Executing job: {job_id}")

        if blocking:
            # Execute synchronously
            result = run_backup_job(
                job.to_dict(), self._global_settings.to_dict(), self._log_buffer
            )
            self._on_job_complete(result)
            self._update_status_file("IDLE", f"Job {job_id} completed")
            return {
                "success": result.get("success", False),
                "data": result,
                "message": result.get("message", "Backup completed"),
            }
        else:
            # Queue for async execution
            self._queue_job_internal(job.to_dict())
            return {
                "success": True,
                "message": f"Job '{job_id}' queued for execution",
                "job_id": job_id,
            }

    def run_all_enabled_jobs(self) -> Dict[str, Any]:
        """
        Run all enabled backup jobs sequentially.

        Returns:
            Dict with 'success', 'message', 'queued_jobs'
        """
        enabled_jobs = [job for job in self._jobs.values() if job.enabled]

        if not enabled_jobs:
            return {
                "success": True,
                "message": "No enabled jobs to run",
                "queued_jobs": [],
            }

        queued = []
        for job in enabled_jobs:
            self._queue_job_internal(job.to_dict())
            queued.append(job.name)

        self._update_status_file("RUNNING", f"Running {len(queued)} jobs")

        return {
            "success": True,
            "message": f"Queued {len(queued)} jobs for execution",
            "queued_jobs": queued,
        }

    def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """
        Get current status of a job.

        Args:
            job_id: Job name/identifier

        Returns:
            Dict with job status information
        """
        job = self._jobs.get(job_id)
        if not job:
            return {
                "success": False,
                "error": f"Job '{job_id}' not found",
                "code": "NOT_FOUND",
            }

        is_running = self._queue_manager.get_current_job() == job_id

        # Get next scheduled run if scheduler is running
        next_run = None
        if self._scheduler_manager.is_running() and job.schedule != "manual":
            try:
                scheduler_job = self._scheduler_manager.scheduler.get_job(job_id)
                if scheduler_job and scheduler_job.next_run_time:
                    next_run = scheduler_job.next_run_time.isoformat()
            except Exception:
                pass

        return {
            "success": True,
            "data": {
                "job_id": job_id,
                "enabled": job.enabled,
                "is_running": is_running,
                "last_run": job.last_run,
                "last_status": job.last_status,
                "next_run": next_run,
                "schedule": job.schedule,
            },
        }

    # ==========================================================================
    # SCHEDULER API
    # ==========================================================================

    def start_scheduler(self) -> Dict[str, Any]:
        """
        Start the background scheduler for automated backups.

        Returns:
            Dict with 'success', 'message'
        """
        if not APS_AVAILABLE:
            return {
                "success": False,
                "error": "APScheduler not installed. Install with: pip install apscheduler",
                "code": "SCHEDULER_UNAVAILABLE",
            }

        if self._scheduler_manager.is_running():
            return {"success": True, "message": "Scheduler is already running"}

        if self._scheduler_manager.start(self._log_buffer):
            self._scheduler_manager.reload_jobs(self._config)
            self._update_status_file("IDLE", "Scheduler running")
            return {"success": True, "message": "Scheduler started successfully"}
        else:
            return {
                "success": False,
                "error": "Failed to start scheduler",
                "code": "SCHEDULER_ERROR",
            }

    def stop_scheduler(self) -> Dict[str, Any]:
        """
        Stop the scheduler gracefully.

        Returns:
            Dict with 'success', 'message'
        """
        if not APS_AVAILABLE:
            return {
                "success": False,
                "error": "APScheduler not installed",
                "code": "SCHEDULER_UNAVAILABLE",
            }

        if self._scheduler_manager.stop():
            self._update_status_file("IDLE", "Scheduler stopped")
            return {"success": True, "message": "Scheduler stopped successfully"}
        else:
            return {
                "success": False,
                "error": "Failed to stop scheduler",
                "code": "SCHEDULER_ERROR",
            }

    def reload_scheduler(self) -> Dict[str, Any]:
        """
        Reload all jobs from config into scheduler.

        Returns:
            Dict with 'success', 'message'
        """
        if not APS_AVAILABLE:
            return {
                "success": False,
                "error": "APScheduler not installed",
                "code": "SCHEDULER_UNAVAILABLE",
            }

        if not self._scheduler_manager.is_running():
            return {
                "success": False,
                "error": "Scheduler is not running",
                "code": "SCHEDULER_NOT_RUNNING",
            }

        # Reload config first
        self._load_config()
        self._scheduler_manager.reload_jobs(self._config)

        return {
            "success": True,
            "message": "Scheduler jobs reloaded from configuration",
        }

    def get_scheduler_status(self) -> Dict[str, Any]:
        """
        Get scheduler status.

        Returns:
            Dict with scheduler status information
        """
        if not APS_AVAILABLE:
            return {
                "success": False,
                "error": "APScheduler not installed",
                "code": "SCHEDULER_UNAVAILABLE",
            }

        is_running = self._scheduler_manager.is_running()
        jobs = []

        if is_running:
            try:
                for job in self._scheduler_manager.scheduler.get_jobs():
                    jobs.append(
                        {
                            "id": job.id,
                            "name": job.name,
                            "next_run": job.next_run_time.isoformat()
                            if job.next_run_time
                            else None,
                            "trigger": str(job.trigger),
                        }
                    )
            except Exception as e:
                logger.error(f"Error getting scheduler jobs: {e}")

        return {"success": True, "data": {"running": is_running, "jobs": jobs}}

    # ==========================================================================
    # LOGGING & STATUS API
    # ==========================================================================

    def get_logs(self, job_id: Optional[str] = None, lines: int = 100) -> List[str]:
        """
        Get recent log entries.

        Args:
            job_id: Filter by job name (optional)
            lines: Number of lines to return (default 100)

        Returns:
            List of log lines
        """
        all_logs = self._log_buffer.get_lines(lines)

        if job_id:
            return [line for line in all_logs if f"[{job_id}]" in line]

        return all_logs

    def export_status_file(self) -> Dict[str, Any]:
        """
        Write current status to status file.

        Returns:
            Dict with 'success', 'message'
        """
        try:
            status_data = {
                "status": "IDLE",
                "message": "Backup Manager Active",
                "timestamp": datetime.now().isoformat(),
                "pid": os.getpid(),
                "current_job": self._queue_manager.get_current_job(),
                "scheduler_running": self._scheduler_manager.is_running(),
                "job_count": len(self._jobs),
                "enabled_jobs": sum(1 for j in self._jobs.values() if j.enabled),
            }

            with open(self.status_file, "w") as f:
                json.dump(status_data, f, indent=2)

            return {
                "success": True,
                "message": f"Status exported to {self.status_file}",
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to export status: {e}",
                "code": "EXPORT_ERROR",
            }

    def add_log_callback(self, callback: Callable[[str], None]) -> None:
        """
        Add a callback to receive log messages in real-time.

        Args:
            callback: Function that takes a single string argument
        """
        self._log_buffer.add_callback(callback)

    def add_status_callback(
        self, callback: Callable[[str, int, int, str], None]
    ) -> None:
        """
        Add a callback to receive status updates in real-time.

        Args:
            callback: Function with signature (job_name, step, total_steps, message)
        """
        self._log_buffer.add_status_callback(callback)

    # ==========================================================================
    # GLOBAL SETTINGS API
    # ==========================================================================

    def get_global_settings(self) -> Dict[str, Any]:
        """Get global backup settings."""
        return self._global_settings.to_dict()

    def update_global_settings(self, **kwargs) -> Dict[str, Any]:
        """
        Update global settings.

        Args:
            **kwargs: Settings to update (default_volumes_to_keep, default_backup_base_name, theme)

        Returns:
            Dict with 'success', 'data', 'message'
        """
        allowed = {"default_volumes_to_keep", "default_backup_base_name", "theme"}

        for key, value in kwargs.items():
            if key not in allowed:
                return {
                    "success": False,
                    "error": f"Invalid setting: {key}",
                    "code": "INVALID_SETTING",
                }
            setattr(self._global_settings, key, value)

        if self._save_config():
            return {
                "success": True,
                "data": self._global_settings.to_dict(),
                "message": "Global settings updated",
            }
        else:
            return {
                "success": False,
                "error": "Failed to save configuration",
                "code": "SAVE_ERROR",
            }

    def is_job_running(self) -> bool:
        """Check if any job is currently running."""
        return self._queue_manager.is_running()

    def get_current_job(self) -> Optional[str]:
        """Get the name of the currently running job."""
        return self._queue_manager.get_current_job()


# ==============================================================================
# MODULE TEST
# ==============================================================================
if __name__ == "__main__":
    print("Backup API Module Test")
    print("=" * 50)

    # Test that module can be imported without tkinter
    try:
        import tkinter

        print("WARNING: tkinter is available (expected headless environment)")
    except ImportError:
        print("OK: tkinter not available (headless environment confirmed)")

    # Test BackupManager initialization
    try:
        bm = BackupManager()
        print(f"OK: BackupManager initialized")
        print(f"  Config path: {bm.config_path}")
        print(f"  Status file: {bm.status_file}")
        print(f"  Jobs: {len(bm.list_jobs())}")
        print(f"  APScheduler available: {APS_AVAILABLE}")
    except Exception as e:
        print(f"ERROR: Failed to initialize BackupManager: {e}")
        import traceback

        traceback.print_exc()
        exit(1)

    # Test job CRUD operations
    print("\nTesting job management...")

    # Create test job
    test_result = bm.create_job(
        name="test_job",
        source_dir=".",
        destination_base="/tmp/test_backups",
        schedule="manual",
        enabled=False,
    )
    print(f"Create job: {test_result['success']}")
    if not test_result["success"]:
        print(f"  Error: {test_result.get('error')}")

    # List jobs
    jobs = bm.list_jobs()
    print(f"List jobs: {len(jobs)} job(s)")

    # Get job
    job = bm.get_job("test_job")
    print(f"Get job: {'found' if job else 'not found'}")

    # Update job
    update_result = bm.update_job("test_job", volumes_to_keep=5)
    print(f"Update job: {update_result['success']}")

    # Enable/disable
    enable_result = bm.enable_job("test_job")
    print(f"Enable job: {enable_result['success']}")

    disable_result = bm.disable_job("test_job")
    print(f"Disable job: {disable_result['success']}")

    # Get status
    status = bm.get_job_status("test_job")
    print(f"Get status: {status['success']}")

    # Delete job
    delete_result = bm.delete_job("test_job")
    print(f"Delete job: {delete_result['success']}")

    # Test scheduler status
    print("\nTesting scheduler...")
    sched_status = bm.get_scheduler_status()
    print(f"Scheduler available: {sched_status.get('success', False)}")

    # Test global settings
    print("\nTesting global settings...")
    settings = bm.get_global_settings()
    print(f"Settings: {settings}")

    # Test status export
    print("\nTesting status export...")
    export_result = bm.export_status_file()
    print(f"Export: {export_result['success']}")

    print("\n" + "=" * 50)
    print("Module test completed successfully!")
