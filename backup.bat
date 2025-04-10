@echo off
setlocal enabledelayedexpansion

:: === Initialize Logging ===
set "log_file=%~dp0backup_debug.log"
echo === Backup Started: %DATE% %TIME% === > "%log_file%"
echo Script version: [V11 - Configurable Backup] >> "%log_file%"

echo Starting script... [V11 - Configurable Backup]

:: === Path Calculations ===
set "script_dir=%~dp0"
echo Script directory (Backup_Suite): "%script_dir%"

:: Calculate the Target Source Directory (Parent of script_dir)
set "target_source_dir=%script_dir:~0,-1%"
for %%A in ("%target_source_dir%") do set "target_source_dir=%%~dpA"
:: Remove trailing slash from target source dir path
set "target_source_dir=%target_source_dir:~0,-1%"
echo Target Source Directory (Parent): "%target_source_dir%"

:: Calculate the Grandparent Directory (Parent of target_source_dir)
set "grandparent_dir=%target_source_dir%"
for %%A in ("%grandparent_dir%") do set "grandparent_dir=%%~dpA"
echo Grandparent Directory: "%grandparent_dir%"

:: Define Backup Folder (sibling to target_source_dir)
set "backup_folder=%grandparent_dir%Backups"
echo Backup destination base folder: "%backup_folder%"

:: === Configuration Settings ===
set "config_file=%script_dir%config_settings.txt"
set "exclusion_file=%script_dir%backup_exclusions.txt"

:: Default backup retention if not configured
set "backup_volumes_to_keep=2"

:: Read config file if exists
if exist "%config_file%" (
    echo Reading configuration from "%config_file%" >> "%log_file%"
    for /f "tokens=1,2 delims==" %%A in ('type "%config_file%" ^| findstr /i "backup_volumes_to_keep"') do (
        set "%%A=%%B"
    )
)

echo Backup volumes to keep: %backup_volumes_to_keep% >> "%log_file%"
echo Exclusion list file: "%exclusion_file%"

:: === Build Robocopy exclusion arguments ===
set "robocopy_xd_args="
set "robocopy_xf_args="
if exist "%exclusion_file%" (
    echo Reading exclusions from "%exclusion_file%"...
    for /f "usebackq delims=" %%L in ("%exclusion_file%") do (
        echo Processing exclusion pattern: "%%L" >> "%log_file%"
        echo Adding exclusion from file: "%%L"
        :: Check if pattern ends with / or \ (directory)
        echo %%L|findstr /r "[\\/]$" >nul
        if !errorlevel! equ 0 (
            set "robocopy_xd_args=!robocopy_xd_args! /XD "%%L""
            echo Classified as directory exclusion >> "%log_file%"
        ) else (
            set "robocopy_xf_args=!robocopy_xf_args! /XF "%%L""
            echo Classified as file exclusion >> "%log_file%"
        )
    )
) else (
    echo Exclusion file not found. Proceeding without file-based exclusions. >> "%log_file%"
)
echo Final Robocopy Directory exclusions: %robocopy_xd_args% >> "%log_file%"
echo Final Robocopy File exclusions: %robocopy_xf_args% >> "%log_file%"


:: Create Backups folder if it doesn't exist
if not exist "%backup_folder%" (
    echo Creating Backups folder: "%backup_folder%"
    mkdir "%backup_folder%"
    if errorlevel 1 (
      echo ERROR: Failed to create backup folder. Check permissions. >> "%log_file%"
      echo ERROR: Failed to create backup folder. Check permissions.
      pause
      goto :eof_error
    ) else (
      echo Successfully created backup folder >> "%log_file%"
    )
)

:: Get the current date and time (formatted)
set "datetime="
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set datetime=%%I
if not defined datetime (
    echo ERROR: Failed to get date/time from WMIC. >> "%log_file%"
    echo ERROR: Failed to get date/time from WMIC.
    pause
    goto :eof_error
) else (
    echo Timestamp acquired: %datetime% >> "%log_file%"
)
set "YYYY=!datetime:~0,4!"
set "MM=!datetime:~4,2!"
set "DD=!datetime:~6,2!"
set "HH=!datetime:~8,2!"
set "MIN=!datetime:~10,2!"
set "SS=!datetime:~12,2!"
set "timestamp=%YYYY%-%MM%-%DD%_%HH%-%MIN%-%SS%"

:: Create backup name with timestamp (Using script name, which is now 'backup')
set "zip_file=%backup_folder%\Backup_%timestamp%.zip"
set "copy_dir=%backup_folder%\backup_copy_%timestamp%" :: Simpler temp name

:: Prepare source path for Robocopy (Already done as target_source_dir)
set "robosource=%target_source_dir%"

:: Ensure the temporary copy directory doesn't exist from a previous failed run
if exist "%copy_dir%\" rmdir /S /Q "%copy_dir%"

:: --- Use Robocopy with specific progress display options ---
echo Copying files from "%robosource%" to "%copy_dir%" using Robocopy...
echo *** Robocopy progress: Shows directories, per-file %%, ETA ***
:: *** Added /XD "Backup_Suite" to always exclude the script's own folder ***
robocopy "%robosource%" "%copy_dir%" /E /COPY:DAT %robocopy_xd_args% %robocopy_xf_args% /XD "Backup_Suite" /R:1 /W:1 /NFL /NJH /NJS /ETA
set RBERROR=%ERRORLEVEL%
echo *** Robocopy finished with Exit Code: %RBERROR% ***

:: --- Check Robocopy Result using GOTO ---
if %RBERROR% GTR 1 goto :robocopy_failed
goto :robocopy_ok

:robocopy_failed
    echo ERROR: Robocopy failed (Code %RBERROR%). See Robocopy docs for error meanings. >> "%log_file%"
    echo ERROR: Robocopy failed or had issues (Code %RBERROR%). Check Robocopy documentation for code meanings.
    if exist "%copy_dir%\" (
        echo Cleaning up failed copy directory >> "%log_file%"
        rmdir /S /Q "%copy_dir%"
    )
    pause
    goto :eof_error

:robocopy_ok
    echo Robocopy reported success (Code %RBERROR% ^<= 1^) - Proceeding to zip.


:: --- Explicit Zipping Section ---
echo *** STEP: Preparing to Zip ***
echo    Source for Zip: "%copy_dir%\*"
echo    Destination Zip: "%zip_file%"
powershell Compress-Archive -Path "%copy_dir%\*" -DestinationPath "%zip_file%" -Force -ErrorAction SilentlyContinue
set ZIPERROR=%ERRORLEVEL%
echo *** STEP: PowerShell Compress-Archive finished with Exit Code: %ZIPERROR% ***

:: --- Check Zipping Result ---
if %ZIPERROR% neq 0 goto :zip_failed
goto :zip_ok

:zip_failed
    echo ERROR: PowerShell zip failed (Code %ZIPERROR%). Check disk space/permissions. >> "%log_file%"
    echo ERROR: Failed to create zip file with PowerShell. Exit Code %ZIPERROR%.
    if exist "%copy_dir%\" (
        echo Cleaning up after zip failure >> "%log_file%"
        rmdir /S /Q "%copy_dir%"
    )
    pause
    goto :eof_error

:zip_ok
    echo Zipping PowerShell command reported success (Exit Code 0).
    echo Now checking if the file actually exists: !zip_file!
    if exist "!zip_file!" goto :zip_verify_ok
    goto :zip_verify_failed

:zip_verify_failed
    echo CRITICAL ERROR: Zip file missing after successful creation! Check disk space. >> "%log_file%"
    echo CRITICAL ERROR: Zip file "!zip_file!" NOT found despite PowerShell success! Check disk space/permissions.
    pause
    goto :eof_error

:zip_verify_ok
    echo SUCCESS: Zip file created at "!zip_file!" >> "%log_file%"
    echo SUCCESS: Zip file "!zip_file!" found.


:: --- Explicit Delete Temp Folder Section ---
echo *** STEP: Preparing to delete temporary folder "%copy_dir%" ***
rmdir /S /Q "%copy_dir%"
echo *** STEP: rmdir command executed. Checking if directory still exists... ***
if exist "%copy_dir%\" goto :delete_temp_failed
goto :delete_temp_ok

:delete_temp_failed
    echo WARNING: Could not delete temp folder "%copy_dir%". Continuing... >> "%log_file%"
    echo ERROR: Failed to delete temporary folder "%copy_dir%". Check permissions or file locks.
    pause
    goto :perform_cleanup

:delete_temp_ok
    echo Temporary folder cleanup completed >> "%log_file%"
    echo Temporary folder deleted successfully.


:: === Perform Cleanup Section ===
:perform_cleanup
echo Reached backup cleanup section...

:: Count files using a separate command to avoid loop issues
set "backup_pattern=%backup_folder%\Backup_*.zip"
set count=0
for /f %%C in ('dir /b "%backup_pattern%" 2^>nul ^| find /c /v ""') do set count=%%C
echo Found %count% backup files matching pattern in "%backup_folder%"

:: If there are more than configured backups, delete the oldest ones
set /a "delete_threshold=%backup_volumes_to_keep%"
echo Checking if count (%count%) is greater than %delete_threshold%... >> "%log_file%"
echo Checking if count (%count%) is greater than %delete_threshold%...
if %count% GTR %delete_threshold% goto :delete_oldest_check
goto :delete_oldest_skip

:delete_oldest_check
    echo More than 2 backups found. Identifying oldest...
    set "oldest_backup="
    for /f "delims=" %%F in ('dir /b /o:d "%backup_pattern%" 2^>nul') do (
        set "oldest_backup=%%F"
        goto :got_oldest_backup_v10
    )
    :got_oldest_backup_v10
    if defined oldest_backup goto :delete_oldest_do
    goto :delete_oldest_not_found

:delete_oldest_not_found
    echo WARN: Could not identify oldest backup file to delete (Count was %count%).
    goto :delete_oldest_done

:delete_oldest_do
    echo Oldest backup identified as: !oldest_backup! >> "%log_file%"
    set "file_to_delete=%backup_folder%\!oldest_backup!"
    echo Preparing to delete: "!file_to_delete!" >> "%log_file%"
    echo Preparing to delete: "!file_to_delete!"
    del "!file_to_delete!"
    if errorlevel 1 goto :delete_oldest_failed
    goto :delete_oldest_success

:delete_oldest_failed
    echo ERROR: Failed to delete old backup (Code %ERRORLEVEL%). File may be locked. >> "%log_file%"
    echo ERRORLEVEL %ERRORLEVEL%: Failed to delete "!file_to_delete!". Check permissions or lock.
    goto :delete_oldest_done

:delete_oldest_success
    echo Successfully rotated old backup: "!file_to_delete!" >> "%log_file%"
    echo Successfully deleted "!file_to_delete!".
    goto :delete_oldest_done

:delete_oldest_skip
    echo Backup count %count% is not greater than %delete_threshold%. No deletion needed. >> "%log_file%"
    echo Backup count %count% is not greater than %delete_threshold%. No deletion needed.

:delete_oldest_done
    echo Finished backup rotation check.


echo Backup process complete. Result: %zip_file% >> "%log_file%"
echo Backup process complete. Last operation resulted in: %zip_file%

:final_pause
echo === Script Finished ===
pause
endlocal
goto :eof

:eof_error
echo --- SCRIPT ENDED DUE TO ERROR --- >> "%log_file%"
echo --- SCRIPT ENDED DUE TO ERROR ---
pause
endlocal
goto :eof