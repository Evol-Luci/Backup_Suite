# 🗃️ Backup Suite v1.0 - Simple Automated Backups

## ✅ Before You Begin
- [ ] You have Windows 10/11
- [ ] Files to backup are in one main folder
- [ ] You have at least 1GB free disk space

## 🚀 Quick Start Guide

1. **Copy** the Backup_Suite folder into your project  
2. **Double-click** `manage_exclusions.bat` (the "Control Panel")  
3. Follow the simple menu (use ↑/↓ arrows if available)

## 🔍 What Does This Do?
Automatically creates organized backups while letting you exclude unnecessary files - like a smart digital filing assistant.

Key Features:  
✓ **Automatic cleanup** - keeps only recent backups  
✓ **Easy exclusions** - skip temporary/unimportant files  
✓ **One-click restore** - standard ZIP format  
✓ **Detailed logging** - tracks all operations  

## 🗂️ Files Explained

### Main Control Files:
| File | Purpose | Best For |
|------|---------|----------|
| 📋 `manage_exclusions.bat` | Friendly control panel | Daily use |
| ⚙️ `backup.bat` | Backup engine | Advanced automation |
| ✏️ `config_settings.txt` | Settings | Customizing behavior |
| ✂️ `backup_exclusions.txt` | Exclusion list | Quick edits |

## 💡 Basic Usage Walkthrough

### Making Your First Backup  
Simply:  
1. Open `manage_exclusions.bat`  
2. Press `R` then Enter to run a backup  
3. Find your backup in the new `Backups` folder  

### Managing What Gets Backed Up
Common commands:  
```
ADD node_modules   ← Exclude directory
ADD *.tmp          ← Skip all .tmp files
REMOVE node_modules ← Remove exclusion
LIST             ← View exclusions
```

## ⚙️ Configuration Made Simple

Edit `config_settings.txt` to change:
```ini
### Backup Settings  
backup_volumes_to_keep=5  ← Keep 5 most recent backups  

### Files/Folders to Exclude:  
- temp/          ← Folder exclusion  
- cache/*.tmp    ← Complex pattern  
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

## 🛠️ Need Help?
Contact support with:  
1. Your `backup_debug.log` file  
2. What operation you were attempting  
3. Any error messages seen