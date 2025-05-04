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
