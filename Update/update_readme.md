# Backup Suite Update Process

## Overview

The Backup Suite project includes an update mechanism to simplify the process of updating the project with new versions while preserving configuration files and settings.

## Update Directory Structure

To update the Backup Suite project, place the new version of the project inside the `Update` directory. The `Update` directory should be located in the root of your Backup Suite project.

## Update Script

The update process is managed by the `update_backup_suite.bat` script, which is located in the `Update` directory. If you don't have an `Update` directory in your root simply copy the one in the new update into your `Backup_Suite` root, This script performs the following tasks:

1. **Copy New Files**: Copies new or updated files from the new version of the project to the Backup Suite project directory, overwriting existing files where necessary.
2. **Preserve Configuration Files**: Ensures that configuration files (`backup_exclusions.txt` and `config_settings.txt`) are not overwritten during the update process.
3. **Cleanup**: Removes any files and directories from the `Update` directory that are not part of the update script itself.

## Using the Update Script

1. Place the new version of the Backup Suite project inside the `Update` directory inside of your `Backup_Suite` root directory.
2. If you do not have an `Update` Directory simply copy the one from the update into your `Backup_Suite` Directory
3. Run `update_backup_suite.bat` from your root `Backup_Suite` Directory.

The script will update the Backup Suite project and clean up the `Update` directory.

## Notes

- Ensure that the new version of the Backup Suite project is correctly structured and placed inside the `Update` directory.
- The update script assumes that configuration files are named `backup_exclusions.txt` and `config_settings.txt` and are located in the `Settings` directory of the Backup Suite project.

## Update Log and Debugging

You can find the update logs in the `Debug` folder.

## Robocopy Logging

Robocopy provides logging that can be useful for debugging:
It produces verbose output, displaying all files and directories processed.

