@echo off
setlocal enabledelayedexpansion

:: === Initialize Logging ===
set "log_file=%~dp0Debug\backup_debug.log"
echo === Backup Started: %DATE% %TIME% === > "%log_file%"
echo Script version: [V2.5 - Configurable Backup] >> "%log_file%"

echo Starting script... [V2.5 - Configurable Backup]

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
:: Remove trailing slash from grandparent dir path
set "grandparent_dir=%grandparent_dir:~0,-1%"
echo Grandparent Directory: "%grandparent_dir%"

:: Backup Folder definition moved below config reading
:: === Configuration Settings ===
set "config_file=%script_dir%Settings\config_settings.txt"
set "exclusion_file=%script_dir%Settings\backup_exclusions.txt"

:: Default backup retention if not configured
set "backup_volumes_to_keep=2"
set "backup_base_name=Backups" :: Default backup directory base name

:: Read config file if exists
if exist "%config_file%" (
    echo Reading configuration from "%config_file%" >> "%log_file%"
    for /f "usebackq tokens=1,* delims==" %%A in ("%config_file%") do (
        set "raw_key=%%A"
        set "raw_value=%%B"
        :: Enhanced trimming for key and value
        for /f "tokens=* delims= 	" %%K in ("!raw_key!") do set "key=%%K"
        for /f "tokens=* delims= 	" %%V in ("!raw_value!") do set "value=%%V"
        :: Remove any remaining leading/trailing spaces
        set "key=!key: =!"
        set "value=!value: =!"
        echo DEBUG: Raw config line: %%A=%%B >> "%log_file%"
        echo DEBUG: Trimmed Config Key: '!key!', Value: '!value!' >> "%log_file%"
        echo DEBUG: Trimmed Config Key: '!key!', Value: '!value!'
        
        :: Check if the key is one we care about and assign (case-insensitive)
        if /i "!key!"=="backup_volumes_to_keep" (
            echo DEBUG: Key 'backup_volumes_to_keep' found. Setting value to '!value!' >> "%log_file%"
            set "backup_volumes_to_keep=!value!"
        )
        if /i "!key!"=="backup_base_name" (
            echo DEBUG: Key 'backup_base_name' found. Setting value to '!value!' >> "%log_file%"
            set "backup_base_name=!value!"
        )
    )
)

echo Backup volumes to keep: !backup_volumes_to_keep! >> "%log_file%"
echo Exclusion list file: "%exclusion_file%"

:: Define Backup Folder (sibling to target_source_dir) using potentially configured name
set "backup_folder=%grandparent_dir%\!backup_base_name!"
:: Normalize path by replacing any double backslashes
set "backup_folder=!backup_folder:\\=\!"
echo Backup destination base folder: "%backup_folder%" using base name "!backup_base_name!" >> "%log_file%"
echo Backup destination base folder: "%backup_folder%" using base name "!backup_base_name!"

:: === Build Robocopy exclusion arguments ===
set "robocopy_xd_args="
set "robocopy_xf_args="

:: === Process Exclusion File ===
echo Processing exclusion file: "%exclusion_file%" >> "%log_file%"
if exist "%exclusion_file%" (
 echo Exclusion file found. Reading exclusions... >> "%log_file%"
 for /F "tokens=*" %%e in (%exclusion_file%) DO (
  echo Processing exclusion line: "%%e" >> "%log_file%"
  set "exclude_path=%%e"
  if not "!exclude_path!"=="" (
   echo Adding exclusion path: "!exclude_path!" >> "%log_file%"
   set "robocopy_xd_args=!robocopy_xd_args! /XD "!exclude_path!" "
  )
 )
) else (
 echo Exclusion file not found. Skipping exclusion processing. >> "%log_file%"
)
echo Final Robocopy Directory exclusions: %robocopy_xd_args% >> "%log_file%"
echo Final Robocopy File exclusions: %robocopy_xf_args% >> "%log_file%"


:: Create Backups folder if it doesn't exist
if not exist "%backup_folder%" (
    echo Creating backup base folder: "%backup_folder%"
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
set "copy_dir=%backup_folder%\Temp_Copy_%timestamp%" :: Consistent temp name

:: Prepare source path for Robocopy (Already done as target_source_dir)
set "robosource=%target_source_dir%"

:: Ensure the temporary copy directory doesn't exist from a previous failed run
if exist "%copy_dir%\" rmdir /S /Q "%copy_dir%"

:: --- Use Robocopy with specific progress display options ---
echo Copying files from "%robosource%" to "%copy_dir%" using Robocopy...
echo *** Robocopy progress: Shows directories, per-file %%, ETA ***
:: *** Added /XD "Backup_Suite" to always exclude the script's own folder ***
echo === Executing Robocopy command: === >> "%log_file%"
echo robocopy "%robosource%" "%copy_dir%" /E /COPY:DAT %robocopy_xd_args% %robocopy_xf_args% /XD "Backup_Suite" /R:1 /W:1 /NFL /NJH /NJS /ETA >> "%log_file%"
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
:: === Perform Cleanup Section ===
:perform_cleanup
echo Reached backup cleanup section...
echo Starting backup rotation check... >> "%log_file%"
echo Starting backup rotation check...

set "backup_count=0"
set "delete_count=0"
set "backups_to_delete="

:: Get list of backup files, sorted by name (which is by date)
for /f "delims=" %%f in ('dir /b /a-d "%backup_folder%\Backup_*.zip"') do (
    set /a backup_count+=1
    set "backups[!backup_count!]=%%f"
)

echo DEBUG: Found !backup_count! backup files. Volumes to keep: %backup_volumes_to_keep% >> "%log_file%"
echo DEBUG: Found !backup_count! backup files. Volumes to keep: %backup_volumes_to_keep%

:: Check if cleanup is needed
if !backup_count! GTR %backup_volumes_to_keep% (
    set /a delete_count=!backup_count! - %backup_volumes_to_keep%
    echo DEBUG: Need to delete !delete_count! backups. >> "%log_file%"
    echo DEBUG: Need to delete !delete_count! backups.

    :: Loop through backups to delete (oldest ones)
    for /l %%i in (1,1,!delete_count!) do (
        set "backup_file_name=!backups[%%i]!"
        set "backup_file_path=%backup_folder%\!backup_file_name!"
        echo DEBUG: Deleting backup: "!backup_file_path!" >> "%log_file%"
        echo Deleting old backup: "!backup_file_name!"

        del /f /q "!backup_file_path!" >nul 2>&1
        if !errorlevel! neq 0 (
            echo WARNING: Delete failed for "!backup_file_name!" with error code !errorlevel! >> "%log_file%"
        ) else (
            echo DEBUG: Deleted successfully: "!backup_file_name!" >> "%log_file%"
        )
    )
) else (
    echo DEBUG: No cleanup needed. Backup count is within limit. >> "%log_file%"
    echo DEBUG: No cleanup needed. Backup count is within limit.
)

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