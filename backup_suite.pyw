#!/usr/bin/env python3
# backup_suite_fixed.pyw
# A GUI Backup Application with Themes, Scheduling, System Tray, Startup Option, Per-Job Retention, and UI Adjustments

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import tkinter.font
from project_vault import ProjectVault
from branch_visualizer import BranchTreeVisualizer
import json
import os
import logging
from datetime import datetime
import subprocess
import shutil
import glob
import threading
import queue
import time
import io # Needed for Popen output handling
import platform
import zipfile
import sys    # For executable path and sys.argv
import signal # For Unix signal handling (Waybar integration)
import argparse # For command line argument parsing

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import winreg # For Windows startup registry

# --- Attempt to import Scheduling & Tray libraries ---
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    APS_AVAILABLE = True
except ImportError:
    APS_AVAILABLE = False
    print("WARNING: APScheduler not found. Scheduling will be disabled.")
    print("         Install with: pip install apscheduler")

try:
    # On Wayland (e.g. Hyprland), use AppIndicator/SNI backend for system tray
    # The 'gtk' backend uses deprecated GtkStatusIcon (invisible on Wayland)
    # The 'appindicator' backend uses StatusNotifierItem protocol (works with Waybar)
    if not IS_WINDOWS and os.environ.get('WAYLAND_DISPLAY'):
        os.environ.setdefault('PYSTRAY_BACKEND', 'appindicator')
    import pystray
    from PIL import Image, ImageDraw, ImageTk, ImageOps
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    print("WARNING: pystray or Pillow not found. System tray icon will be disabled.")
    print("         Install with: pip install pystray pillow")

# ==============================================================================
# 0. THEME DEFINITIONS
# ==============================================================================
THEMES = {
    "Light (Default)": {
        "BG_COLOR": "#F0F0F0", "FRAME_BG": "#FFFFFF", "TEXT_COLOR": "#333333",
        "BUTTON_BG": "#E1E1E1", "BUTTON_ACTIVE": "#C0E0FF", "ACCENT_COLOR": "#0078D7",
        "LIST_BG": "#FFFFFF", "LIST_ALT_BG": "#F5F5F5", "HEADER_BG": "#FFFFFF",
        "LOG_BG": "#FFFFFF", "LOG_FG": "#333333", "ENTRY_BG": "#FFFFFF", "ENTRY_FG": "#333333",
        "SELECT_BG": "#0078D7", "SELECT_FG": "#FFFFFF", "SPIN_BG": "#FFFFFF", "TEXT_INSERT": "#000000",
        "BORDER": "#CCCCCC", "TAB_BG": "#D9D9D9",
    },
    "Dark Mode": {
        "BG_COLOR": "#2d353b", "FRAME_BG": "#343f44", "TEXT_COLOR": "#d3c6aa",
        "BUTTON_BG": "#475258", "BUTTON_ACTIVE": "#4f5b62", "ACCENT_COLOR": "#d3c6aa",
        "LIST_BG": "#343f44", "LIST_ALT_BG": "#3d484d", "HEADER_BG": "#343f44",
        "LOG_BG": "#232a2e", "LOG_FG": "#d3c6aa", "ENTRY_BG": "#475258", "ENTRY_FG": "#d3c6aa",
        "SELECT_BG": "#d3c6aa", "SELECT_FG": "#2d353b", "SPIN_BG": "#475258", "TEXT_INSERT": "#d3c6aa",
        "BORDER": "#d3c6aa", "TAB_BG": "#262E33",
    },
}

# ==============================================================================
# 0.5 ASCII ART FRAMES
# ==============================================================================

# ==============================================================================
# 0.5 ASCII ART FRAMES (REMOVED)
# ==============================================================================
# LOGO_FRAMES removed in favor of PNG logo.

def get_system_theme():
    """Detects the system theme (Light or Dark)."""
    try:
        if IS_WINDOWS:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                winreg.CloseKey(key)
                return "Light (Default)" if value == 1 else "Dark Mode"
            except Exception:
                return "Light (Default)"
        else:
            # Linux detection via gsettings
            try:
                # Check for GTK theme preference
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                    capture_output=True, text=True
                )
                output = result.stdout.strip().strip("'")
                if "dark" in output.lower():
                    return "Dark Mode"
                
                # Fallback check for legacy GTK theme name
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                    capture_output=True, text=True
                )
                output = result.stdout.strip().strip("'")
                if "dark" in output.lower():
                    return "Dark Mode"
            except FileNotFoundError:
                pass
                
            return "Dark Mode" # Default to Dark on Linux if detection fails
    except Exception as e:
        logging.warning(f"Failed to detect system theme: {e}")
        return "Light (Default)"

current_theme_colors = THEMES["Light (Default)"]

# ==============================================================================
# 1. LOGGING CONFIGURATION
# ==============================================================================
LOG_DIR = "Debug"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "backup_suite_debug.log")
STATUS_FILE = "/tmp/solace_backup_status.json"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    filemode='w'
)
logging.info(f"Application Starting Up... Executable: {sys.executable}, Script: {os.path.abspath(sys.argv[0])}")


def check_for_running_instance():
    """
    Check if another instance of the application is already running.
    Returns True if a valid running instance exists, False otherwise.
    Uses the existing STATUS_FILE to check for running PID.
    """
    if not os.path.exists(STATUS_FILE):
        return False
    
    try:
        with open(STATUS_FILE, 'r') as f:
            data = json.load(f)
        
        pid = data.get('pid')
        if pid is None:
            return False
        
        # Check if the process is still running
        try:
            # On Unix, sending signal 0 checks if process exists without killing it
            os.kill(pid, 0)
            logging.warning(f"Found running instance with PID {pid}. Exiting.")
            return pid
        except OSError:
            # Process doesn't exist (errno ESRCH) - stale status file
            logging.info(f"Stale status file found for PID {pid}. Starting new instance.")
            return False
    except (json.JSONDecodeError, IOError, KeyError) as e:
        logging.warning(f"Could not parse status file: {e}")
        return False


# ==============================================================================
# 2. CONFIGURATION MANAGEMENT
# ==============================================================================
CONFIG_DIR = "Settings"
CONFIG_FILE = "backup_config.json"
CONFIG_PATH = os.path.join(CONFIG_DIR, CONFIG_FILE)

def get_default_config():
    return {
        "global_settings": {
            "default_volumes_to_keep": 3,
            "default_backup_base_name": "Backups_Py",
            "theme": "Light (Default)"
        },
        "backup_jobs": []
    }

def load_config():
    logging.info(f"Attempting to load configuration from: {CONFIG_PATH}")
    if not os.path.exists(CONFIG_PATH):
        logging.warning("Configuration file not found. Creating a default one.")
        config_data = get_default_config()
        save_config(config_data)
        return config_data
    else:
        try:
            with open(CONFIG_PATH, 'r') as f:
                config_data = json.load(f)
                if "theme" not in config_data.get("global_settings", {}):
                    config_data["global_settings"]["theme"] = "Light (Default)"
                    save_config(config_data)
                logging.info("Configuration loaded successfully.")
                return config_data
        except (json.JSONDecodeError, IOError) as e:
            logging.error(f"Error loading/decoding {CONFIG_PATH}: {e}. Backing up and creating default.")
            if os.path.exists(CONFIG_PATH):
                try:
                    bak_path = f"{CONFIG_PATH}.bak_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                    os.rename(CONFIG_PATH, bak_path)
                    logging.info(f"Backed up corrupted config to {bak_path}")
                except OSError as e_rename:
                    logging.error(f"Could not rename corrupted config: {e_rename}")
            config_data = get_default_config()
            save_config(config_data)
            return config_data

def save_config(config_data):
    logging.info(f"Saving configuration to: {CONFIG_PATH}")
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config_data, f, indent=4)
        logging.info("Configuration saved successfully.")
        return True
    except Exception as e:
        logging.error(f"Failed to save config: {e}")
        return False

# ==============================================================================
# 2.5 STARTUP REGISTRY/AUTOSTART FUNCTIONS
# ==============================================================================
REG_APP_NAME = "PythonBackupSuite"
REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
LINUX_AUTOSTART_DIR = os.path.expanduser("~/.config/autostart")
LINUX_DESKTOP_FILE = os.path.join(LINUX_AUTOSTART_DIR, "solace_backup.desktop")

def get_application_path_for_startup():
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        app_path = sys.executable
        return f'"{app_path}"'
    else:
        python_exe = sys.executable
        if python_exe.lower().endswith("python.exe"):
            pythonw_exe_alt = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
            if os.path.exists(pythonw_exe_alt):
                python_exe = pythonw_exe_alt
        script_path = os.path.abspath(sys.argv[0])
        return f'"{python_exe}" "{script_path}"'

def add_to_startup():
    try:
        command = get_application_path_for_startup()
        
        if IS_WINDOWS:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_WRITE)
            winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, command)
            winreg.CloseKey(key)
        else:
            # Linux .desktop file creation
            os.makedirs(LINUX_AUTOSTART_DIR, exist_ok=True)
            python_exe = sys.executable
            script_path = os.path.abspath(sys.argv[0])
            
            desktop_content = f"""[Desktop Entry]
Type=Application
Name=Solace Backup
Exec={python_exe} "{script_path}"
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=Solace Backup Suite
"""
            with open(LINUX_DESKTOP_FILE, "w") as f:
                f.write(desktop_content)
            os.chmod(LINUX_DESKTOP_FILE, 0o755)

        logging.info(f"Application added to startup: {command}")
        if main_app_ref: main_app_ref.log_message_gui("Application added to System startup.")
        return True
    except Exception as e:
        logging.error(f"Error adding to startup: {e}")
        if main_app_ref: main_app_ref.log_message_gui(f"ERROR adding to startup: {e}")
        return False

def remove_from_startup():
    try:
        if IS_WINDOWS:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_WRITE)
            winreg.DeleteValue(key, REG_APP_NAME)
            winreg.CloseKey(key)
        else:
            if os.path.exists(LINUX_DESKTOP_FILE):
                os.remove(LINUX_DESKTOP_FILE)

        logging.info("Application removed from startup.")
        if main_app_ref: main_app_ref.log_message_gui("Application removed from System startup.")
        return True
    except FileNotFoundError:
        logging.info("Application was not in startup.")
        return True
    except Exception as e:
        logging.error(f"Error removing from startup: {e}")
        if main_app_ref: main_app_ref.log_message_gui(f"ERROR removing from startup: {e}")
        return False

def check_if_in_startup():
    try:
        if IS_WINDOWS:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, REG_APP_NAME)
            winreg.CloseKey(key)
            return True
        else:
            return os.path.exists(LINUX_DESKTOP_FILE)
    except FileNotFoundError:
        return False
    except Exception as e:
        logging.error(f"Error checking startup status: {e}")
        return False

# ==============================================================================
# 3. CORE BACKUP LOGIC
# ==============================================================================
def get_subprocess_flags():
    """Returns platform-specific creation flags for subprocesses."""
    if IS_WINDOWS:
        return {'creationflags': subprocess.CREATE_NO_WINDOW}
    return {}

def read_subprocess_output(process, log_queue, job_name):
    try:
        # Use a more generic encoding handling
        with io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace') as stdout_reader:
            for line in iter(stdout_reader.readline, ''):
                line = line.strip()
                if line:
                    # Robocopy specific parsing (tabs)
                    if IS_WINDOWS and line.startswith('\t'):
                        parts = line.split('\t')
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

def run_file_copy(job_details, temp_dest_dir, log_queue):
    """
    Handles file copying, automatically choosing between Robocopy for standard
    Windows paths and rsync (via WSL) for WSL paths.
    """
    source_dir = job_details['source_dir']
    job_name = job_details['name']
    exclusions = job_details.get('exclusions', [])

    # --- Linux Native (Rsync) ---
    if not IS_WINDOWS:
        log_queue.put(f"[{job_name}] Starting Rsync (Linux)...")
        
        # Ensure source ends with / for rsync to copy contents, not the dir itself
        src_path = source_dir if source_dir.endswith(os.sep) else source_dir + os.sep
        
        command = ["rsync", "-av", "--delete", src_path, temp_dest_dir]
        for exclusion in exclusions:
            command.append(f"--exclude={exclusion}")
            
        log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **get_subprocess_flags())
            
            # We can use the same reader thread, it's generic enough now
            reader_thread = threading.Thread(target=read_subprocess_output, args=(process, log_queue, job_name),
                                             daemon=True, name=f"RsyncRead-{job_name}")
            reader_thread.start()
            process.wait()
            reader_thread.join(timeout=5)
            
            return_code = process.returncode
            log_queue.put(f"[{job_name}]   Rsync finished with Exit Code: {return_code}")
            return return_code
        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: rsync not found. Please install rsync.")
            return -1
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during rsync: {e}")
            return -2

    # --- WSL Path Detection ---
    is_wsl_path = source_dir.lower().startswith('\\\\wsl')

    if is_wsl_path:
        log_queue.put(f"[{job_name}] WSL path detected. Using rsync via wsl.exe...")
        # Translate paths from \\wsl.localhost\Ubuntu\home\... to /home/...
        # This logic robustly finds the distro name after the initial share path.
        try:
            path_parts = source_dir.lower().split('\\')
            # Expected path: '', '', 'wsl.localhost' or 'wsl$', 'distro_name', ...
            if len(path_parts) < 4:
                raise ValueError("WSL path is not in the expected format.")
            distro_name = path_parts[3]
            wsl_prefix = f"\\\\{path_parts[2]}\\{distro_name}"
            
            source_linux = source_dir[len(wsl_prefix):].replace("\\", "/")
            temp_dest_linux = temp_dest_dir[len(wsl_prefix):].replace("\\", "/")
        except (ValueError, IndexError) as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: Could not parse WSL path '{source_dir}'. Error: {e}")
            return -2

        # Ensure source path ends with a slash for rsync to copy contents
        if not source_linux.endswith('/'):
            source_linux += '/'

        command = ["wsl", "-d", distro_name, "rsync", "-av", source_linux, temp_dest_linux]
        # rsync exclusions are different from robocopy
        for exclusion in exclusions:
            command.append(f"--exclude={exclusion}")

        log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")
        try:
            # For WSL/rsync, we can use subprocess.run as output is less verbose
            process = subprocess.run(command, capture_output=True, text=True, check=False, **get_subprocess_flags())
            return_code = process.returncode
            log_queue.put(f"[{job_name}]   rsync finished with Exit Code: {return_code}")
            if return_code != 0:
                log_queue.put(f"[{job_name}]   rsync stderr: {process.stderr.strip()}")
            return return_code # rsync exit code 0 is success
        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: wsl.exe not found. Is WSL installed and in your PATH?")
            return -1
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during rsync: {e}")
            return -2

    else: # --- Standard Windows Path ---
        log_queue.put(f"[{job_name}] Starting Robocopy...")
        command = ["robocopy", source_dir, temp_dest_dir, "/E", "/COPY:DAT", "/R:1", "/W:1", "/BYTES", "/NJH", "/NJS", "/NDL", "/NP"]
        for exclusion in exclusions: command.append("/XD"); command.append(exclusion)
        log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       **get_subprocess_flags())
            reader_thread = threading.Thread(target=read_subprocess_output, args=(process, log_queue, job_name),
                                             daemon=True, name=f"RoboRead-{job_name}")
            reader_thread.start()
            process.wait()
            reader_thread.join(timeout=5)
            return_code = process.returncode
            log_queue.put(f"[{job_name}]   Robocopy finished with Exit Code: {return_code}")
            return return_code
        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: robocopy.exe not found.")
            return -1 # Special code for not found
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during Robocopy: {e}")
            return -2 # Special code for other exceptions

def create_zip_archive(job_details, source_dir, zip_file_path, log_queue):
    job_name = job_details['name']
    log_queue.put(f"[{job_name}] Starting Zipping Process (Internal)...")
    log_queue.put(f"[{job_name}]   Source: {source_dir}")
    log_queue.put(f"[{job_name}]   Zip File: {zip_file_path}")
    
    try:
        with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Calculate relative path for the archive
                    arcname = os.path.relpath(file_path, source_dir)
                    try:
                        zipf.write(file_path, arcname)
                    except (FileNotFoundError, OSError) as e:
                        log_queue.put(f"[{job_name}] WARNING: Skipped missing file during zip: {file} ({e})")
        
        if os.path.exists(zip_file_path) and os.path.getsize(zip_file_path) > 0:
            log_queue.put(f"[{job_name}] SUCCESS: Zip file created."); return True
        else:
            log_queue.put(f"[{job_name}] WARNING: Zip file created but is empty!"); return False

    except Exception as e:
        log_queue.put(f"[{job_name}] CRITICAL ERROR during Zipping: {e}"); return False

def cleanup_temp_dir(job_details, temp_dir, log_queue):
    job_name = job_details['name']
    log_queue.put(f"[{job_name}] Cleaning up temporary folder: {temp_dir}")
    if not os.path.exists(temp_dir):
        log_queue.put(f"[{job_name}]   Temp folder not found, skipping.")
        return True

    is_wsl_path = temp_dir.lower().startswith('\\\\wsl')

    if is_wsl_path:
        log_queue.put(f"[{job_name}]   WSL path detected for cleanup. Using 'rm -rf' via wsl.exe.")
        try:
            path_parts = temp_dir.lower().split('\\')
            if len(path_parts) < 4:
                raise ValueError("WSL path is not in the expected format.")
            distro_name = path_parts[3]
            wsl_prefix = f"\\\\{path_parts[2]}\\{distro_name}"
            
            temp_dir_linux = temp_dir[len(wsl_prefix):].replace("\\", "/")
        except (ValueError, IndexError) as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: Could not parse WSL path for cleanup. Error: {e}")
            return False

        command = ["wsl", "-d", distro_name, "rm", "-rf", temp_dir_linux]
        log_queue.put(f"[{job_name}]   Executing WSL command...")
        try:
            process = subprocess.run(command, capture_output=True, text=True, check=False, **get_subprocess_flags())
            if process.returncode == 0:
                log_queue.put(f"[{job_name}]   Temp folder deleted via WSL.")
                return True
            else:
                log_queue.put(f"[{job_name}] ERROR: WSL 'rm -rf' failed with exit code {process.returncode}.")
                log_queue.put(f"[{job_name}]   stderr: {process.stderr.strip()}")
                return False
        except FileNotFoundError:
            log_queue.put(f"[{job_name}] CRITICAL ERROR: wsl.exe not found."); return False
        except Exception as e:
            log_queue.put(f"[{job_name}] CRITICAL ERROR during WSL cleanup: {e}"); return False

    else: # --- Standard Windows Path or Linux ---
        log_queue.put(f"[{job_name}]   Using standard Python cleanup.")
        try:
            shutil.rmtree(temp_dir)
            log_queue.put(f"[{job_name}]   Temp folder deleted.")
            return True
        except Exception as e:
            log_queue.put(f"[{job_name}] ERROR: Failed to delete temp folder: {e}")
            return False

def perform_cleanup(job_details, volumes_to_keep, log_queue):
    job_name = job_details['name']
    backup_folder = job_details['destination_base']
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
                log_queue.put(f"[{job_name}]     Deleting: {os.path.basename(file_path)}")
                try: os.remove(file_path)
                except OSError as e: log_queue.put(f"[{job_name}]     WARNING: Delete failed: {e}")
        else: log_queue.put(f"[{job_name}]   No cleanup needed for this job.")
    except Exception as e: log_queue.put(f"[{job_name}] ERROR during cleanup search: {e}")

def run_backup_job(job_details, global_settings, log_queue):
    job_name = job_details['name']
    total_steps = 4
    def update_status(step, message=""): log_queue.put(("status", job_name, step, total_steps, message))
    update_status(0, "Starting...")
    if not job_details.get('enabled', False):
        log_queue.put(f"[{job_name}] SKIPPED: Job is disabled.")
        update_status(0, "Skipped (Disabled)"); return

    backup_folder = job_details['destination_base']

    job_specific_volumes = job_details.get("volumes_to_keep_override")
    if job_specific_volumes is not None and job_specific_volumes > 0:
        volumes_to_keep = job_specific_volumes
        log_queue.put(f"[{job_name}] Using job-specific retention: {volumes_to_keep} backups.")
    else:
        volumes_to_keep = global_settings.get('default_volumes_to_keep', 3)
        log_queue.put(f"[{job_name}] Using global retention (defaulting to {volumes_to_keep} backups).")

    os.makedirs(backup_folder, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    temp_copy_dir = os.path.join(backup_folder, f"Temp_{job_name}_{timestamp}")
    zip_file = os.path.join(backup_folder, f"{job_name}_{timestamp}.zip")

    update_status(1, "Copying files...")
    copy_exit_code = run_file_copy(job_details, temp_copy_dir, log_queue)

    # Check for success. Robocopy is successful if exit code is < 8. rsync is successful if 0.
    if not IS_WINDOWS:
        copy_ok = copy_exit_code == 0
    else:
        is_wsl = job_details['source_dir'].lower().startswith('\\\\wsl')
        if is_wsl:
            copy_ok = copy_exit_code == 0
        else:
            copy_ok = copy_exit_code < 8

    # Handle specific Robocopy fatal error
    if not IS_WINDOWS and copy_exit_code != 0:
         log_queue.put(f"[{job_name}] FATAL ERROR: rsync failed with exit code {copy_exit_code}. Check logs.")
    elif IS_WINDOWS:
        is_wsl = job_details['source_dir'].lower().startswith('\\\\wsl')
        if not is_wsl and copy_exit_code == 16:
            log_queue.put(f"[{job_name}] FATAL ERROR: Robocopy failed. This can be caused by an invalid source/destination path (e.g., destination is inside the source) or permission issues. Please check the job configuration.")
        elif is_wsl and not copy_ok:
            log_queue.put(f"[{job_name}] FATAL ERROR: rsync failed with exit code {copy_exit_code}. This is likely a permissions issue within WSL or an invalid path. Check the rsync stderr output above for details.")

    zip_ok = False
    if copy_ok:
        update_status(2, "Zipping files...")
        zip_ok = create_zip_archive(job_details, temp_copy_dir, zip_file, log_queue)
    else:
        log_queue.put(f"[{job_name}] Skipping zip due to file copy failure.")
        update_status(2, "Skipping zip...")

    update_status(3, "Cleaning temp files...")
    if os.path.exists(temp_copy_dir): cleanup_temp_dir(job_details, temp_copy_dir, log_queue)
    else: log_queue.put(f"[{job_name}] Temp dir doesn't exist.")

    update_status(4, "Cleaning old backups...")
    if zip_ok: perform_cleanup(job_details, volumes_to_keep, log_queue)
    else: log_queue.put(f"[{job_name}] Skipping rotation.")

    if copy_ok and zip_ok:
        log_queue.put(f"--- Job: {job_name} COMPLETED SUCCESSFULLY ---"); update_status(0, "Finished Successfully!")
    else:
        log_queue.put(f"--- Job: {job_name} FAILED ---"); update_status(0, "Finished with Errors!")
    time.sleep(2)
    log_queue.put(("status", "Idle", 0, 4, ""))

# ==============================================================================
# 4. SCHEDULER LOGIC (Unchanged)
# ==============================================================================
scheduler = None
if APS_AVAILABLE:
    scheduler = BackgroundScheduler(daemon=True)
    def schedule_trigger_backup(job_details, global_settings, log_queue):
        log_queue.put(f"SCHEDULER: Triggered backup for {job_details['name']}.")
        if main_app_ref and hasattr(main_app_ref, 'queue_manager'):
            main_app_ref.queue_manager.add_job(job_details, global_settings, log_queue)
        else:
            log_queue.put(f"ERROR: Queue manager not available for scheduled job '{job_details['name']}'.")

    def parse_and_add_job_to_scheduler(job_details, global_settings, log_queue):
        if not scheduler: return
        job_id = job_details['name']
        schedule_str = job_details.get('schedule', 'manual').lower().strip()
        is_enabled = job_details.get('enabled', False)
        try: scheduler.remove_job(job_id)
        except Exception: pass
        if not is_enabled or schedule_str == 'manual':
            logging.info(f"Job '{job_id}' disabled/manual, not scheduling."); return
        trigger = None
        try:
            if schedule_str.startswith('daily@'):
                h, m = map(int, schedule_str.split('@')[1].split(':'))
                trigger = CronTrigger(hour=h, minute=m)
            elif schedule_str.startswith('interval@'):
                m = int(schedule_str.split('@')[1])
                trigger = IntervalTrigger(minutes=m)
            elif schedule_str.startswith('weekly@'):
                parts = schedule_str.split('@')[1].split(':'); d=parts[0][:3].lower();h=int(parts[1]);m=int(parts[2])
                trigger = CronTrigger(day_of_week=d, hour=h, minute=m)
            else: logging.warning(f"Bad schedule: '{schedule_str}' for '{job_id}'.")
            if trigger: logging.info(f"Scheduling '{job_id}': {trigger}.")
            else: log_queue.put(f"WARNING: No valid trigger for '{job_id}', schedule '{schedule_str}'.")
        except Exception as e:
            logging.error(f"Error parsing schedule '{schedule_str}' for '{job_id}': {e}")
            log_queue.put(f"ERROR: Invalid schedule '{schedule_str}' for {job_id}.")
        if trigger:
            scheduler.add_job(schedule_trigger_backup, trigger, id=job_id, name=job_id,
                              args=[job_details, global_settings, log_queue],
                              replace_existing=True, misfire_grace_time=3600)
            log_queue.put(f"Job '{job_id}' scheduled.")

    def load_all_jobs_to_scheduler(config, log_queue):
        if not scheduler: return
        log_queue.put("Loading jobs into scheduler...")
        if 'backup_jobs' in config and 'global_settings' in config:
            for job in config['backup_jobs']:
                parse_and_add_job_to_scheduler(job, config['global_settings'], log_queue)
        log_stream = io.StringIO()
        scheduler.print_jobs(out=log_stream)
        logging.info(f"APScheduler jobs:\n{log_stream.getvalue()}")
        log_stream.close()


# ==============================================================================
# 5. SYSTEM TRAY ICON (Unchanged)
# ==============================================================================
def create_image(width, height, color1, color2):
    if not TRAY_AVAILABLE: return None
    image = Image.new('RGB', (width, height), color1)
    dc = ImageDraw.Draw(image)
    dc.rectangle((width // 2, 0, width, height // 2), fill=color2)
    dc.rectangle((0, height // 2, width // 2, height), fill=color2)
    return image

# ==============================================================================
# 6. TKINTER GUI CLASSES (Themed)
# ==============================================================================

class JobEditorWindow(tk.Toplevel):
    def __init__(self, parent, job_data=None, original_job_name=None):
        super().__init__(parent)
        self.parent = parent; self.job_data_to_edit = job_data; self.original_job_name = original_job_name
        self.title("Add/Edit Backup Job"); self.geometry("650x600"); self.transient(parent); self.grab_set()
        
        theme = current_theme_colors
        self.configure(bg=theme["BG_COLOR"])

        self.job_name_var = tk.StringVar(); self.source_dir_var = tk.StringVar(); self.dest_base_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar(value=True); self.schedule_var = tk.StringVar(value="manual")
        self.volumes_override_var = tk.IntVar(value=0)
        main_frame = ttk.Frame(self, padding="15"); main_frame.pack(fill=tk.BOTH, expand=True)

        row_num = 0; pady_val = 6; padx_val = 5

        ttk.Label(main_frame, text="Job Name:").grid(row=row_num, column=0, sticky=tk.W, pady=pady_val)
        self.name_entry = ttk.Entry(main_frame, textvariable=self.job_name_var, width=60)
        self.name_entry.grid(row=row_num, column=1, columnspan=2, sticky=tk.EW, pady=pady_val, padx=padx_val); row_num += 1

        ttk.Label(main_frame, text="Source Directory:").grid(row=row_num, column=0, sticky=tk.W, pady=pady_val)
        self.source_entry = ttk.Entry(main_frame, textvariable=self.source_dir_var, width=50)
        self.source_entry.grid(row=row_num, column=1, sticky=tk.EW, pady=pady_val, padx=padx_val)
        ttk.Button(main_frame, text="Browse...", command=self._browse_source).grid(row=row_num, column=2, sticky=tk.W, padx=padx_val); row_num += 1

        ttk.Label(main_frame, text="Destination Base:").grid(row=row_num, column=0, sticky=tk.W, pady=pady_val)
        self.dest_entry = ttk.Entry(main_frame, textvariable=self.dest_base_var, width=50)
        self.dest_entry.grid(row=row_num, column=1, sticky=tk.EW, pady=pady_val, padx=padx_val)
        ttk.Button(main_frame, text="Browse...", command=self._browse_dest).grid(row=row_num, column=2, sticky=tk.W, padx=padx_val); row_num += 1

        ttk.Label(main_frame, text="Exclusions (one per line):").grid(row=row_num, column=0, sticky=tk.NW, pady=pady_val)
        text_frame = ttk.Frame(main_frame, relief=tk.SOLID, borderwidth=1, style='Content.TFrame')
        self.exclusions_text = tk.Text(text_frame, height=8, width=60, relief=tk.FLAT, borderwidth=0, font=('Segoe UI', 9),
                                       highlightthickness=0, bd=0,
                                       bg=theme["LOG_BG"], fg=theme["LOG_FG"], insertbackground=theme["TEXT_INSERT"])
        excl_scrollbar = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=self.exclusions_text.yview)
        self.exclusions_text.config(yscrollcommand=excl_scrollbar.set)
        excl_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.exclusions_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2, pady=2)
        text_frame.grid(row=row_num, column=1, columnspan=2, sticky=tk.NSEW, pady=pady_val, padx=padx_val)
        row_num += 1

        ttk.Label(main_frame, text="Backups to Keep:").grid(row=row_num, column=0, sticky=tk.W, pady=pady_val)
        self.volumes_override_spinbox = ttk.Spinbox(main_frame, from_=0, to=99, textvariable=self.volumes_override_var, width=8)
        self.volumes_override_spinbox.grid(row=row_num, column=1, sticky=tk.W, pady=pady_val, padx=padx_val)
        ttk.Label(main_frame, text="(0 = use global default)").grid(row=row_num, column=2, sticky=tk.W, padx=padx_val, pady=pady_val); row_num += 1

        self.enabled_check = ttk.Checkbutton(main_frame, text="Enabled", variable=self.enabled_var)
        self.enabled_check.grid(row=row_num, column=1, sticky=tk.W, pady=pady_val, padx=padx_val); row_num += 1

        ttk.Label(main_frame, text="Schedule:").grid(row=row_num, column=0, sticky=tk.W, pady=pady_val)
        self.schedule_entry = ttk.Entry(main_frame, textvariable=self.schedule_var, width=30)
        self.schedule_entry.grid(row=row_num, column=1, columnspan=2, sticky=tk.EW, pady=pady_val, padx=padx_val); row_num += 1

        ttk.Separator(main_frame, orient=tk.HORIZONTAL).grid(row=row_num, column=0, columnspan=3, pady=(15, 10), sticky=tk.EW); row_num += 1

        buttons_frame = ttk.Frame(main_frame); buttons_frame.grid(row=row_num, column=0, columnspan=3, pady=10, sticky=tk.E)
        ttk.Button(buttons_frame, text="Save", command=self._save_job).pack(side=tk.RIGHT, padx=5)
        ttk.Button(buttons_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=5)

        if self.job_data_to_edit: self._populate_fields()
        main_frame.columnconfigure(1, weight=1); main_frame.rowconfigure(3, weight=1)

    def _populate_fields(self): # Unchanged
        self.job_name_var.set(self.job_data_to_edit.get("name", ""))
        self.source_dir_var.set(self.job_data_to_edit.get("source_dir", ""))
        self.dest_base_var.set(self.job_data_to_edit.get("destination_base", ""))
        self.enabled_var.set(self.job_data_to_edit.get("enabled", True))
        self.schedule_var.set(self.job_data_to_edit.get("schedule", "manual"))
        self.volumes_override_var.set(self.job_data_to_edit.get("volumes_to_keep_override", 0))
        self.exclusions_text.delete("1.0", tk.END)
        self.exclusions_text.insert("1.0", "\n".join(self.job_data_to_edit.get("exclusions", [])))

    def _browse_source(self): # Unchanged
        d = filedialog.askdirectory(title="Select Source Directory", parent=self)
        if d: self.source_dir_var.set(d)

    def _browse_dest(self): # Unchanged
        d = filedialog.askdirectory(title="Select Destination Base Directory", parent=self)
        if d: self.dest_base_var.set(d)

    def _save_job(self): # Unchanged
        job_name = self.job_name_var.get().strip(); source_dir = self.source_dir_var.get().strip(); dest_base = self.dest_base_var.get().strip()
        if not all([job_name, source_dir, dest_base]): messagebox.showerror("Error", "Name, Source, & Dest cannot be empty.", parent=self); return
        is_new = self.job_data_to_edit is None; name_changed = not is_new and self.original_job_name != job_name
        if (is_new or name_changed) and any(j['name'] == job_name for j in current_config['backup_jobs']):
            messagebox.showerror("Error", f"Job name '{job_name}' already exists.", parent=self); return

        exclusions = [ln.strip() for ln in self.exclusions_text.get("1.0",tk.END).strip().splitlines() if ln.strip()]
        details = {"name":job_name, "source_dir":source_dir, "destination_base":dest_base, "exclusions":exclusions,
                   "enabled":self.enabled_var.get(), "schedule":self.schedule_var.get().strip() or "manual"}

        try:
            volumes_override_val = self.volumes_override_var.get()
            if volumes_override_val < 0:
                messagebox.showerror("Validation Error", "Job-specific volumes to keep cannot be negative.", parent=self); return
            if volumes_override_val > 0:
                details["volumes_to_keep_override"] = volumes_override_val
        except tk.TclError:
            messagebox.showerror("Validation Error", "Job-specific volumes to keep must be a whole number.", parent=self); return

        if self.job_data_to_edit:
            current_config['backup_jobs']=[details if j['name']==self.original_job_name else j for j in current_config['backup_jobs']]
        else: current_config['backup_jobs'].append(details)

        if save_config(current_config):
            if APS_AVAILABLE: parse_and_add_job_to_scheduler(details, current_config['global_settings'], main_app_ref.log_queue)
            messagebox.showinfo("Success", "Job saved.", parent=self)
            if main_app_ref: main_app_ref.populate_job_list()
            self.destroy()
        else: messagebox.showerror("Error", "Failed to save config.", parent=self)

class SettingsWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent=parent; self.title("Global Settings"); self.geometry("500x320"); self.transient(parent); self.grab_set()
        
        theme = current_theme_colors
        self.configure(bg=theme["BG_COLOR"])

        self.volumes_var=tk.IntVar(); self.base_name_var=tk.StringVar(); self.start_with_windows_var=tk.BooleanVar()
        self.theme_var = tk.StringVar()
        self.start_minimized_var = tk.BooleanVar()

        main_frame = ttk.Frame(self, padding="20"); main_frame.pack(fill=tk.BOTH, expand=True)
        row_num = 0; pady_val = 8; padx_val = 5

        ttk.Label(main_frame,text="Default Volumes to Keep:").grid(row=row_num,column=0,sticky=tk.W,pady=pady_val)
        self.volumes_spinbox = ttk.Spinbox(main_frame,from_=1,to=100,textvariable=self.volumes_var,width=10)
        self.volumes_spinbox.grid(row=row_num,column=1,sticky=tk.W,pady=pady_val, padx=padx_val); row_num+=1

        ttk.Label(main_frame,text="Default Backup Base Name:").grid(row=row_num,column=0,sticky=tk.W,pady=pady_val)
        self.base_name_entry = ttk.Entry(main_frame,textvariable=self.base_name_var,width=35)
        self.base_name_entry.grid(row=row_num,column=1,sticky=tk.EW,pady=pady_val, padx=padx_val); row_num+=1

        ttk.Label(main_frame,text="Application Theme:").grid(row=row_num,column=0,sticky=tk.W,pady=pady_val)
        self.theme_combo = ttk.Combobox(main_frame, textvariable=self.theme_var, values=["System"] + list(THEMES.keys()), state="readonly", width=33)
        self.theme_combo.grid(row=row_num,column=1,sticky=tk.EW,pady=pady_val, padx=padx_val); row_num+=1

        self.start_with_windows_check = ttk.Checkbutton(main_frame,text="Start application with System",variable=self.start_with_windows_var)
        self.start_with_windows_check.grid(row=row_num,column=0,columnspan=2,sticky=tk.W,pady=15); row_num+=1

        self.start_minimized_check = ttk.Checkbutton(main_frame,text="Start minimized to system tray",variable=self.start_minimized_var)
        self.start_minimized_check.grid(row=row_num,column=0,columnspan=2,sticky=tk.W,pady=15); row_num+=1

        ttk.Separator(main_frame, orient=tk.HORIZONTAL).grid(row=row_num, column=0, columnspan=2, pady=(15, 10), sticky=tk.EW); row_num += 1

        buttons_frame = ttk.Frame(main_frame); buttons_frame.grid(row=row_num,column=0,columnspan=2,pady=15, sticky=tk.E)
        ttk.Button(buttons_frame,text="Save",command=self._save_settings).pack(side=tk.RIGHT,padx=5)
        ttk.Button(buttons_frame,text="Cancel",command=self.destroy).pack(side=tk.RIGHT,padx=5)

        self._populate_fields(); main_frame.columnconfigure(1,weight=1)

    def _populate_fields(self):
        settings = current_config.get('global_settings',{})
        self.volumes_var.set(settings.get("default_volumes_to_keep",3))
        self.base_name_var.set(settings.get("default_backup_base_name","Backups_Py"))
        self.start_with_windows_var.set(check_if_in_startup())
        self.theme_var.set(settings.get("theme", "Light (Default)"))
        self.start_minimized_var.set(settings.get("start_minimized", False))

    def _save_settings(self):
        try: volumes = self.volumes_var.get(); assert volumes >= 1
        except: messagebox.showerror("Error","Volumes must be >= 1.",parent=self); return
        base_name = self.base_name_var.get().strip()
        if not base_name: messagebox.showerror("Error","Base Name empty.",parent=self); return

        current_config['global_settings']['default_volumes_to_keep']=volumes
        current_config['global_settings']['default_backup_base_name']=base_name
        current_config['global_settings']['start_minimized'] = self.start_minimized_var.get()
        
        selected_theme = self.theme_var.get()
        current_theme = current_config['global_settings'].get("theme", "Light (Default)")
        current_config['global_settings']['theme'] = selected_theme

        current_startup = check_if_in_startup(); desired_startup = self.start_with_windows_var.get()
        startup_ok = True
        if desired_startup and not current_startup:
            if not add_to_startup(): startup_ok=False; messagebox.showerror("Error","Failed to add to startup.",parent=self); self.start_with_windows_var.set(False)
        elif not desired_startup and current_startup:
            if not remove_from_startup(): startup_ok=False; messagebox.showerror("Error","Failed to remove from startup.",parent=self); self.start_with_windows_var.set(True)
            
        if save_config(current_config) and startup_ok:
            messagebox.showinfo("Success","Settings saved.",parent=self)
            if main_app_ref:
                main_app_ref.log_message_gui("Global settings updated.")
                if current_theme != selected_theme:
                    logging.info(f"SettingsWindow: Switching theme from '{current_theme}' to '{selected_theme}'")
                    main_app_ref.apply_theme(selected_theme)
            self.destroy()
        elif not startup_ok: pass
        else: messagebox.showerror("Error","Failed to save config file.",parent=self)


# ==============================================================================
# 6.5 BACKUP QUEUE MANAGER
# ==============================================================================
class BackupQueueManager:
    def __init__(self):
        self.job_queue = queue.Queue()
        self.is_running = False
        self.lock = threading.Lock()

    def add_job(self, job_details, global_settings, log_queue):
        self.job_queue.put((job_details, global_settings, log_queue))
        log_queue.put(f"Queue Manager: Job '{job_details['name']}' added to queue.")
        self.process_queue()

    def process_queue(self):
        with self.lock:
            if self.is_running:
                return
            
            if self.job_queue.empty():
                return

            self.is_running = True
            
        # Get next job
        try:
            job_data = self.job_queue.get_nowait()
        except queue.Empty:
            with self.lock:
                self.is_running = False
            return

        # Run in a separate thread so we don't block the GUI or the caller
        threading.Thread(target=self._run_job_wrapper, args=job_data, daemon=True).start()

    def _run_job_wrapper(self, job_details, global_settings, log_queue):
        try:
            run_backup_job(job_details, global_settings, log_queue)
        except Exception as e:
            log_queue.put(f"Queue Manager Error running job {job_details['name']}: {e}")
        finally:
            with self.lock:
                self.is_running = False
            # Trigger next job
            self.process_queue()

# ==============================================================================
# 6.6 QUEUE SELECTION WINDOW
# ==============================================================================
class QueueSelectionWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("Run Queue Selection")
        self.geometry("400x500")
        self.transient(parent)
        self.grab_set()
        
        theme = current_theme_colors
        self.configure(bg=theme["BG_COLOR"])
        
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(main_frame, text="Select Backups to Queue:", style="Header.TLabel").pack(pady=(0, 10), anchor=tk.W)
        
        # Scrollable frame for checkboxes
        canvas_frame = ttk.Frame(main_frame, style='Content.TFrame')
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.canvas = tk.Canvas(canvas_frame, bg=theme["LIST_BG"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas, style='TFrame')
        
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        self.check_vars = {}
        self.jobs = [j for j in current_config['backup_jobs'] if j.get('enabled', False)]
        
        if not self.jobs:
            ttk.Label(self.scrollable_frame, text="No enabled jobs found.").pack(pady=10, padx=10)
        else:
            for job in self.jobs:
                var = tk.BooleanVar(value=False)
                self.check_vars[job['name']] = var
                cb = ttk.Checkbutton(self.scrollable_frame, text=job['name'], variable=var)
                cb.pack(anchor=tk.W, padx=5, pady=2)
                
        # Buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        
        ttk.Button(btn_frame, text="Run Selected", command=self._run_selected).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Select All", command=self._select_all).pack(side=tk.LEFT, padx=5)

    def _select_all(self):
        for var in self.check_vars.values():
            var.set(True)

    def _run_selected(self):
        selected_jobs = [j for j in self.jobs if self.check_vars[j['name']].get()]
        if not selected_jobs:
            messagebox.showwarning("Warning", "No jobs selected.", parent=self)
            return
            
        if main_app_ref:
            main_app_ref.log_message_gui(f"Queueing {len(selected_jobs)} selected jobs...")
            for job in selected_jobs:
                main_app_ref.log_message_gui(f"Queueing: {job['name']}")
                main_app_ref.queue_manager.add_job(job, current_config['global_settings'], main_app_ref.log_queue)
        
        self.destroy()

# ==============================================================================
# 6.7 VAULT MERGE WINDOW (Phase 4)
# ==============================================================================
class VaultMergeWindow(tk.Toplevel):
    def __init__(self, parent, vault, snapshot_name):
        super().__init__(parent.root)
        self.parent = parent
        self.vault = vault
        self.snapshot_name = snapshot_name
        display = vault.get_commit_display_name(snapshot_name)
        self.title(f"Compare: Project VS {display}")
        self.geometry("900x600")
        self.transient(parent.root)
        self.grab_set()
        
        self.temp_dir = None
        self.diffs = []

        theme = current_theme_colors
        self.configure(bg=theme["BG_COLOR"])
        
        self.create_widgets()
        self.load_diffs()
        
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def create_widgets(self):
        # Paned Window for Tree + Preview
        self.paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Left: Treeview
        tree_frame = ttk.Frame(self.paned)
        columns = ("file", "status", "size")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show='headings', selectmode='browse')
        self.tree.heading("file", text="File")
        self.tree.heading("status", text="Status")
        self.tree.heading("size", text="Size Diff")
        
        self.tree.column("file", width=300)
        self.tree.column("status", width=80)
        self.tree.column("size", width=80)
        
        self.tree.tag_configure('NEW', foreground='green')
        self.tree.tag_configure('MODIFIED', foreground='#dcae00')
        self.tree.tag_configure('MISSING', foreground='red')
        
        self.tree.bind('<<TreeviewSelect>>', self.on_select)

        scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.paned.add(tree_frame, weight=2)
        
        # Right: Preview Frame (Phase 7)
        self.preview_frame = ttk.LabelFrame(self.paned, text="Preview", padding=10)
        self.paned.add(self.preview_frame, weight=1)
        
        self.preview_label = ttk.Label(self.preview_frame, text="Select a file to preview.")
        self.preview_label.pack(fill=tk.BOTH, expand=True)
        
        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        
        ttk.Button(btn_frame, text="Cancel / Close", command=self.on_close).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Merge Selected (Overwrite)", command=self.do_merge).pack(side=tk.RIGHT, padx=5)
        
    def load_diffs(self):
        self.parent.vault_log("Compare: Calculating diffs...")
        self.update()
        try:
            self.diffs, self.temp_dir = self.vault.compare_snapshot(self.snapshot_name, progress_callback=self.parent.vault_log)
            new_count = sum(1 for d in self.diffs if d['status'] == 'NEW')
            mod_count = sum(1 for d in self.diffs if d['status'] == 'MODIFIED')
            miss_count = sum(1 for d in self.diffs if d['status'] == 'MISSING')
            self.parent.vault_log(f"Diff results: {new_count} new, {mod_count} modified, {miss_count} missing")
            for item in self.diffs:
                self.tree.insert('', tk.END, values=(item['file'], item['status'], parse_bytes(item['size_diff'])), tags=(item['status'],))
            if not self.diffs:
                 messagebox.showinfo("No Changes", "Snapshots are identical.")
        except Exception as e:
            self.parent.vault_log(f"Compare FAILED: {e}")
            messagebox.showerror("Error", f"Diff failed: {e}")
            self.on_close()

    def on_select(self, event):
        sel = self.tree.selection()
        if not sel: return
        vals = self.tree.item(sel[0])['values']
        fname = vals[0]
        
        # Find item
        item = next((d for d in self.diffs if d['file'] == fname), None)
        if not item: return
        
        # Show preview
        self.show_preview(item)

    def show_preview(self, item):
        # Clear old
        for widget in self.preview_frame.winfo_children():
            widget.destroy()
            
        if item['status'] == 'MISSING' or not item['temp_path']:
             ttk.Label(self.preview_frame, text="(File missing in snapshot)").pack(fill=tk.BOTH, expand=True)
             return

        fpath = item['temp_path']
        try:
            # Check extension
            ext = os.path.splitext(fpath)[1].lower()
            if ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico']:
                # Image
                img = Image.open(fpath)
                # Resize to fit
                w = self.preview_frame.winfo_width() - 20
                h = self.preview_frame.winfo_height() - 20
                if w > 0 and h > 0:
                    img.thumbnail((w, h))
                
                photo = ImageTk.PhotoImage(img)
                lbl = ttk.Label(self.preview_frame, image=photo)
                lbl.image = photo # Keep ref
                lbl.pack(anchor=tk.CENTER, expand=True)
                
            elif ext in ['.txt', '.json', '.xml', '.py', '.md', '.log', '.ini']:
                # Text
                text_widget = tk.Text(self.preview_frame, wrap=tk.WORD, width=1, height=1)
                text_widget.pack(fill=tk.BOTH, expand=True)
                with open(fpath, 'r', errors='replace') as f:
                    content = f.read(2000) # Limit
                    text_widget.insert("1.0", content)
                text_widget.config(state=tk.DISABLED)
            else:
                 ttk.Label(self.preview_frame, text=f"(Preview not available for {ext})").pack(fill=tk.BOTH, expand=True)
        except Exception as e:
             ttk.Label(self.preview_frame, text=f"Error previewing file: {e}").pack(fill=tk.BOTH, expand=True)

    def do_merge(self):
        selected_items = self.tree.selection()
        if not selected_items: return
        
        to_merge = []
        for iid in selected_items:
            vals = self.tree.item(iid)['values']
            fname = vals[0]
            for d in self.diffs:
                if d['file'] == fname:
                    to_merge.append(d); break
        
        if not to_merge: return
        
        if messagebox.askyesno("Confirm Merge", f"Overwrite {len(to_merge)} files in your active project?"):
            try:
                self.parent.vault_log(f"Merging {len(to_merge)} file(s)...")
                self.vault.apply_merge(to_merge, None)
                self.parent.vault_log(f"Merge complete: {len(to_merge)} file(s) applied")
                messagebox.showinfo("Success", "Files merged.")
                self.on_close()
            except Exception as e:
                 self.parent.vault_log(f"Merge FAILED: {e}")
                 messagebox.showerror("Error", f"Merge failed: {e}")

    def on_close(self):
        if self.temp_dir and os.path.exists(self.temp_dir):
            try: shutil.rmtree(self.temp_dir)
            except: pass
        self.destroy()

def parse_bytes(size):
    return f"{size} B"

# ==============================================================================
# 7. MAIN APPLICATION CLASS (Themed)
# ==============================================================================
class BackupApp:
    def __init__(self, root, start_minimized=False):
        self.root = root
        self.root.title("Solace Backup - v1.7 (Final)")
        self.root.geometry("1000x650")
        
        # Store start_minimized flag for later use
        self._start_minimized = start_minimized

        global main_app_ref
        main_app_ref = self

        self.log_queue = queue.Queue()
        self.queue_manager = BackupQueueManager()
        self.tray_icon = None
        self.current_vault = None # Initialize Project Vault

        global current_config
        current_config = load_config()
        initial_theme = current_config.get("global_settings", {}).get("theme", "Light (Default)")

        self.create_widgets()
        self.apply_theme(initial_theme)
        self.set_window_icon()
        self.populate_job_list()
        self.root.after(100, self.process_log_queue)

        if APS_AVAILABLE and scheduler:
            try:
                load_all_jobs_to_scheduler(current_config, self.log_queue)
                scheduler.start()
                self.log_message_gui("Scheduler started.")
            except Exception as e:
                self.log_message_gui(f"ERROR starting scheduler: {e}")
        else:
            self.log_message_gui("Scheduler disabled.")

        if TRAY_AVAILABLE:
            self.setup_tray_icon()
            self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
            
            # If --tray or --minimized flag was passed, start minimized to tray
            if self._start_minimized:
                self.root.after(200, self.hide_window)  # Small delay to ensure tray is ready
        else:
            self.root.protocol("WM_DELETE_WINDOW", self.quit_application)
            self.log_message_gui("System Tray disabled.")

            # Even without tray, if --minimized was passed, hide the window
            if self._start_minimized:
                self.root.withdraw()

        self.log_message_gui("Application initialized.")

        # Set up signal handlers for Waybar integration
        self._setup_signal_handlers()
        self._update_status_file("IDLE", "Application ready")




    def create_widgets(self):
        self.main_frame = ttk.Frame(self.root, padding="10")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # --- TABBED INTERFACE ---
        self.notebook = ttk.Notebook(self.main_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: Automated Backups (Existing UI)
        self.tab_backup = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_backup, text="Automated Backups")

        # Tab 2: Project Vault (New)
        self.tab_vault = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_vault, text="Project Vault")

        self.create_vault_widgets()

        # --- Re-parent existing widgets to self.tab_backup ---
        self.paned_window = ttk.PanedWindow(self.tab_backup, orient=tk.HORIZONTAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # Left Frame (Job List) - Increased width
        left_outer_frame = ttk.Frame(self.paned_window, width=350) 
        self.left_frame = ttk.Frame(left_outer_frame, style='Content.TFrame', padding="10")
        
        # --- LOGO (PNG with Tinting) - MOVED TO LEFT FRAME ---
        try:
            # Path to the logo file
            logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "title_art", "logo.png")
            
            if os.path.exists(logo_path) and TRAY_AVAILABLE:
                # Load the image directly without tinting
                self.original_logo_image = Image.open(logo_path)
                self.logo_photo = ImageTk.PhotoImage(self.original_logo_image)
                
                self.logo_label = tk.Label(
                    self.left_frame, # Changed parent to left_frame
                    image=self.logo_photo,
                    bg=current_theme_colors["BG_COLOR"],
                    bd=0
                )
                self.logo_label.pack(side=tk.TOP, pady=(0, 10)) # Adjusted padding
                
                # Bind resize event for responsive scaling
                self.resize_timer = None
                self.root.bind('<Configure>', self.on_window_resize)
                # Trigger initial resize
                self.root.after(100, self.update_logo_size)
            else:
                raise FileNotFoundError("Logo file not found or PIL not available")
                
        except Exception as e:
            logging.error(f"Error loading logo: {e}")
            # Fallback to simple text if logo fails
            self.title_label = ttk.Label(
                self.left_frame, # Changed parent
                text="Solace Backup Suite", 
                font=("Segoe UI", 24, "bold"),
                anchor=tk.CENTER
            )
            self.title_label.pack(side=tk.TOP, fill=tk.X, pady=(0, 10))
        
        # ----------------------------------

        self.job_header_label = ttk.Label(self.left_frame, text="Backup Jobs", style="Header.TLabel", anchor=tk.CENTER)
        self.job_header_label.pack(pady=(0, 10), fill=tk.X)

        global job_listbox
        list_frame = tk.Frame(self.left_frame) # CHANGED: Was ttk.Frame
        job_listbox = tk.Listbox(list_frame, selectmode=tk.SINGLE, exportselection=False,
                                 bd=0, relief=tk.FLAT, font=('Segoe UI', 10),
                                 highlightthickness=0)
        list_scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=job_listbox.yview)
        job_listbox.config(yscrollcommand=list_scrollbar.set)
        list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        job_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.list_frame = list_frame
        self.list_scrollbar = list_scrollbar

        self.job_buttons_frame = ttk.Frame(self.left_frame)
        self.job_buttons_frame.pack(pady=(10, 0), fill=tk.X, anchor=tk.CENTER)
        btn_add = ttk.Button(self.job_buttons_frame, text="Add", command=self.open_add_job_window)
        btn_remove = ttk.Button(self.job_buttons_frame, text="Remove", command=self.remove_backup_job)
        btn_edit = ttk.Button(self.job_buttons_frame, text="Edit", command=self.open_edit_job_window)
        btn_add.pack(side=tk.LEFT, padx=5, expand=True)
        btn_remove.pack(side=tk.LEFT, padx=5, expand=True)
        btn_edit.pack(side=tk.LEFT, padx=5, expand=True)

        self.left_frame.pack(fill=tk.BOTH, expand=True, padx=(0, 5), pady=0)
        left_outer_frame.pack_propagate(False)

        # Right Frame (Logs)
        right_outer_frame = ttk.Frame(self.paned_window, width=650) 
        self.right_frame = ttk.Frame(right_outer_frame, style='Content.TFrame', padding="10")
        self.log_header_label = ttk.Label(self.right_frame, text="Status / Logs", style="Header.TLabel", anchor=tk.CENTER)
        self.log_header_label.pack(pady=(0, 10), fill=tk.X)

        global log_text_widget
        log_frame = tk.Frame(self.right_frame) # CHANGED: Was ttk.Frame
        log_text_widget = tk.Text(log_frame, wrap=tk.WORD, state=tk.DISABLED,
                                  bd=0, relief=tk.FLAT, font=('Consolas', 9),
                                  highlightthickness=0)
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_text_widget.yview)
        log_text_widget.config(yscrollcommand=log_scrollbar.set)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        log_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_frame = log_frame
        self.log_scrollbar = log_scrollbar

        self.right_frame.pack(fill=tk.BOTH, expand=True, padx=(5, 0), pady=0)
        right_outer_frame.pack_propagate(False)

        self.paned_window.add(left_outer_frame)
        self.paned_window.add(right_outer_frame)
        self.paned_window.pane(left_outer_frame, weight=1)
        self.paned_window.pane(right_outer_frame, weight=2)

        # Status Frame
        self.status_frame = ttk.Frame(self.tab_backup, padding=(5, 2))
        self.status_frame.pack(fill=tk.X)
        self.status_label = ttk.Label(self.status_frame, text="Status: Idle", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, padx=5, pady=2, fill=tk.X, expand=True)
        self.progress_bar = ttk.Progressbar(self.status_frame, orient='horizontal', length=250, mode='determinate', style='Horizontal.TProgressbar')
        self.progress_bar.pack(side=tk.RIGHT, padx=5, pady=2)

        # Bottom Frame
        self.bottom_separator = ttk.Separator(self.tab_backup, orient=tk.HORIZONTAL)
        self.bottom_separator.pack(fill=tk.X, pady=5)
        self.bottom_frame = ttk.Frame(self.tab_backup)
        self.bottom_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(self.bottom_frame, text="Run Selected", command=self.run_selected_backup).pack(side=tk.LEFT, padx=(5, 5))
        ttk.Button(self.bottom_frame, text="Run Queue...", command=self.open_queue_selection_window).pack(side=tk.LEFT, padx=5)
        ttk.Button(self.bottom_frame, text="Run All", command=self.run_all_backups).pack(side=tk.LEFT, padx=5)
        ttk.Button(self.bottom_frame, text="Settings", command=self.open_settings).pack(side=tk.RIGHT, padx=5)
        ttk.Button(self.bottom_frame, text="View Log File", command=self.view_log_file).pack(side=tk.RIGHT, padx=5)


    def create_vault_widgets(self):
        """Constructs the UI for the Project Vault tab."""
        # Top Control Bar
        control_frame = ttk.LabelFrame(self.tab_vault, text="Project Control", padding=10)
        control_frame.pack(fill=tk.X, pady=(0, 10))

        self.vault_path_var = tk.StringVar(value="No Project Loaded")
        ttk.Label(control_frame, text="Path:").pack(side=tk.LEFT)
        ttk.Label(control_frame, textvariable=self.vault_path_var, font=('Segoe UI', 9, 'italic')).pack(side=tk.LEFT, padx=5, expand=True, fill=tk.X)
        ttk.Button(control_frame, text="Load / Init Project...", command=self.open_vault_project).pack(side=tk.RIGHT)

        # Branch Info
        branch_frame = ttk.LabelFrame(self.tab_vault, text="Branch Management", padding=10)
        branch_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(branch_frame, text="Current Branch:").pack(side=tk.LEFT)
        self.vault_branch_var = tk.StringVar(value="--")
        ttk.Label(branch_frame, textvariable=self.vault_branch_var, font=('Segoe UI', 9, 'bold')).pack(side=tk.LEFT, padx=5)
        
        ttk.Button(branch_frame, text="New Branch...", command=self.create_vault_branch).pack(side=tk.RIGHT)
        ttk.Button(branch_frame, text="Export Branch...", command=self.export_vault_branch).pack(side=tk.RIGHT, padx=5)
        ttk.Button(branch_frame, text="Delete Branch...", command=self.delete_vault_branch).pack(side=tk.RIGHT, padx=5)
        # Placeholder for Switch Branch combobox if needed

        # Vertical PanedWindow for snapshot browser + vault log
        self.vault_paned = ttk.PanedWindow(self.tab_vault, orient=tk.VERTICAL)
        self.vault_paned.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        # History / Snapshots (top pane)
        history_frame = ttk.LabelFrame(self.vault_paned, text="Branch & Snapshot Browser", padding=10, style="Snapshot.TLabelframe")
        
        # Paned Window for Split View
        h_paned = ttk.PanedWindow(history_frame, orient=tk.HORIZONTAL)
        h_paned.pack(fill=tk.BOTH, expand=True)
        
        # Left: Branch Visualizer
        viz_frame = ttk.Frame(h_paned)
        self.branch_viz = BranchTreeVisualizer(viz_frame, self.current_vault, on_click_callback=self.on_tree_node_click, bg="white")
        
        # Scrollbars for Visualizer
        v_scroll_viz = ttk.Scrollbar(viz_frame, orient=tk.VERTICAL, command=self.branch_viz.yview)
        h_scroll_viz = ttk.Scrollbar(viz_frame, orient=tk.HORIZONTAL, command=self.branch_viz.xview)
        self.branch_viz.configure(yscrollcommand=v_scroll_viz.set, xscrollcommand=h_scroll_viz.set)
        
        self.branch_viz.grid(row=0, column=0, sticky="nsew")
        v_scroll_viz.grid(row=0, column=1, sticky="ns")
        h_scroll_viz.grid(row=1, column=0, sticky="ew")
        viz_frame.grid_columnconfigure(0, weight=1)
        viz_frame.grid_rowconfigure(0, weight=1)
        
        # Right: Traditional List (Details)
        list_frame = ttk.Frame(h_paned)
        self.vault_history_list = tk.Listbox(list_frame, font=('Consolas', 10), activestyle='none')
        scroll_list = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.vault_history_list.yview)
        self.vault_history_list.configure(yscrollcommand=scroll_list.set)
        
        self.vault_history_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_list.pack(side=tk.RIGHT, fill=tk.Y)

        h_paned.add(viz_frame, weight=3)
        h_paned.add(list_frame, weight=1)

        # Vault Activity Log (bottom pane)
        vault_log_frame = ttk.LabelFrame(self.vault_paned, text="Vault Activity Log", padding=5, style="Snapshot.TLabelframe")
        vault_log_inner = tk.Frame(vault_log_frame)
        self.vault_log_text = tk.Text(vault_log_inner, wrap=tk.WORD, state=tk.DISABLED,
                                      bd=0, relief=tk.FLAT, font=('Consolas', 9),
                                      highlightthickness=0, height=6)
        vault_log_scroll = ttk.Scrollbar(vault_log_inner, orient=tk.VERTICAL, command=self.vault_log_text.yview)
        self.vault_log_text.config(yscrollcommand=vault_log_scroll.set)
        vault_log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.vault_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vault_log_inner.pack(fill=tk.BOTH, expand=True)
        self.vault_log_inner_frame = vault_log_inner

        self.vault_paned.add(history_frame, weight=3)
        self.vault_paned.add(vault_log_frame, weight=1)

        # Actions
        action_frame = ttk.LabelFrame(self.tab_vault, text="Actions", padding=10)
        action_frame.pack(fill=tk.X)

        ttk.Label(action_frame, text="Commit Message:").pack(side=tk.LEFT)
        self.vault_commit_msg = ttk.Entry(action_frame, width=40)
        self.vault_commit_msg.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        self.btn_commit = ttk.Button(action_frame, text="Commit Snapshot", command=self.perform_vault_commit, state=tk.DISABLED)
        self.btn_commit.pack(side=tk.LEFT, padx=5)
        
        # Restore / Compare
        self.btn_compare = ttk.Button(action_frame, text="Compare/Merge...", state=tk.DISABLED, command=self.open_compare_window)
        self.btn_compare.pack(side=tk.RIGHT, padx=5)
        
        self.btn_checkout = ttk.Button(action_frame, text="Full Restore...", state=tk.DISABLED, command=self.perform_full_restore)
        self.btn_checkout.pack(side=tk.RIGHT, padx=5)

        self.btn_explore = ttk.Button(action_frame, text="Explore...", state=tk.DISABLED, command=self.explore_snapshot)
        self.btn_explore.pack(side=tk.RIGHT, padx=5)

    def open_vault_project(self):
        path = filedialog.askdirectory(title="Select Project Folder", parent=self.root)
        if not path: return

        try:
            self.vault_log(f"Loading project: {path}")
            self.current_vault = ProjectVault(path)
            self.vault_path_var.set(path)

            # Update visualizer
            if hasattr(self, 'branch_viz'):
                self.branch_viz.vault = self.current_vault

            meta = self.current_vault._load_meta()
            branch_count = len(meta.get('branches', []))
            current_branch = meta.get('current_branch', 'unknown')
            commits = self.current_vault.get_commits(include_inherited=True)
            self.vault_log(f"Vault initialized: {branch_count} branch(es), {len(commits)} commit(s)")
            self.vault_log(f"Active branch: {current_branch}")

            self.refresh_vault_ui()
            self.btn_commit.configure(state=tk.NORMAL)
            self.btn_compare.configure(state=tk.NORMAL)
            self.btn_checkout.configure(state=tk.NORMAL)
            self.btn_explore.configure(state=tk.NORMAL)
        except Exception as e:
            self.vault_log(f"ERROR: Failed to load vault: {e}")
            messagebox.showerror("Error", f"Failed to load vault: {e}", parent=self.root)

    def refresh_vault_ui(self):
        if not self.current_vault: return

        meta = self.current_vault._load_meta()
        current_branch = meta.get('current_branch', 'unknown')
        self.vault_branch_var.set(current_branch)

        # Load history with display names — only show commits for the current branch
        commits = self.current_vault.get_commits(include_inherited=False)
        new_index_map = {}
        display_names = []
        for idx, c in enumerate(commits):
            display_names.append(self.current_vault.get_commit_display_name(c, current_branch))
            new_index_map[idx] = c

        self.vault_history_list.delete(0, tk.END)
        for display in display_names:
            self.vault_history_list.insert(tk.END, display)
        self._commit_index_map = new_index_map

        # Refresh visualizer
        if hasattr(self, 'branch_viz'):
            self.branch_viz.refresh()

    def _get_selected_commit_filename(self):
        """Maps the current listbox selection back to the actual commit filename."""
        selection = self.vault_history_list.curselection()
        if not selection:
            return None
        return self._commit_index_map.get(selection[0])

    def on_tree_node_click(self, event_type, data):
        """Called when a node or branch in the visualizer is clicked."""
        try:
            target_branch = data.get('branch')
            
            # 1. Switch Logical Branch (View Only)
            if target_branch:
                self.current_vault.switch_branch(target_branch)
                self.vault_log(f"Switched view to branch '{target_branch}'")
                self.refresh_vault_ui() 
            
            # 2. If it was a commit click, select it in the list
            if event_type == "commit":
                snapshot_name = data.get('commit')
                # Reverse lookup: find index by commit filename
                for idx, filename in getattr(self, '_commit_index_map', {}).items():
                    if filename == snapshot_name:
                        self.vault_history_list.selection_clear(0, tk.END)
                        self.vault_history_list.selection_set(idx)
                        self.vault_history_list.see(idx)
                        break
                    
        except Exception as e:
            logging.error(f"Error handling tree click: {e}")
            messagebox.showerror("Error", f"Could not inspect branch: {e}")
            
    def perform_vault_commit(self):
        if not self.current_vault: return
        msg = self.vault_commit_msg.get().strip()
        if not msg:
            messagebox.showwarning("Input Required", "Please enter a commit message.", parent=self.root)
            return
            
        threading.Thread(target=self._commit_thread, args=(msg,), daemon=True).start()

    def _commit_thread(self, message):
        self.vault_log_safe("Starting commit...")
        self.root.after(0, lambda: self.btn_commit.configure(state=tk.DISABLED))
        try:
            snapshot = self.current_vault.commit(message, progress_callback=self.vault_log_safe)
            display = self.current_vault.get_commit_display_name(snapshot)
            self.vault_log_safe(f"Commit successful: {display}")
            self.root.after(0, self.refresh_vault_ui)
            self.root.after(0, lambda: self.vault_commit_msg.delete(0, tk.END))
        except Exception as e:
             self.vault_log_safe(f"Commit FAILED: {e}")
             messagebox.showerror("Commit Failed", str(e))
        finally:
            self.root.after(0, lambda: self.btn_commit.configure(state=tk.NORMAL))

    def create_vault_branch(self):
        if not self.current_vault: return
        
        # Custom dialog to ask for name AND type (Linked vs Empty)
        dialog = tk.Toplevel(self.root)
        dialog.title("Create New Branch")
        dialog.geometry("400x180")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=current_theme_colors["BG_COLOR"])
        
        # Center the dialog
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - (dialog.winfo_width() // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{x}+{y}")

        ttk.Label(dialog, text="Branch Name:", background=current_theme_colors["BG_COLOR"], foreground=current_theme_colors["TEXT_COLOR"]).pack(pady=(20, 5))
        name_var = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=name_var, width=40)
        entry.pack(pady=5)
        entry.focus_set()

        link_var = tk.BooleanVar(value=True)
        # Checkbox for linking
        current_branch = self.current_vault._load_meta().get('current_branch', 'unknown')
        chk = ttk.Checkbutton(dialog, text=f"Link to current branch '{current_branch}' (Inherit History)", variable=link_var)
        chk.pack(pady=10)

        def on_create():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("Input Required", "Please enter a branch name.", parent=dialog)
                return
            
            try:
                self.current_vault.create_branch(name, linked=link_var.get())
                link_type = "linked" if link_var.get() else "independent"
                self.vault_log(f"Created branch '{name}' ({link_type}) from '{current_branch}'")

                if messagebox.askyesno("Switch Branch", f"Switch to new branch '{name}'?", parent=dialog):
                    self.current_vault.switch_branch(name)
                    self.vault_log(f"Switched to branch '{name}'")
                    self.refresh_vault_ui()

                dialog.destroy()
            except Exception as e:
                self.vault_log(f"ERROR: Failed to create branch: {e}")
                messagebox.showerror("Error", f"Failed to create branch: {e}", parent=dialog)

        ttk.Button(dialog, text="Create Branch", command=on_create).pack(pady=10)

    def delete_vault_branch(self):
        if not self.current_vault: return
        
        # Get list of branches (excluding current)
        meta = self.current_vault._load_meta()
        all_branches = meta.get('branches', [])
        current = meta.get('current_branch', '')
        
        candidates = [b for b in all_branches if b != current]
        
        if not candidates:
            messagebox.showinfo("Delete Branch", "No other branches available to delete.\n(You cannot delete the current branch).", parent=self.root)
            return

        # Simple input dialog or custom list selection? 
        # For safety, let's list them.
        
        dialog = tk.Toplevel(self.root)
        dialog.title("Delete Branch")
        dialog.geometry("300x350")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg=current_theme_colors["BG_COLOR"])
        
        ttk.Label(dialog, text="Select Branch to Delete:", background=current_theme_colors["BG_COLOR"], foreground=current_theme_colors["TEXT_COLOR"]).pack(pady=10)
        
        list_frame = ttk.Frame(dialog)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        lb = tk.Listbox(list_frame, height=10)
        lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=lb.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        lb.config(yscrollcommand=sb.set)
        
        for b in candidates:
            lb.insert(tk.END, b)
            
        def on_delete():
            selection = lb.curselection()
            if not selection:
                return
            target = lb.get(selection[0])
            
            if messagebox.askyesno("Confirm Deletion", f"Are you sure you want to PERMANENTLY DELETE branch '{target}'?\n\nThis cannot be undone.", parent=dialog):
                try:
                    self.current_vault.delete_branch(target)
                    self.vault_log(f"Deleted branch '{target}'")
                    messagebox.showinfo("Success", f"Branch '{target}' deleted.", parent=dialog)
                    dialog.destroy()
                    self.refresh_vault_ui()
                except Exception as e:
                    self.vault_log(f"ERROR: Failed to delete branch '{target}': {e}")
                    messagebox.showerror("Delete Failed", str(e), parent=dialog)

        ttk.Button(dialog, text="Delete Selected", command=on_delete).pack(pady=10)
        ttk.Button(dialog, text="Cancel", command=dialog.destroy).pack(pady=5)

    def export_vault_branch(self):
        if not self.current_vault: return
        
        # Get list of branches to let user choose? 
        # Requirement: "select a branch as the current branch and then click a button"
        # So it uses the current branch.
        
        meta = self.current_vault._load_meta()
        current_branch = meta.get('current_branch', 'unknown')
        
        if messagebox.askyesno("Export Branch", f"Export latest snapshot of branch '{current_branch}'?"):
            dest_dir = filedialog.askdirectory(title="Select Destination for Export", parent=self.root)
            if dest_dir:
                threading.Thread(target=self._export_thread, args=(current_branch, dest_dir), daemon=True).start()

    def _export_thread(self, branch_name, dest_dir):
        self.vault_log_safe(f"Exporting branch '{branch_name}' to {dest_dir}...")
        try:
            snapshot_name = self.current_vault.export_branch_head(branch_name, dest_dir, progress_callback=self.vault_log_safe)
            display = self.current_vault.get_commit_display_name(snapshot_name, branch_name)
            self.vault_log_safe(f"Export complete: {display}")
            messagebox.showinfo("Success", f"Branch '{branch_name}' exported to:\n{dest_dir}")
        except Exception as e:
            self.vault_log_safe(f"Export FAILED: {e}")
            messagebox.showerror("Export Failed", str(e))

    def open_compare_window(self):
        if not self.current_vault: return
        snapshot = self._get_selected_commit_filename()
        if not snapshot:
            messagebox.showinfo("Select Snapshot", "Please select a snapshot from the history list to compare.")
            return
        VaultMergeWindow(self, self.current_vault, snapshot)

    def perform_full_restore(self):
        if not self.current_vault: return
        snapshot = self._get_selected_commit_filename()
        if not snapshot:
            messagebox.showinfo("Select Snapshot", "Please select a snapshot from the history list to restore.")
            return
        display = self.current_vault.get_commit_display_name(snapshot)

        if messagebox.askyesno("High Risk Operation", f"Are you SURE you want to restore:\n\n{display}\n\nThis will WIPE all current files in the project folder (except .solace_vault) and replace them with the snapshot.\n\nUnsaved changes will be LOST."):
             threading.Thread(target=self._restore_thread, args=(snapshot,), daemon=True).start()

    def _restore_thread(self, snapshot):
        display = self.current_vault.get_commit_display_name(snapshot)
        self.vault_log_safe(f"Starting full restore: {display}")
        self.root.after(0, lambda: self.btn_checkout.configure(state=tk.DISABLED))
        try:
             self.current_vault.restore_snapshot(snapshot, progress_callback=self.vault_log_safe)
             self.vault_log_safe("Restore complete.")
             messagebox.showinfo("Success", f"Restored: {display}")
        except Exception as e:
             self.vault_log_safe(f"Restore FAILED: {e}")
             messagebox.showerror("Error", f"Restore failed: {e}")
        finally:
             if self.btn_checkout.winfo_exists():
                 self.root.after(0, lambda: self.btn_checkout.configure(state=tk.NORMAL))

    def explore_snapshot(self):
        if not self.current_vault: return
        snapshot = self._get_selected_commit_filename()
        if not snapshot:
             messagebox.showinfo("Select Snapshot", "Please select a snapshot from the history list to explore.")
             return

        threading.Thread(target=self._explore_thread, args=(snapshot,), daemon=True).start()

    def _explore_thread(self, snapshot):
         display = self.current_vault.get_commit_display_name(snapshot)
         self.vault_log_safe(f"Mounting snapshot: {display}")
         try:
             meta = self.current_vault._load_meta()
             branch = meta['current_branch']
             self.vault_log_safe("Locating snapshot archive...")
             snap_path = self.current_vault.find_snapshot_path(snapshot, branch)

             # Use a temp directory
             import tempfile
             temp_base = tempfile.gettempdir()
             temp_mount = os.path.join(temp_base, f"Solace_Mount_{snapshot}")

             if not os.path.exists(temp_mount):
                 self.vault_log_safe("Extracting to temporary directory...")
                 os.makedirs(temp_mount)
                 with zipfile.ZipFile(snap_path, 'r') as zf:
                     file_count = len(zf.namelist())
                     zf.extractall(temp_mount)
                 self.vault_log_safe(f"Extracted {file_count} files to {temp_mount}")
             else:
                 self.vault_log_safe("Using cached extraction")

             if IS_WINDOWS:
                 os.startfile(temp_mount)
             else:
                 # Try linux openers
                 try: subprocess.Popen(['xdg-open', temp_mount])
                 except: pass

             self.vault_log_safe("Opened snapshot in file explorer.")
         except Exception as e:
             self.vault_log_safe(f"Mount FAILED: {e}")


    def apply_theme(self, theme_name):
        logging.info(f"Applying theme: {theme_name}")
        real_theme_name = theme_name
        if theme_name == "System":
            real_theme_name = get_system_theme()
            logging.info(f"System theme detected as: {real_theme_name}")

        try:
            theme = THEMES[real_theme_name]
            logging.info(f"Loaded theme definition for '{real_theme_name}': BG_COLOR={theme.get('BG_COLOR')}")
        except KeyError:
            logging.warning(f"Theme '{real_theme_name}' not found. Using default.")
            real_theme_name = "Light (Default)"
            theme = THEMES[real_theme_name]

        global current_theme_colors
        current_theme_colors = theme

        style = ttk.Style()
        try:
            theme_to_use = 'clam' if 'clam' in style.theme_names() else 'vista'
            style.theme_use(theme_to_use)
        except tk.TclError:
            pass

        style.configure('.', background=theme["BG_COLOR"], foreground=theme["TEXT_COLOR"],
                        font=('Segoe UI', 9), fieldbackground=theme["ENTRY_BG"])
        style.map('.', fieldbackground=[('readonly', theme["ENTRY_BG"])],
                  foreground=[('readonly', theme["ENTRY_FG"])])

        style.configure('TFrame', background=theme["BG_COLOR"])
        style.configure('TButton', padding=(10, 5), font=('Segoe UI', 9), relief="flat",
                        background=theme["BUTTON_BG"], foreground=theme["TEXT_COLOR"])
        style.map('TButton', background=[('active', theme["BUTTON_ACTIVE"])])
        style.configure('TLabel', background=theme["BG_COLOR"], foreground=theme["TEXT_COLOR"], font=('Segoe UI', 9))
        style.configure('Header.TLabel', background=theme["HEADER_BG"], foreground=theme["TEXT_COLOR"], font=('Segoe UI', 11, "bold"))
        style.configure('TEntry', padding=5, font=('Segoe UI', 9),
                        fieldbackground=theme["ENTRY_BG"], foreground=theme["ENTRY_FG"],
                        insertcolor=theme["TEXT_INSERT"])
        style.configure('TSpinbox', padding=5,
                        fieldbackground=theme["SPIN_BG"], foreground=theme["ENTRY_FG"],
                        insertcolor=theme["TEXT_INSERT"])
        style.configure('TCheckbutton', background=theme["BG_COLOR"], foreground=theme["TEXT_COLOR"],
                        indicatorbackground=theme["ENTRY_BG"], indicatorforeground=theme["TEXT_COLOR"])
        style.map('TCheckbutton', indicatorbackground=[('selected', theme["ACCENT_COLOR"])])
        style.configure('TPanedwindow', background=theme["BG_COLOR"])
        style.configure('Horizontal.TProgressbar', thickness=10, background=theme["ACCENT_COLOR"], troughcolor=theme["FRAME_BG"])
        style.configure('Content.TFrame', background=theme["FRAME_BG"], relief=tk.SOLID, borderwidth=1, bordercolor=theme["BORDER"])
        style.configure('TScrollbar', relief=tk.FLAT, background=theme["BG_COLOR"], troughcolor=theme["FRAME_BG"],
                        arrowcolor=theme["TEXT_COLOR"], bordercolor=theme["BG_COLOR"])
        style.map('TScrollbar', background=[('active', theme["BUTTON_ACTIVE"])])
        style.configure('TSeparator', background=theme["BORDER"])
        style.configure('TCombobox',
                        fieldbackground=theme["ENTRY_BG"], foreground=theme["ENTRY_FG"],
                        selectbackground=theme["SELECT_BG"], selectforeground=theme["SELECT_FG"],
                        insertcolor=theme["TEXT_INSERT"], arrowcolor=theme["TEXT_COLOR"])
        style.map('TCombobox', fieldbackground=[('readonly', theme["ENTRY_BG"])],
                  selectbackground=[('!focus', theme["SELECT_BG"])],
                  selectforeground=[('!focus', theme["SELECT_FG"])])

        style.configure('TNotebook.Tab', background=theme["TAB_BG"], foreground=theme["TEXT_COLOR"], padding=(10, 5))
        style.map('TNotebook.Tab', background=[('selected', theme["BG_COLOR"])], foreground=[('selected', theme["TEXT_COLOR"])])

        # New style for Snapshot History to match Status/Logs
        style.configure('Snapshot.TLabelframe', background=theme["BG_COLOR"])
        style.configure('Snapshot.TLabelframe.Label', background=theme["HEADER_BG"], foreground=theme["TEXT_COLOR"], font=('Segoe UI', 9, "bold"))

        self.root.configure(bg=theme["BG_COLOR"])
        self.main_frame.configure(style='TFrame')
        
        # --- NEW: Update Logo Colors ---
        if hasattr(self, 'logo_label'):
            self.logo_label.configure(bg=theme["BG_COLOR"])
            # Tinting removed for now to ensure raw PNG works
        
        if hasattr(self, 'title_label'):
            self.title_label.configure(background=theme["BG_COLOR"], foreground=theme["TEXT_COLOR"])
        # -------------------------------

        self.paned_window.configure(style='TPanedwindow')
        self.left_frame.configure(style='Content.TFrame')
        self.right_frame.configure(style='Content.TFrame')
        self.list_frame.configure(bg=theme["FRAME_BG"]) # NOW WORKS (tk.Frame)
        self.log_frame.configure(bg=theme["FRAME_BG"])  # NOW WORKS (tk.Frame)
        self.job_buttons_frame.configure(style='TFrame')
        self.status_frame.configure(style='TFrame')
        self.bottom_frame.configure(style='TFrame')

        self.job_header_label.configure(style='Header.TLabel')
        self.log_header_label.configure(style='Header.TLabel')

        if job_listbox:
            job_listbox.configure(bg=theme["LIST_BG"], fg=theme["TEXT_COLOR"],
                                  selectbackground=theme["SELECT_BG"], selectforeground=theme["SELECT_FG"])
            self.populate_job_list()

        if log_text_widget:
            log_text_widget.configure(bg=theme["LOG_BG"], fg=theme["LOG_FG"],
                                      insertbackground=theme["TEXT_INSERT"])

        if hasattr(self, 'vault_history_list'):
            self.vault_history_list.configure(bg=theme["LOG_BG"], fg=theme["LOG_FG"],
                                              selectbackground=theme["SELECT_BG"], selectforeground=theme["SELECT_FG"])

        if hasattr(self, 'vault_log_text'):
            self.vault_log_text.configure(bg=theme["LOG_BG"], fg=theme["LOG_FG"],
                                          insertbackground=theme["TEXT_INSERT"])
        if hasattr(self, 'vault_log_inner_frame'):
            self.vault_log_inner_frame.configure(bg=theme["FRAME_BG"])

        if hasattr(self, 'branch_viz'):
            self.branch_viz.set_colors(theme)

        logging.info("Theme application finished.")
        
        # Refresh logo with new theme colors if it exists
        if hasattr(self, 'update_logo_size'):
            self.update_logo_size()

        self.root.update_idletasks()
        self.root.update()

    def set_window_icon(self):
        try:
            if getattr(sys, 'frozen', False): script_dir = os.path.dirname(sys.executable)
            else: script_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(script_dir, "Solace_Backup.ico")
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
                logging.info(f"Window/Taskbar icon set to: {icon_path}")
            else:
                logging.warning(f"Window icon file not found: {icon_path}. Using default.")
                self.log_message_gui(f"WARNING: Window icon file not found: {icon_path}")
        except tk.TclError as e:
            logging.error(f"Failed to set window icon: {e}. Ensure it's a valid .ico file.")
            self.log_message_gui(f"ERROR: Failed to set window icon. Check format/path. Details: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred while setting window icon: {e}")
            self.log_message_gui(f"ERROR: Unexpected error setting window icon. Details: {e}")

    def populate_job_list(self):
        theme = current_theme_colors
        job_listbox.delete(0, tk.END)
        if current_config and 'backup_jobs' in current_config:
            for i, job in enumerate(current_config['backup_jobs']):
                status = " (Enabled)" if job.get('enabled', False) else " (Disabled)"
                schedule = job.get('schedule', 'manual')
                job_listbox.insert(tk.END, f"{job['name']}{status} [{schedule}]")
                color = theme["LIST_BG"] if i % 2 == 0 else theme["LIST_ALT_BG"]
                job_listbox.itemconfig(i, {'bg': color, 'fg': theme["TEXT_COLOR"],
                                          'selectbackground': theme["SELECT_BG"],
                                          'selectforeground': theme["SELECT_FG"]})
        logging.info("Job listbox updated.")

    def process_log_queue(self): # Unchanged
        try:
            while True:
                message = self.log_queue.get_nowait()
                if isinstance(message, tuple):
                    msg_type = message[0]
                    if msg_type == "status": _, j, s, t, m = message; self.update_status_bar(j, s, t, m, (s in [1, 2]))
                    elif msg_type == "file_update": _, j, f = message; self.update_status_bar(j, 1, 4, f"Copying: {f}", True)
                else: self.log_message_gui(message)
        except queue.Empty: pass
        finally: self.root.after(100, self.process_log_queue)

    def update_status_bar(self, job_name, step, total, message, indeterminate=False): # Unchanged
        if job_name == "Idle" or step == 0:
            self.status_label.config(text="Status: Idle"); self.progress_bar.stop()
            self.progress_bar.config(mode='determinate'); self.progress_bar['value'] = 0
        else:
            self.status_label.config(text=f"Status: [{job_name}] {step}/{total} - {message}")
            if indeterminate:
                if self.progress_bar['mode'] != 'indeterminate':
                    self.progress_bar.config(mode='indeterminate'); self.progress_bar.start(15)
            else:
                if self.progress_bar['mode'] != 'determinate':
                    self.progress_bar.stop(); self.progress_bar.config(mode='determinate')
                self.progress_bar['maximum'] = total; self.progress_bar['value'] = step

    def log_message_gui(self, message): # Unchanged
        now = datetime.now().strftime("%H:%M:%S")
        if log_text_widget:
            log_text_widget.config(state=tk.NORMAL)
            log_text_widget.insert(tk.END, f"[{now}] {message}\n")
            log_text_widget.see(tk.END); log_text_widget.config(state=tk.DISABLED)
        logging.info(message)

    def vault_log(self, message):
        """Writes a timestamped message to the vault activity log. Must be called from the main thread."""
        now = datetime.now().strftime("%H:%M:%S")
        if hasattr(self, 'vault_log_text'):
            self.vault_log_text.config(state=tk.NORMAL)
            self.vault_log_text.insert(tk.END, f"[{now}] {message}\n")
            self.vault_log_text.see(tk.END)
            self.vault_log_text.config(state=tk.DISABLED)
        logging.info(f"Vault: {message}")

    def vault_log_safe(self, message):
        """Thread-safe vault log — schedules vault_log() on the main thread."""
        self.root.after(0, lambda m=message: self.vault_log(m))

    def view_log_file(self): # Unchanged
        try: os.startfile(os.path.abspath(LOG_FILE))
        except Exception as e: messagebox.showerror("Error", f"Could not open log file: {e}")

    def open_add_job_window(self): JobEditorWindow(self.root) # Unchanged
    def open_edit_job_window(self): # Unchanged
        try:
            idx = job_listbox.curselection()[0]; name = job_listbox.get(idx).split(" (")[0].strip()
            job = next((j for j in current_config['backup_jobs'] if j['name'] == name), None)
            if job: JobEditorWindow(self.root, job_data=job, original_job_name=name)
            else: messagebox.showerror("Error", "Could not find job.")
        except IndexError: messagebox.showwarning("Warning", "Select a job.")

    def remove_backup_job(self): # Unchanged
        try:
            idx = job_listbox.curselection()[0]; name = job_listbox.get(idx).split(" (")[0].strip()
            if messagebox.askyesno("Confirm", f"Remove '{name}'?"):
                if APS_AVAILABLE and scheduler:
                    try: scheduler.remove_job(name)
                    except: pass
                current_config['backup_jobs'] = [j for j in current_config['backup_jobs'] if j['name'] != name]
                if save_config(current_config):
                    self.log_message_gui(f"Removed '{name}'."); self.populate_job_list()
                else: messagebox.showerror("Error", "Failed to save config.")
        except IndexError: messagebox.showwarning("Warning", "Select a job.")

    def run_selected_backup(self): # Unchanged
        try:
            idx = job_listbox.curselection()[0]; name = job_listbox.get(idx).split(" (")[0].strip()
            job = next((j for j in current_config['backup_jobs'] if j['name'] == name), None)
            if job:
                if not job.get('enabled'): messagebox.showwarning("Disabled", "Job disabled."); return
                self.log_message_gui(f"Queueing manual backup: {name}")
                self.queue_manager.add_job(job, current_config['global_settings'], self.log_queue)
            else: messagebox.showerror("Error", "Could not find job.")
        except IndexError: messagebox.showwarning("Warning", "Select a job.")

    def run_all_backups(self): # Unchanged
        self.log_message_gui("--- Starting 'Run All Backups' ---")
        jobs = [j for j in current_config['backup_jobs'] if j.get('enabled')]
        if not jobs: self.log_message_gui("No enabled jobs."); return
        self.log_message_gui(f"Queueing {len(jobs)} jobs...")
        for job in jobs:
            self.log_message_gui(f"Queueing: {job['name']}")
            self.queue_manager.add_job(job, current_config['global_settings'], self.log_queue)



    def open_queue_selection_window(self):
        QueueSelectionWindow(self.root)

    def open_settings(self):
        SettingsWindow(self.root)

    def on_closing(self):
        minimize = False
        if current_config and 'global_settings' in current_config:
             minimize = current_config['global_settings'].get('minimize_to_tray', False)
        
        if minimize and TRAY_AVAILABLE:
            self.hide_window()
        else:
            self.quit_application()

    def setup_tray_icon(self):
        if not TRAY_AVAILABLE: return
        try:
            # Prioritize PNG for better cross-platform compatibility (especially Linux)
            icon_names = ["Solace_Backup_Tray.png", "Solace_Backup_Tray.ico"]
            
            if getattr(sys, 'frozen', False): script_dir = os.path.dirname(sys.executable)
            else: script_dir = os.path.dirname(os.path.abspath(__file__))
            
            image = None
            loaded_path = None

            for icon_name in icon_names:
                full_icon_path = os.path.join(script_dir, icon_name)
                if os.path.exists(full_icon_path):
                    try:
                        image = Image.open(full_icon_path)
                        loaded_path = full_icon_path
                        break # Found a valid icon
                    except Exception:
                        continue # Try next format

            if image:
                self.log_message_gui(f"Loaded custom tray icon: {loaded_path}")
            else:
                raise FileNotFoundError(f"No suitable icon found (tried: {icon_names})")

        except Exception as e:
            self.log_message_gui(f"WARNING: Icon load error ({e}). Using default.")
            logging.warning(f"Icon load error: {e}")
            image = create_image(64, 64, 'darkblue', 'lightblue')

        menu = (pystray.MenuItem('Show', self.show_window, default=True),
                pystray.MenuItem('Run All Now', self.run_all_backups),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Exit', self.quit_application))
        self.tray_icon = pystray.Icon("BackupSuite", image, "Solace Backup Suite", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True, name="SystemTrayThread").start()

    def show_window(self): self.root.after(0, self.root.deiconify) # Unchanged
    def hide_window(self): # Unchanged
        self.root.withdraw()
        self.log_message_gui("Application hidden to system tray.")
        if TRAY_AVAILABLE and self.tray_icon:
             try: self.tray_icon.notify('Running in background', 'Backup Suite')
             except Exception as e: logging.warning(f"Tray notification error: {e}")

    def on_window_resize(self, event):
        # Only handle resize of the main window, not child widgets
        if event.widget == self.root:
            # Debounce the resize event
            if self.resize_timer:
                self.root.after_cancel(self.resize_timer)
            self.resize_timer = self.root.after(100, self.update_logo_size)

    def apply_logo_tint(self, base_image):
        try:
            # Get accent color from current theme
            accent_color = current_theme_colors.get("ACCENT_COLOR", "#0078D7")
            
            width, height = base_image.size
            
            # Create a solid color image
            tint_image = Image.new("RGBA", (width, height), accent_color)
            
            # Create a gradient mask (L mode) for the alpha channel of the tint
            # Gradient from transparent (top) to semi-opaque (bottom)
            mask = Image.new('L', (width, height))
            draw = ImageDraw.Draw(mask)
            
            # Draw gradient
            for y in range(height):
                # Calculate opacity: 0 at top, up to ~100 (approx 40%) at bottom
                opacity = int(100 * (y / height)) 
                draw.line((0, y, width, y), fill=opacity)
            
            # Apply the gradient mask to the tint image's alpha channel
            tint_image.putalpha(mask)
            
            # Composite the tint over the base image
            # Ensure base image is RGBA
            if base_image.mode != 'RGBA':
                base_image = base_image.convert('RGBA')
                
            return Image.alpha_composite(base_image, tint_image)
            
        except Exception as e:
            logging.error(f"Error tinting logo: {e}")
            return base_image

    def update_logo_size(self):
        try:
            if not hasattr(self, 'original_logo_image') or not self.original_logo_image:
                return

            # Get width of the left frame (parent of the logo)
            # Use winfo_width() but ensure it's valid (>1)
            container_width = self.left_frame.winfo_width()
            
            # Fallback if container width is not yet established
            if container_width <= 1:
                 # Try to estimate from window width if left frame isn't ready, 
                 # assuming roughly 1/3 split or just wait
                 return 

            # Target width: Fill the container minus some padding
            target_width = container_width - 20 
            
            # Ensure minimum and maximum sizes
            # Max size shouldn't exceed original image width to avoid blurriness, or set a reasonable cap
            target_width = max(50, min(target_width, 400))

            # Calculate height to maintain aspect ratio
            orig_width, orig_height = self.original_logo_image.size
            aspect_ratio = orig_height / orig_width
            target_height = int(target_width * aspect_ratio)

            # Resize image (LANCZOS for quality)
            resized_image = self.original_logo_image.resize((target_width, target_height), Image.Resampling.LANCZOS)
            
            # Apply Tint
            tinted_image = self.apply_logo_tint(resized_image)
            
            self.logo_photo = ImageTk.PhotoImage(tinted_image)
            
            self.logo_label.configure(image=self.logo_photo)
            self.logo_label.image = self.logo_photo # Keep reference
            
        except Exception as e:
            logging.error(f"Error resizing logo: {e}")

    def quit_application(self): # Unchanged
        self.log_message_gui("Shutting down...")
        if APS_AVAILABLE and scheduler and scheduler.running:
            try: scheduler.shutdown(wait=False)
            except Exception as e: logging.error(f"Error shutting scheduler: {e}")
        if TRAY_AVAILABLE and self.tray_icon:
            try: self.tray_icon.stop()
            except Exception as e: logging.error(f"Error stopping tray: {e}")
        self.root.destroy()
        logging.info("Application Exited."); os._exit(0)

    def _setup_signal_handlers(self):
        """Set up Unix signal handlers for external control (Waybar integration)."""
        try:
            signal.signal(signal.SIGUSR1, self._signal_show_window)
            signal.signal(signal.SIGUSR2, self._signal_run_backups)
            self.log_message_gui("Signal handlers registered (SIGUSR1=show, SIGUSR2=run)")
        except Exception as e:
            logging.warning(f"Could not set up signal handlers: {e}")

    def _signal_show_window(self, signum, frame):
        """Handler for SIGUSR1 - shows the main window."""
        self.log_message_gui("Signal received: Show window (SIGUSR1)")
        self.show_window()
        self._update_status_file("IDLE", "Window shown via signal")

    def _signal_run_backups(self, signum, frame):
        """Handler for SIGUSR2 - runs all enabled backups."""
        self.log_message_gui("Signal received: Run all backups (SIGUSR2)")
        self._update_status_file("RUNNING", "Running all backups via signal")
        self.run_all_backups()

    def _update_status_file(self, status, message):
        """Write current status to the status file for Waybar to read."""
        try:
            import json
            from datetime import datetime
            data = {
                "status": status,
                "message": message,
                "timestamp": datetime.now().isoformat(),
                "pid": os.getpid()
            }
            with open(STATUS_FILE, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logging.warning(f"Could not write status file: {e}")

    def show_window(self):
        """Show the main window (deiconify)."""
        self.root.after(0, self.root.deiconify)
        self.log_message_gui("Window shown")

# ==============================================================================
# 8. MAIN EXECUTION BLOCK
# ==============================================================================
if __name__ == "__main__":
    # Ensure we are running from the script's directory so relative paths (like "Settings") work
    if getattr(sys, 'frozen', False):
        os.chdir(os.path.dirname(sys.executable))
    else:
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Solace Backup Suite')
    parser.add_argument('--tray', action='store_true', 
                        help='Start minimized to system tray')
    parser.add_argument('--minimized', action='store_true',
                        help='Start minimized (alias for --tray)')
    args = parser.parse_args()
    
    # Combine both flags and check config setting
    start_minimized = args.tray or args.minimized
    
    # Also check the saved setting in config (only if not explicitly overridden by command line)
    if not args.tray and not args.minimized:
        try:
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Settings", "backup_config.json")
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config_data = json.load(f)
                    global_settings = config_data.get('global_settings', {})
                    if global_settings.get('start_minimized', False):
                        start_minimized = True
        except Exception as e:
            logging.warning(f"Could not read start_minimized setting: {e}")
    
    # Check for running instance before starting
    running_pid = check_for_running_instance()
    if running_pid:
        print(f"Solace Backup Suite is already running (PID {running_pid}).")
        # Always signal the existing instance to show its window
        # This ensures CLI access always brings up the GUI
        try:
            os.kill(running_pid, signal.SIGUSR1)
            print("Sent signal to show existing window.")
        except Exception as e:
            print(f"Failed to signal running instance: {e}")
            logging.warning(f"Failed to signal running instance: {e}")
        sys.exit(0)

    current_config = None
    main_app_ref = None
    job_listbox = None
    log_text_widget = None

    root = tk.Tk()
    app = BackupApp(root, start_minimized=start_minimized)
    root.mainloop()
