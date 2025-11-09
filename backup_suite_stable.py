# backup_suite.pyw
# A GUI Backup Application with Scheduling, System Tray, Startup Option, and Per-Job Retention

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
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
import winreg # For Windows startup registry
import sys    # For executable path and sys.argv

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
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    print("WARNING: pystray or Pillow not found. System tray icon will be disabled.")
    print("         Install with: pip install pystray pillow")

# ==============================================================================
# 1. LOGGING CONFIGURATION
# ==============================================================================
LOG_DIR = "Debug"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "backup_suite_debug.log")

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format='%(asctime)s - %(threadName)s - %(levelname)s - %(message)s',
    filemode='w'
)
logging.info(f"Application Starting Up... Executable: {sys.executable}, Script: {os.path.abspath(sys.argv[0])}")


# ==============================================================================
# 2. CONFIGURATION MANAGEMENT
# ==============================================================================
CONFIG_DIR = "Settings"
CONFIG_FILE = "backup_config.json"
CONFIG_PATH = os.path.join(CONFIG_DIR, CONFIG_FILE)

def get_default_config():
    """Returns a dictionary with the default configuration structure."""
    return {
        "global_settings": {
            "default_volumes_to_keep": 3,
            "default_backup_base_name": "Backups_Py"
        },
        "backup_jobs": []
    }

def load_config():
    """Loads the configuration from JSON or creates a default."""
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
    """Saves the given configuration dictionary to JSON."""
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
# 2.5 WINDOWS STARTUP REGISTRY FUNCTIONS
# ==============================================================================
REG_APP_NAME = "PythonBackupSuite"
REG_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

def get_application_path_for_startup():
    """Determines the command to run the application for startup."""
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
    """Adds the application to Windows startup for the current user."""
    try:
        command = get_application_path_for_startup()
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_WRITE)
        winreg.SetValueEx(key, REG_APP_NAME, 0, winreg.REG_SZ, command)
        winreg.CloseKey(key)
        logging.info(f"Application added to startup: {command}")
        if main_app_ref: main_app_ref.log_message_gui("Application added to Windows startup.")
        return True
    except Exception as e:
        logging.error(f"Error adding to startup: {e}")
        if main_app_ref: main_app_ref.log_message_gui(f"ERROR adding to startup: {e}")
        return False

def remove_from_startup():
    """Removes the application from Windows startup for the current user."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_WRITE)
        winreg.DeleteValue(key, REG_APP_NAME)
        winreg.CloseKey(key)
        logging.info("Application removed from startup.")
        if main_app_ref: main_app_ref.log_message_gui("Application removed from Windows startup.")
        return True
    except FileNotFoundError:
        logging.info("Application was not in startup (registry key not found).")
        return True
    except Exception as e:
        logging.error(f"Error removing from startup: {e}")
        if main_app_ref: main_app_ref.log_message_gui(f"ERROR removing from startup: {e}")
        return False

def check_if_in_startup():
    """Checks if the application is configured to run on startup via registry."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY_PATH, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, REG_APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        logging.error(f"Error checking startup status: {e}")
        return False


# ==============================================================================
# 3. CORE BACKUP LOGIC
# ==============================================================================

def read_subprocess_output(process, log_queue, job_name):
    try:
        with io.TextIOWrapper(process.stdout, encoding='cp437', errors='replace') as stdout_reader:
            for line in iter(stdout_reader.readline, ''):
                line = line.strip()
                if line:
                    if line.startswith('\t'):
                        parts = line.split('\t')
                        file_info = next((part for part in parts if part.strip()), None)
                        if file_info:
                            log_queue.put(("file_update", job_name, file_info.strip()))
    except Exception as e:
        log_queue.put(f"[{job_name}] ERROR reading Robocopy output: {e}")
    finally:
        log_queue.put(f"[{job_name}] Robocopy output reader finished.")


def run_robocopy_backup(job_details, temp_dest_dir, log_queue):
    source_dir = job_details['source_dir']
    exclusions = job_details.get('exclusions', [])
    job_name = job_details['name']
    log_queue.put(f"[{job_name}] Starting Robocopy...")
    command = ["robocopy", source_dir, temp_dest_dir, "/E", "/COPY:DAT", "/R:1", "/W:1", "/BYTES"]
    for exclusion in exclusions: command.append("/XD"); command.append(exclusion)
    log_queue.put(f"[{job_name}]   Executing: {' '.join(command)}")
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        reader_thread = threading.Thread(target=read_subprocess_output, args=(process, log_queue, job_name),
                                         daemon=True, name=f"RoboRead-{job_name}")
        reader_thread.start()
        process.wait()
        reader_thread.join(timeout=5)
        return_code = process.returncode
        log_queue.put(f"[{job_name}]   Robocopy finished with Exit Code: {return_code}")
        return return_code <= 1
    except FileNotFoundError: log_queue.put(f"[{job_name}] CRITICAL ERROR: robocopy.exe not found."); return False
    except Exception as e: log_queue.put(f"[{job_name}] CRITICAL ERROR during Robocopy: {e}"); return False

def create_zip_archive(job_details, source_dir, zip_file_path, log_queue):
    job_name = job_details['name']
    log_queue.put(f"[{job_name}] Starting Zipping Process...")
    log_queue.put(f"[{job_name}]   Source: {source_dir}")
    log_queue.put(f"[{job_name}]   Zip File: {zip_file_path}")
    source_path_for_ps = os.path.join(source_dir, '*')
    command_str = f"Compress-Archive -Path '{source_path_for_ps}' -DestinationPath '{zip_file_path}' -Force -ErrorAction SilentlyContinue"
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command_str]
    log_queue.put(f"[{job_name}]   Executing PowerShell...")
    try:
        process = subprocess.run(command, capture_output=True, text=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        return_code = process.returncode
        log_queue.put(f"[{job_name}]   PowerShell Exit Code: {return_code}")
        if process.stderr: log_queue.put(f"[{job_name}]   PowerShell StdErr: {process.stderr.strip()}")
        if return_code != 0: log_queue.put(f"[{job_name}] ERROR: PowerShell zip failed (Code {return_code})."); return False
        if os.path.exists(zip_file_path) and os.path.getsize(zip_file_path) > 0:
            log_queue.put(f"[{job_name}] SUCCESS: Zip file created."); return True
        elif os.path.exists(zip_file_path):
             log_queue.put(f"[{job_name}] WARNING: Zip file created but is empty!"); return False
        else: log_queue.put(f"[{job_name}] CRITICAL ERROR: Zip file missing after PowerShell success!"); return False
    except FileNotFoundError: log_queue.put(f"[{job_name}] CRITICAL ERROR: powershell.exe not found."); return False
    except Exception as e: log_queue.put(f"[{job_name}] CRITICAL ERROR during Zipping: {e}"); return False

def cleanup_temp_dir(job_details, temp_dir, log_queue):
    job_name = job_details['name']
    log_queue.put(f"[{job_name}] Cleaning up temporary folder: {temp_dir}")
    if not os.path.exists(temp_dir): log_queue.put(f"[{job_name}]   Temp folder not found, skipping."); return True
    try: shutil.rmtree(temp_dir); log_queue.put(f"[{job_name}]   Temp folder deleted."); return True
    except Exception as e: log_queue.put(f"[{job_name}] ERROR: Failed to delete temp folder: {e}"); return False

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
    robocopy_ok = run_robocopy_backup(job_details, temp_copy_dir, log_queue)

    zip_ok = False
    if robocopy_ok:
        update_status(2, "Zipping files...")
        zip_ok = create_zip_archive(job_details, temp_copy_dir, zip_file, log_queue)
    else: log_queue.put(f"[{job_name}] Skipping zip."); update_status(2, "Skipping zip...")

    update_status(3, "Cleaning temp files...")
    if os.path.exists(temp_copy_dir): cleanup_temp_dir(job_details, temp_copy_dir, log_queue)
    else: log_queue.put(f"[{job_name}] Temp dir doesn't exist.")

    update_status(4, "Cleaning old backups...")
    if zip_ok: perform_cleanup(job_details, volumes_to_keep, log_queue)
    else: log_queue.put(f"[{job_name}] Skipping rotation.")

    if robocopy_ok and zip_ok:
        log_queue.put(f"--- Job: {job_name} COMPLETED SUCCESSFULLY ---"); update_status(0, "Finished Successfully!")
    else: log_queue.put(f"--- Job: {job_name} FAILED ---"); update_status(0, "Finished with Errors!")
    time.sleep(2)
    log_queue.put(("status", "Idle", 0, 4, ""))

# ==============================================================================
# 4. SCHEDULER LOGIC
# ==============================================================================
scheduler = None
if APS_AVAILABLE:
    scheduler = BackgroundScheduler(daemon=True)
    def schedule_trigger_backup(job_details, global_settings, log_queue):
        log_queue.put(f"SCHEDULER: Triggered backup for {job_details['name']}.")
        threading.Thread(target=run_backup_job, args=(job_details, global_settings, log_queue),
                         daemon=True, name=f"Backup-{job_details['name']}").start()

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
                trigger = CronTrigger(day_of_week=d, hour=h, minute=minute)
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
        log_stream = io.StringIO() # Use io.StringIO for APScheduler's print_jobs
        scheduler.print_jobs(out=log_stream)
        logging.info(f"APScheduler jobs:\n{log_stream.getvalue()}")
        log_stream.close()


# ==============================================================================
# 5. SYSTEM TRAY ICON
# ==============================================================================
def create_image(width, height, color1, color2):
    if not TRAY_AVAILABLE: return None
    image = Image.new('RGB', (width, height), color1)
    dc = ImageDraw.Draw(image)
    dc.rectangle((width // 2, 0, width, height // 2), fill=color2)
    dc.rectangle((0, height // 2, width // 2, height), fill=color2)
    return image

# ==============================================================================
# 6. TKINTER GUI CLASSES
# ==============================================================================

class JobEditorWindow(tk.Toplevel):
    def __init__(self, parent, job_data=None, original_job_name=None):
        super().__init__(parent)
        self.parent = parent; self.job_data_to_edit = job_data; self.original_job_name = original_job_name
        self.title("Add/Edit Backup Job"); self.geometry("600x580"); self.transient(parent); self.grab_set()
        self.job_name_var = tk.StringVar(); self.source_dir_var = tk.StringVar(); self.dest_base_var = tk.StringVar()
        self.enabled_var = tk.BooleanVar(value=True); self.schedule_var = tk.StringVar(value="manual")
        self.volumes_override_var = tk.IntVar(value=0) # Default 0 meaning use global
        main_frame = ttk.Frame(self, padding="10"); main_frame.pack(fill=tk.BOTH, expand=True)

        row_num = 0
        ttk.Label(main_frame, text="Job Name:").grid(row=row_num, column=0, sticky=tk.W, pady=2)
        self.name_entry = ttk.Entry(main_frame, textvariable=self.job_name_var, width=60)
        self.name_entry.grid(row=row_num, column=1, columnspan=2, sticky=tk.EW, pady=2); row_num += 1

        ttk.Label(main_frame, text="Source Directory:").grid(row=row_num, column=0, sticky=tk.W, pady=2)
        self.source_entry = ttk.Entry(main_frame, textvariable=self.source_dir_var, width=50)
        self.source_entry.grid(row=row_num, column=1, sticky=tk.EW, pady=2)
        ttk.Button(main_frame, text="Browse...", command=self._browse_source).grid(row=row_num, column=2, sticky=tk.W, padx=5); row_num += 1

        ttk.Label(main_frame, text="Destination Base:").grid(row=row_num, column=0, sticky=tk.W, pady=2)
        self.dest_entry = ttk.Entry(main_frame, textvariable=self.dest_base_var, width=50)
        self.dest_entry.grid(row=row_num, column=1, sticky=tk.EW, pady=2)
        ttk.Button(main_frame, text="Browse...", command=self._browse_dest).grid(row=row_num, column=2, sticky=tk.W, padx=5); row_num += 1

        ttk.Label(main_frame, text="Exclusions (one per line):").grid(row=row_num, column=0, sticky=tk.NW, pady=2)
        self.exclusions_text = tk.Text(main_frame, height=8, width=60, relief=tk.SUNKEN, borderwidth=1)
        self.exclusions_text.grid(row=row_num, column=1, columnspan=2, sticky=tk.NSEW, pady=2)
        excl_scrollbar = ttk.Scrollbar(main_frame, orient=tk.VERTICAL, command=self.exclusions_text.yview)
        excl_scrollbar.grid(row=row_num, column=3, sticky=tk.NS, pady=2)
        self.exclusions_text.config(yscrollcommand=excl_scrollbar.set); row_num += 1

        ttk.Label(main_frame, text="Backups to Keep (Job Specific):").grid(row=row_num, column=0, sticky=tk.W, pady=5)
        self.volumes_override_spinbox = ttk.Spinbox(main_frame, from_=0, to=99, textvariable=self.volumes_override_var, width=8)
        self.volumes_override_spinbox.grid(row=row_num, column=1, sticky=tk.W, pady=5)
        ttk.Label(main_frame, text="(0 = use global default)").grid(row=row_num, column=2, sticky=tk.W, padx=5, pady=5); row_num += 1
        
        self.enabled_check = ttk.Checkbutton(main_frame, text="Enabled", variable=self.enabled_var)
        self.enabled_check.grid(row=row_num, column=1, sticky=tk.W, pady=5); row_num += 1
        
        ttk.Label(main_frame, text="Schedule (manual, daily@HH:MM, etc.):").grid(row=row_num, column=0, sticky=tk.W, pady=5)
        self.schedule_entry = ttk.Entry(main_frame, textvariable=self.schedule_var, width=30)
        self.schedule_entry.grid(row=row_num, column=1, columnspan=2, sticky=tk.EW, pady=5); row_num += 1
        
        buttons_frame = ttk.Frame(main_frame); buttons_frame.grid(row=row_num, column=0, columnspan=3, pady=20)
        ttk.Button(buttons_frame, text="Save", command=self._save_job).pack(side=tk.LEFT, padx=10)
        ttk.Button(buttons_frame, text="Cancel", command=self.destroy).pack(side=tk.LEFT, padx=10)
        
        if self.job_data_to_edit: self._populate_fields()
        main_frame.columnconfigure(1, weight=1); main_frame.rowconfigure(3, weight=1) # Exclusions text row is row index 3 now

    def _populate_fields(self):
        self.job_name_var.set(self.job_data_to_edit.get("name", ""))
        self.source_dir_var.set(self.job_data_to_edit.get("source_dir", ""))
        self.dest_base_var.set(self.job_data_to_edit.get("destination_base", ""))
        self.enabled_var.set(self.job_data_to_edit.get("enabled", True))
        self.schedule_var.set(self.job_data_to_edit.get("schedule", "manual"))
        self.volumes_override_var.set(self.job_data_to_edit.get("volumes_to_keep_override", 0))
        self.exclusions_text.delete("1.0", tk.END)
        self.exclusions_text.insert("1.0", "\n".join(self.job_data_to_edit.get("exclusions", [])))

    def _browse_source(self):
        d = filedialog.askdirectory(title="Select Source Directory", parent=self)
        if d: self.source_dir_var.set(d)

    def _browse_dest(self):
        d = filedialog.askdirectory(title="Select Destination Base Directory", parent=self)
        if d: self.dest_base_var.set(d)
        
    def _save_job(self):
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
            if volumes_override_val < 0: # Should be caught by spinbox, but double check
                messagebox.showerror("Validation Error", "Job-specific volumes to keep cannot be negative.", parent=self); return
            if volumes_override_val > 0:
                details["volumes_to_keep_override"] = volumes_override_val
            # If 0, key is not added, signifying global default
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
        self.parent=parent; self.title("Global Settings"); self.geometry("450x300"); self.transient(parent); self.grab_set()
        self.volumes_var=tk.IntVar(); self.base_name_var=tk.StringVar(); self.start_with_windows_var=tk.BooleanVar()
        main_frame = ttk.Frame(self, padding="15"); main_frame.pack(fill=tk.BOTH, expand=True)
        row_num = 0
        ttk.Label(main_frame,text="Default Volumes to Keep:").grid(row=row_num,column=0,sticky=tk.W,pady=8)
        self.volumes_spinbox = ttk.Spinbox(main_frame,from_=1,to=100,textvariable=self.volumes_var,width=10)
        self.volumes_spinbox.grid(row=row_num,column=1,sticky=tk.W,pady=8); row_num+=1
        ttk.Label(main_frame,text="Default Backup Base Name:").grid(row=row_num,column=0,sticky=tk.W,pady=8)
        self.base_name_entry = ttk.Entry(main_frame,textvariable=self.base_name_var,width=30)
        self.base_name_entry.grid(row=row_num,column=1,sticky=tk.EW,pady=8); row_num+=1
        self.start_with_windows_check = ttk.Checkbutton(main_frame,text="Start application when Windows starts",variable=self.start_with_windows_var)
        self.start_with_windows_check.grid(row=row_num,column=0,columnspan=2,sticky=tk.W,pady=15); row_num+=1
        buttons_frame = ttk.Frame(main_frame); buttons_frame.grid(row=row_num,column=0,columnspan=2,pady=30)
        ttk.Button(buttons_frame,text="Save",command=self._save_settings).pack(side=tk.LEFT,padx=10)
        ttk.Button(buttons_frame,text="Cancel",command=self.destroy).pack(side=tk.LEFT,padx=10)
        self._populate_fields(); main_frame.columnconfigure(1,weight=1)
    def _populate_fields(self):
        settings = current_config.get('global_settings',{})
        self.volumes_var.set(settings.get("default_volumes_to_keep",3))
        self.base_name_var.set(settings.get("default_backup_base_name","Backups_Py"))
        self.start_with_windows_var.set(check_if_in_startup())
    def _save_settings(self):
        try: volumes = self.volumes_var.get(); assert volumes >= 1
        except: messagebox.showerror("Error","Volumes must be >= 1.",parent=self); return
        base_name = self.base_name_var.get().strip()
        if not base_name: messagebox.showerror("Error","Base Name empty.",parent=self); return
        current_config['global_settings']['default_volumes_to_keep']=volumes
        current_config['global_settings']['default_backup_base_name']=base_name
        current_startup = check_if_in_startup(); desired_startup = self.start_with_windows_var.get()
        startup_ok = True
        if desired_startup and not current_startup:
            if not add_to_startup(): startup_ok=False; messagebox.showerror("Error","Failed to add to startup.",parent=self); self.start_with_windows_var.set(False)
        elif not desired_startup and current_startup:
            if not remove_from_startup(): startup_ok=False; messagebox.showerror("Error","Failed to remove from startup.",parent=self); self.start_with_windows_var.set(True)
        if save_config(current_config) and startup_ok:
            messagebox.showinfo("Success","Settings saved.",parent=self)
            if main_app_ref: main_app_ref.log_message_gui("Global settings updated.")
            self.destroy()
        elif not startup_ok: pass
        else: messagebox.showerror("Error","Failed to save config file.",parent=self)

# ==============================================================================
# 7. MAIN APPLICATION CLASS
# ==============================================================================
class BackupApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Solace Backup - v1.5") # Updated version
        self.root.geometry("900x600") # User's geometry

        # Initialize main_app_ref FIRST
        global main_app_ref
        main_app_ref = self

        # Style, log_queue, tray_icon - Initialize ONCE
        style = ttk.Style()
        try:
            style.theme_use('vista')
        except tk.TclError:
            style.theme_use('clam') # Fallback theme

        self.log_queue = queue.Queue()
        self.tray_icon = None

        # --- GUI Frames Setup ---
        paned_window = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))
        left_frame = ttk.Frame(paned_window, width=250, relief=tk.RIDGE)
        right_frame = ttk.Frame(paned_window, width=550, relief=tk.RIDGE)
        paned_window.add(left_frame, weight=1)
        paned_window.add(right_frame, weight=3)

        # Left Frame (Job List)
        left_frame.pack_propagate(False)
        ttk.Label(left_frame, text="Backup Jobs", font=("Arial", 12, "bold")).pack(pady=(10, 5))
        global job_listbox
        job_listbox = tk.Listbox(left_frame, selectmode=tk.SINGLE, exportselection=False)
        list_scrollbar = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=job_listbox.yview)
        job_listbox.config(yscrollcommand=list_scrollbar.set)
        list_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        job_listbox.pack(fill=tk.BOTH, expand=True, padx=(10,0), pady=5)
        job_buttons = ttk.Frame(left_frame)
        job_buttons.pack(pady=10)
        ttk.Button(job_buttons, text="Add", command=self.open_add_job_window).grid(row=0, column=0, padx=5)
        ttk.Button(job_buttons, text="Remove", command=self.remove_backup_job).grid(row=0, column=1, padx=5)
        ttk.Button(job_buttons, text="Edit", command=self.open_edit_job_window).grid(row=0, column=2, padx=5)

        # Right Frame (Logs)
        right_frame.pack_propagate(False)
        ttk.Label(right_frame, text="Status / Logs", font=("Arial", 12, "bold")).pack(pady=(10, 5))
        global log_text_widget
        log_text_widget = tk.Text(right_frame, wrap=tk.WORD, state=tk.DISABLED, relief=tk.SUNKEN, borderwidth=1)
        log_scrollbar = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=log_text_widget.yview)
        log_text_widget.config(yscrollcommand=log_scrollbar.set)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        log_text_widget.pack(fill=tk.BOTH, expand=True, padx=(0,10), pady=5)

        # Status Frame (Progress Bar)
        status_frame = ttk.Frame(self.root, relief=tk.RIDGE)
        status_frame.pack(fill=tk.X, padx=10, pady=(0, 5))
        self.status_label = ttk.Label(status_frame, text="Status: Idle", anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, padx=5, pady=2, fill=tk.X, expand=True)
        self.progress_bar = ttk.Progressbar(status_frame, orient='horizontal', length=200, mode='determinate')
        self.progress_bar.pack(side=tk.RIGHT, padx=5, pady=2)

        # Bottom Frame (Action Buttons)
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        ttk.Button(bottom_frame, text="Run Selected", command=self.run_selected_backup).pack(side=tk.LEFT, padx=10, pady=5)
        ttk.Button(bottom_frame, text="Run All", command=self.run_all_backups).pack(side=tk.LEFT, padx=5, pady=5)
        ttk.Button(bottom_frame, text="Settings", command=self.open_settings).pack(side=tk.RIGHT, padx=5, pady=5)
        ttk.Button(bottom_frame, text="View Log File", command=self.view_log_file).pack(side=tk.RIGHT, padx=5, pady=5)

        # --- SET THE WINDOW/TASKBAR ICON (Moved to after log_text_widget is defined) ---
        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(script_dir, "Solace_Backup.ico")             
            if os.path.exists(icon_path):
                self.root.iconbitmap(icon_path)
                logging.info(f"Window icon set to: {icon_path}")
            else:
                logging.warning(f"Window icon file not found: {icon_path}. Using default Tkinter icon.")
                self.log_message_gui(f"WARNING: Window icon file not found: {icon_path}")
        except tk.TclError as e:
            logging.error(f"Failed to set window icon: {e}. Ensure it's a valid .ico file.")
            self.log_message_gui(f"ERROR: Failed to set window icon. Check format/path. Details: {e}")
        except Exception as e:
            logging.error(f"An unexpected error occurred while setting window icon: {e}")
            self.log_message_gui(f"ERROR: Unexpected error setting window icon. Details: {e}")
        # ------------------------------------

        # --- Load Config & Start Queue Polling ---
        global current_config
        current_config = load_config()
        self.populate_job_list() # This uses log_message_gui indirectly if logging job list update
        self.root.after(100, self.process_log_queue)

        # --- Start Scheduler ---
        if APS_AVAILABLE and scheduler:
            try:
                load_all_jobs_to_scheduler(current_config, self.log_queue)
                scheduler.start()
                self.log_message_gui("Scheduler started.")
            except Exception as e:
                self.log_message_gui(f"ERROR starting scheduler: {e}")
        else:
            self.log_message_gui("Scheduler disabled (APScheduler not found or failed to start).")

        # --- Setup Tray & Close Intercept ---
        if TRAY_AVAILABLE:
            self.setup_tray_icon()
            self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        else:
            self.root.protocol("WM_DELETE_WINDOW", self.quit_application)
            self.log_message_gui("System Tray disabled (pystray/Pillow not found or failed to start).")
        
        self.log_message_gui("Application initialized.")

    def populate_job_list(self):
        job_listbox.delete(0, tk.END)
        if current_config and 'backup_jobs' in current_config:
            for job in current_config['backup_jobs']:
                status = " (Enabled)" if job.get('enabled', False) else " (Disabled)"
                schedule = job.get('schedule', 'manual')
                job_listbox.insert(tk.END, f"{job['name']}{status} [{schedule}]")
        logging.info("Job listbox updated.")

    def process_log_queue(self):
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

    def update_status_bar(self, job_name, step, total, message, indeterminate=False):
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

    def log_message_gui(self, message):
        now = datetime.now().strftime("%H:%M:%S")
        if log_text_widget:
            log_text_widget.config(state=tk.NORMAL)
            log_text_widget.insert(tk.END, f"[{now}] {message}\n")
            log_text_widget.see(tk.END); log_text_widget.config(state=tk.DISABLED)
        logging.info(message)

    def view_log_file(self):
        try: os.startfile(os.path.abspath(LOG_FILE))
        except Exception as e: messagebox.showerror("Error", f"Could not open log file: {e}")

    def open_add_job_window(self): JobEditorWindow(self.root)
    def open_edit_job_window(self):
        try:
            idx = job_listbox.curselection()[0]; name = job_listbox.get(idx).split(" (")[0].strip()
            job = next((j for j in current_config['backup_jobs'] if j['name'] == name), None)
            if job: JobEditorWindow(self.root, job_data=job, original_job_name=name)
            else: messagebox.showerror("Error", "Could not find job.")
        except IndexError: messagebox.showwarning("Warning", "Select a job.")

    def remove_backup_job(self):
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

    def run_selected_backup(self):
        try:
            idx = job_listbox.curselection()[0]; name = job_listbox.get(idx).split(" (")[0].strip()
            job = next((j for j in current_config['backup_jobs'] if j['name'] == name), None)
            if job:
                if not job.get('enabled'): messagebox.showwarning("Disabled", "Job disabled."); return
                self.log_message_gui(f"Queueing manual backup: {name}")
                threading.Thread(target=run_backup_job, args=(job, current_config['global_settings'], self.log_queue),
                                 daemon=True, name=f"Backup-{name}").start()
            else: messagebox.showerror("Error", "Could not find job.")
        except IndexError: messagebox.showwarning("Warning", "Select a job.")

    def run_all_backups(self):
        self.log_message_gui("--- Starting 'Run All Backups' ---")
        jobs = [j for j in current_config['backup_jobs'] if j.get('enabled')]
        if not jobs: self.log_message_gui("No enabled jobs."); return
        self.log_message_gui(f"Queueing {len(jobs)} jobs...")
        for job in jobs:
            self.log_message_gui(f"Queueing: {job['name']}")
            threading.Thread(target=run_backup_job, args=(job, current_config['global_settings'], self.log_queue),
                             daemon=True, name=f"Backup-{job['name']}").start()

    def open_settings(self): SettingsWindow(self.root)

    def setup_tray_icon(self):
        if not TRAY_AVAILABLE: return
        try:
            icon_path = "Solace_Backup_Tray.ico"
            script_dir = os.path.dirname(os.path.abspath(__file__))
            full_icon_path = os.path.join(script_dir, icon_path)

            if os.path.exists(full_icon_path): image = Image.open(full_icon_path)
            elif os.path.exists(icon_path): image = Image.open(icon_path); full_icon_path=icon_path # Check CWD as fallback
            else: raise FileNotFoundError(f"Icon '{icon_path}' not in script dir or CWD.")
            self.log_message_gui(f"Loaded custom tray icon: {full_icon_path}")
        except Exception as e:
            self.log_message_gui(f"WARNING: Icon load error ({e}). Using default.")
            logging.warning(f"Icon load error: {e}")
            image = create_image(64, 64, 'darkblue', 'lightblue')

        menu = (pystray.MenuItem('Show', self.show_window, default=True),
                pystray.MenuItem('Run All Now', self.run_all_backups),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Exit', self.quit_application))
        self.tray_icon = pystray.Icon("BackupSuite", image, "Python Backup Suite", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True, name="SystemTrayThread").start()

    def show_window(self): self.root.after(0, self.root.deiconify)
    def hide_window(self):
        self.root.withdraw()
        self.log_message_gui("Application hidden to system tray.")
        if TRAY_AVAILABLE and self.tray_icon:
             try: self.tray_icon.notify('Running in background', 'Backup Suite')
             except Exception as e: logging.warning(f"Tray notification error: {e}")

    def quit_application(self):
        self.log_message_gui("Shutting down...")
        if APS_AVAILABLE and scheduler and scheduler.running:
            try: scheduler.shutdown(wait=False)
            except Exception as e: logging.error(f"Error shutting scheduler: {e}")
        if TRAY_AVAILABLE and self.tray_icon:
            try: self.tray_icon.stop()
            except Exception as e: logging.error(f"Error stopping tray: {e}")
        self.root.destroy()
        logging.info("Application Exited."); os._exit(0)

# ==============================================================================
# 8. MAIN EXECUTION BLOCK
# ==============================================================================
if __name__ == "__main__":
    current_config = None
    main_app_ref = None
    job_listbox = None
    log_text_widget = None

    root = tk.Tk()
    app = BackupApp(root)
    root.mainloop()