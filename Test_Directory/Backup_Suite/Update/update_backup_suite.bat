@echo on
setlocal enabledelayedexpansion

:: Configuration
set "UPDATE_DIR=%~dp0"
set "PROJECT_DIR=%UPDATE_DIR%.."
set "LOG_FILE=%PROJECT_DIR%\Debug\update_log.txt"

:: Log start of update
echo [%date% %time%] - Update started. >> "%LOG_FILE%"

:: Step 1: Copy Settings and Debug directories from original Backup_Suite to new Backup_Suite in Update folder
robocopy "%PROJECT_DIR%\Settings" "%UPDATE_DIR%Backup_Suite\Settings" /e /v /copy:DAT /log+:"%LOG_FILE%"
robocopy "%PROJECT_DIR%\Debug" "%UPDATE_DIR%Backup_Suite\Debug" /e /v /copy:DAT /log+:"%LOG_FILE%"

:: Step 2: Copy all files and directories from new Backup_Suite in Update folder to original Backup_Suite directory
robocopy "%UPDATE_DIR%Backup_Suite" "%PROJECT_DIR%" /e /v /copy:DAT /purge /log+:"%LOG_FILE%"

echo [%date% %time%] - Update completed. >> "%LOG_FILE%"
echo Update completed.