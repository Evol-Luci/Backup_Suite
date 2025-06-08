<<<<<<< HEAD
# 🗃️ Backup Suite v2.5.0 - Simple Automated Backups

## ✅ Overview

The Backup Suite is a Windows-based automated backup solution designed to create organized backups of important files while allowing users to exclude unnecessary files. It's ideal for personal and project-based backups.

## 🚀 Key Features

- 📁 Automatic backup creation with timestamped ZIP files
- 🔧 Configurable backup retention (number of backups to keep)
- 📝 Exclusion list management for skipping unwanted files/directories
- 📊 Detailed logging for backup operations
- 🖥️ Simple interactive menu for managing exclusions and running backups

## 🚀 Quick Start Guide

1. **📋 Copy** the Backup_Suite folder into your project directory.
2. **🖱️ Double-click** `manage_exclusions.bat` to open the interactive menu.
3. Use the menu options to manage exclusions and run backups.

### 📝 Managing Exclusions

- `ADD [pattern]` - Add a file or directory to the exclusion list.
  - Examples: `ADD node_modules`, `ADD *.tmp`
- `REMOVE [pattern]` - Remove an exclusion pattern.
- `LIST` - View current exclusions and backup settings.

### ⏯️ Running Backups

- `RUN` - Execute the backup process manually.

## ⚙️ Configuration

Edit the `config_settings.txt` file in the Settings directory to change:

- `backup_volumes_to_keep` - Number of recent backups to retain.
- `backup_base_name` - Base name for the backup directory.

Example `config_settings.txt`:

```ini
# Backup Settings
backup_volumes_to_keep=5
backup_base_name=Backups
```

## 🔍 How it Works

1. The `backup.bat` script creates a backup of the parent directory.
2. It uses Robocopy to copy files, excluding patterns listed in `backup_exclusions.txt`.
3. The backup is saved as a timestamped ZIP file in the specified backup directory.
4. Old backups are automatically cleaned up based on the `backup_volumes_to_keep` setting.

## 📋 Logging

- Backup operations are logged in `backup_debug.log` (inside the Debug folder).
- Exclusion management actions are logged in `manage_exclusions_debug.log` (inside the Debug folder).

## 💡 Basic Usage Walkthrough

### Making Your First Backup  

Simply:  

1. Double click `backup.bat`
2. Find your backup in the new `Backups` folder

or

1. Open `manage_exclusions.bat`
2. Add any exclusions
3. Configure Settings  
4. Press `R` then Enter to run a backup  
5. Find your backup in the new folder of your chosen name  

### Managing What Gets Backed Up

Common commands:  

```
ADD node_modules   ← Exclude directory
ADD *.tmp          ← Skip all .tmp files
REMOVE node_modules ← Remove exclusion
LIST             ← View exclusions
```

## ❓ Common Questions

**Q: How often should I run backups?**  
A: Weekly for personal files, daily for important projects  

**Q: Can I edit backups after creating them?**  
A: Yes! They're standard ZIP files - open with any ZIP utility  

## 📋 Pro Tips

• Press `TAB` to auto-complete filenames  
• Use `HELP` in the menu for instant guidance  
• Backups are named with date/time automatically  

## 🛠️ Troubleshooting

1. Check the relevant log file for error messages.
2. Verify that the Backup_Suite folder has appropriate permissions.
3. Ensure the backup destination has sufficient disk space.

## 🛠️ Need Help?

Put in an Issue on github (https://github.com/Evol-Luci/Backup_Suite/issues) with:  

1. Your `backup_debug.log` file  
2. What operation you were attempting  
3. Any error messages seen

# Solace Backup - GUI Edition

Solace Backup is a user-friendly graphical application for Windows designed to simplify the process of creating and managing file backups. It leverages robust tools like Robocopy for file transfer and PowerShell for archive creation, providing a reliable backup solution with scheduling, retention policies, and a customizable interface.

## Features

Solace Backup offers a comprehensive set of features to manage your backups effectively:

*   **Graphical User Interface:** Intuitive Tkinter-based UI for easy management of backup jobs and settings.
*   **Job Management:**
    *   Create, edit, and delete backup jobs.
    *   Define job names, source directories, and destination base folders.
    *   Specify file/folder exclusions for each job (e.g., `.venv`, `node_modules`).
*   **Flexible Scheduling:**
    *   Manual backup execution.
    *   Scheduled backups:
        *   Daily at a specific time (e.g., `daily@06:00`).
        *   Interval-based (e.g., every X minutes).
        *   Weekly on a specific day and time (e.g., `weekly@sun:14:30`).
*   **Retention Policies:**
    *   Set a global default for the number of backup versions to keep.
    *   Override the global setting with a job-specific number of backup archives to retain.
    *   Automatic cleanup of old backups.
*   **Robust Backup Operations:**
    *   Uses **Robocopy** for efficient and reliable file/folder copying (supports copying data, attributes, timestamps).
    *   Uses **PowerShell (`Compress-Archive`)** to create ZIP archives of backups.
*   **Theming:**
    *   Supports multiple UI themes (e.g., Light, Dark Mode) for user preference.
    *   Theme selection is saved in global settings.
*   **System Tray Integration:**
    *   Minimizes to the system tray.
    *   Quick actions from tray icon: Show application, Run All Backups, Exit.
    *   Tray notifications for background operations.
*   **Windows Startup Option:**
    *   Configure the application to start automatically when Windows boots up.
*   **Logging:**
    *   Detailed debug logging to `GUIBackup/Debug/backup_suite_debug.log`.
    *   Real-time status updates and logs displayed within the application's UI.
*   **Configuration:**
    *   Backup jobs and global settings are stored in a human-readable JSON file (`GUIBackup/Settings/backup_config.json`), though typically managed via the UI.
*   **User Interface Conveniences:**
    *   Progress bar for ongoing backup operations.
    *   Paned window layout to resize job list and log areas.
    *   Direct button to view the debug log file.

## Requirements

To run Solace Backup, you'll need the following:

*   **Operating System:** Windows (due to reliance on Robocopy and PowerShell).
*   **Python:** Python 3.x (developed with Python 3.12 in mind, but should be compatible with recent Python 3 versions).
*   **Python Libraries:**
    *   `tkinter`: Usually included with standard Python installations on Windows.
    *   `apscheduler`: For scheduling backup jobs. Install via pip: `pip install apscheduler`
    *   `pystray`: For system tray icon functionality. Install via pip: `pip install pystray`
    *   `Pillow`: For image manipulation for the system tray icon. Install via pip: `pip install Pillow`
*   **External Tools:**
    *   **Robocopy:** A robust file copy utility. It is a standard command-line tool available in modern Windows versions (Vista and newer).
    *   **PowerShell:** Used for creating ZIP archives. PowerShell is included by default in modern Windows versions.

## Installation

1.  **Clone the Repository:**
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```
    (Replace `<repository_url>` and `<repository_directory>` with the actual URL and folder name.)

2.  **Ensure Python is Installed:**
    If you don't have Python, download and install the latest Python 3.x version from [python.org](https://www.python.org/). Make sure to check the option "Add Python to PATH" during installation.

3.  **(Optional but Recommended) Create a Virtual Environment:**
    Open a terminal or command prompt in the project's root directory (`<repository_directory>`) and run:
    ```bash
    python -m venv venv
    ```
    Activate the virtual environment:
    *   On Windows:
        ```bash
        venv\Scripts\activate
        ```
    *   On macOS/Linux (though the app is Windows-focused, for development):
        ```bash
        source venv/bin/activate
        ```

4.  **Install Required Python Packages:**
    With your virtual environment activated (if you created one), install the necessary libraries:
    ```bash
    pip install apscheduler pystray Pillow
    ```

5.  **Verify External Tools:**
    *   **Robocopy:** Open Command Prompt and type `robocopy /?`. If it shows help information, it's available.
    *   **PowerShell:** Open Command Prompt and type `powershell -Command "Get-Host"`. If it shows PowerShell version information, it's available.
    (These are typically pre-installed on modern Windows systems.)

## How to Use

### Launching the Application

1.  Navigate to the `GUIBackup` directory within the project folder.
2.  Run the main application script using Python:
    ```bash
    python backup_suite.pyw
    ```
    (If you created a virtual environment, ensure it's activated.)

### Main Window Overview

The main application window is divided into a few key areas:

*   **Job List (Left Pane):**
    *   Displays all configured backup jobs.
    *   Shows job name, enabled/disabled status, and schedule.
    *   Select a job here to act upon it (Remove, Edit, Run Selected).
*   **Status / Logs (Right Pane):**
    *   Displays real-time log messages from the application and backup operations.
    *   Useful for monitoring progress and diagnosing issues.
*   **Job Management Buttons (Below Job List):**
    *   **Add:** Opens the "Add/Edit Backup Job" window to create a new job.
    *   **Remove:** Deletes the selected backup job (after confirmation).
    *   **Edit:** Opens the "Add/Edit Backup Job" window to modify the selected job.
*   **Action Buttons (Bottom Bar):**
    *   **Run Selected:** Manually starts the backup job currently selected in the list.
    *   **Run All:** Manually starts all *enabled* backup jobs.
    *   **View Log File:** Opens the `backup_suite_debug.log` file in your default text editor.
    *   **Settings:** Opens the "Global Settings" window.
*   **Status Bar (Bottom):**
    *   Shows the current status of the application (e.g., "Idle", "Copying files...").
    *   A progress bar indicates the progress of ongoing backup tasks.

### Adding/Editing a Backup Job

Clicking "Add" or "Edit" opens the job editor window with the following fields:

*   **Job Name:** A unique name for your backup job (e.g., "My Documents Backup").
*   **Source Directory:** The folder you want to back up. Click "Browse..." to select it.
*   **Destination Base:** The parent folder where backup archives for this job will be stored. A subfolder named after the job and timestamp will typically be created here for each backup. Click "Browse..." to select it.
*   **Exclusions (one per line):** List any subdirectories or files within the source directory that you want to *exclude* from the backup (e.g., `node_modules`, `__pycache__`, `*.tmp`). These are passed to Robocopy's `/XD` flag.
*   **Backups to Keep (Job Specific):** Specify how many recent backup archives (ZIP files) to keep for this particular job. If set to `0`, the global default retention policy will be used.
*   **Enabled:** Check this box to enable the job. Disabled jobs will not run automatically (scheduled) or when "Run All" is clicked, but can still be run manually via "Run Selected".
*   **Schedule:** Define the schedule for automatic backups:
    *   `manual`: No automatic scheduling.
    *   `daily@HH:MM`: Runs daily at the specified hour and minute (24-hour format, e.g., `daily@23:00`).
    *   `interval@MMM`: Runs every MMM minutes (e.g., `interval@60` for every hour).
    *   `weekly@DDD:HH:MM`: Runs on a specific day of the week (sun, mon, tue, wed, thu, fri, sat) at the specified time (e.g., `weekly@sun:02:30`).

### Global Settings

Accessed via the "Settings" button on the main window:

*   **Default Volumes to Keep:** The default number of backup archives to retain if a job-specific value is not set (or set to 0).
*   **Default Backup Base Name:** (Note: This setting appears in `backup_config.json` but its direct use in the GUI or backup naming convention isn't immediately obvious from the code. It might be a legacy setting or for future use. Job names primarily define backup archive names.)
*   **Application Theme:** Choose between available themes (e.g., "Light (Default)", "Dark Mode") for the application's appearance.
*   **Start application when Windows starts:** If checked, Solace Backup will be added to the Windows startup registry and launch automatically when you log in.

### System Tray Icon

If enabled (dependencies installed), Solace Backup will minimize to the system tray instead of closing:

*   **Left-click:** Shows the main application window.
*   **Right-click:** Opens a context menu:
    *   **Show:** Restores the application window.
    *   **Run All Now:** Triggers all enabled backup jobs immediately.
    *   **Exit:** Shuts down the application.

The application may also show notifications from the tray icon (e.g., when hidden).

## Configuration File

Solace Backup stores its settings and job definitions in a JSON file located at:

`GUIBackup/Settings/backup_config.json`

While all settings can be managed through the application's graphical interface, this file can be manually inspected or backed up if needed. It's generally recommended to make changes via the UI to avoid formatting errors.

The file structure includes:
*   `global_settings`: Contains defaults like `default_volumes_to_keep` and the selected `theme`.
*   `backup_jobs`: An array of all defined backup jobs, each with its specific parameters (name, source, destination, exclusions, schedule, etc.).

## Logging

The application maintains a detailed debug log which can be helpful for troubleshooting:

*   **Location:** `GUIBackup/Debug/backup_suite_debug.log`
*   **Content:** Records application startup, backup job execution details (Robocopy and PowerShell commands, progress), scheduler actions, errors, and other diagnostic information.
*   **Access:** You can view this log file directly using any text editor, or by clicking the "View Log File" button in the application's main window.

Additionally, the "Status / Logs" panel within the application provides a real-time view of important log messages and status updates during operation.

## Troubleshooting

*   **`ModuleNotFoundError: No module named 'apscheduler'` (or `pystray`, `PIL`)**
    *   **Cause:** A required Python library is not installed.
    *   **Solution:** Open your terminal or command prompt (activate your virtual environment if you use one) and install the missing package using pip:
        ```bash
        pip install apscheduler
        # or
        pip install pystray
        # or
        pip install Pillow
        ```
        You can install all at once: `pip install apscheduler pystray Pillow`

*   **Application Fails to Start / Errors on Launch:**
    *   **Cause:** Could be due to various reasons, including corrupted configuration or missing Python/system dependencies.
    *   **Solution:**
        1.  Check the `GUIBackup/Debug/backup_suite_debug.log` for detailed error messages.
        2.  Ensure Python is correctly installed and accessible in your system's PATH.
        3.  Verify that all required libraries (see "Requirements") are installed.
        4.  If you suspect a corrupted `backup_config.json`, you can try renaming or deleting it (the application will create a new default one on next startup, but you will lose your job configurations). The application also attempts to back up corrupted configs with a `.bak_YYYYMMDDHHMMSS` extension.

*   **Robocopy or PowerShell Errors:**
    *   **Cause:** While standard on Windows, there could be issues with their paths or execution policies (especially for PowerShell).
    *   **Solution:**
        1.  Ensure you are running on a supported version of Windows.
        2.  Check the application's log for specific error messages from Robocopy or PowerShell.
        3.  You can test Robocopy and PowerShell independently in Command Prompt or PowerShell terminal to see if they function correctly. For example, type `robocopy /?` or `Get-Host`.

*   **System Tray Icon Not Appearing:**
    *   **Cause:** The `pystray` or `Pillow` library might be missing or there was an issue loading the icon image.
    *   **Solution:**
        1.  Ensure `pystray` and `Pillow` are installed (`pip install pystray Pillow`).
        2.  Check the application logs for any errors related to icon loading. The application will attempt to use a default icon if the custom ones (`Solace_Backup_Tray.ico`, `Solace_Backup_Tray.png`) are not found in the `GUIBackup` directory.

*   **Scheduled Jobs Not Running:**
    *   **Cause:** The job might be disabled, the schedule string might be incorrect, or `apscheduler` might have issues.
    *   **Solution:**
        1.  Verify the job is "Enabled" in the job editor.
        2.  Double-check the schedule string format (e.g., `daily@HH:MM`, `interval@MMM`, `weekly@DDD:HH:MM`).
        3.  Check the application log for any errors from the scheduler.
        4.  Ensure the application is running. If the application (or its tray icon) is closed, the scheduler will not be active. Consider using the "Start with Windows" option if you need schedules to run reliably after a reboot.

## Contributing

Contributions are welcome! If you'd like to improve Solace Backup or add new features, please feel free to:

1.  Fork the repository.
2.  Create a new branch for your feature or bug fix.
3.  Make your changes.
4.  Submit a pull request with a clear description of your changes.

(Please provide more specific contribution guidelines if you have them.)

## License

This project is currently not under a specific license. Please specify a license if you wish to distribute it under particular terms.

(Consider adding an open-source license like MIT, Apache 2.0, or GPLv3. You can create a `LICENSE` file in the repository root and reference it here.)
>>>>>>> ae2b2cd (Jules was unable to complete the task in time. Please review the work done so far and provide feedback for Jules to continue.)
