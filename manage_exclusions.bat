@echo off
setlocal enabledelayedexpansion

:: Define log file
set "debug_log=%~dp0manage_exclusions_debug.log"
echo == Starting manage_exclusions.bat @ %TIME% == > "%debug_log%"

:: Define the exclusion list file and the backup script
set "exclusion_file=%~dp0backup_exclusions.txt"
set "backup_script=%~dp0backup.bat"
set "temp_file=%exclusion_file%.tmp"
echo Exclusion file: %exclusion_file% >> "%debug_log%"
echo Backup script: %backup_script% >> "%debug_log%"


:: Ensure the exclusion file exists, create if not
if not exist "%exclusion_file%" (
    echo Creating empty exclusion file: "%exclusion_file%" >> "%debug_log%"
    echo Creating empty exclusion file: "%exclusion_file%"
    type nul >"%exclusion_file%"
    if errorlevel 1 (
      echo ERROR: Could not create exclusion file. Check permissions. >> "%debug_log%"
      echo ERROR: Could not create exclusion file. Check permissions.
      pause
      goto :eof
    )
)

:menu
echo ==============================================
echo  Manage Backup Exclusions - Interactive Menu
echo  Config: "%exclusion_file%"
echo ==============================================
echo [ACTIONS]
echo   ADD [pattern]    - Add exclusion pattern
echo   REMOVE [pattern] - Remove exclusion pattern
echo   LIST             - List current exclusions
echo   RUN              - Run backup now
echo   HELP             - Show usage instructions
echo   EXIT             - Quit this menu
echo.
echo [PATTERN EXAMPLES]
echo   ADD node_modules/  - Exclude directory
echo   ADD *.tmp          - Exclude all .tmp files
echo   ADD build\         - Exclude build directory
echo ==============================================
echo ===================================
echo.
set "user_input="
set "command="
set "item="
set /p user_input="> Enter action: "
echo User input: "%user_input%" >> "%debug_log%"

if not defined user_input goto :menu

:: Parse the input: command is the first word, item is the rest
for /f "tokens=1,*" %%A in ("%user_input%") do (
    set "command=%%A"
    set "item=%%B"
)
echo Parsed command: "%command%", item: "!item!" >> "%debug_log%"

:: Explicitly check if command is valid before proceeding
set "valid_command=0"
if /i "%command%"=="ADD" set "valid_command=1"
if /i "%command%"=="EXCLUDE" set "valid_command=1" & set "command=ADD"
if /i "%command%"=="REMOVE" set "valid_command=1"
if /i "%command%"=="INCLUDE" set "valid_command=1" & set "command=REMOVE"
if /i "%command%"=="HELP" set "valid_command=1"
if /i "%command%"=="LIST" set "valid_command=1"
if /i "%command%"=="RUN" set "valid_command=1"  :: Added RUN
if /i "%command%"=="BACKUP" set "valid_command=1" & set "command=RUN" :: Synonym
if /i "%command%"=="EXIT" set "valid_command=1"
if /i "%command%"=="QUIT" set "valid_command=1" & set "command=EXIT"

if "%valid_command%"=="0" (
    echo Invalid command "%command%". Please use ADD, REMOVE, LIST, RUN, or EXIT. >> "%debug_log%"
    echo Invalid command "%command%". Please use ADD, REMOVE, LIST, RUN, or EXIT.
    echo.
    pause
    goto :menu
)

:: Route valid commands
echo Routing valid command "%command%" with item "!item!" >> "%debug_log%"
if /i "%command%"=="ADD" goto :add_action
if /i "%command%"=="REMOVE" goto :remove_action
if /i "%command%"=="LIST" goto :list_action
if /i "%command%"=="RUN" goto :run_backup_action  :: Added RUN route
if /i "%command%"=="HELP" goto :help_action
if /i "%command%"=="EXIT" goto :quit

echo Internal routing error for command "%command%" >> "%debug_log%"
echo Internal routing error.
pause
goto :menu


:: ================== LIST Action (Corrected with GOTO) ==================
:list_action
echo DEBUG: Reached list_action >> "%debug_log%"
echo --- Current Exclusions ---
if not exist "%exclusion_file%" goto :list_action_file_not_found

:: Check file size to determine emptiness reliably
set "file_is_empty=1"
for %%Z in ("%exclusion_file%") do if %%~zZ GTR 0 set "file_is_empty=0"
echo DEBUG: file_is_empty flag = %file_is_empty% >> "%debug_log%"

if "%file_is_empty%"=="1" goto :list_action_file_is_empty
goto :list_action_file_has_content

:list_action_file_not_found
    echo ERROR: Exclusion file "%exclusion_file%" not found! >> "%debug_log%"
    echo ERROR: Exclusion file "%exclusion_file%" not found!
    goto :list_action_end

:list_action_file_is_empty
    echo (Exclusion file is empty) >> "%debug_log%"
    echo (Exclusion file is empty)
    goto :list_action_end

:list_action_file_has_content
    echo DEBUG: File is not empty, listing with findstr /n... >> "%debug_log%"
    findstr /n /r "^.*" "%exclusion_file%" 2>nul
    goto :list_action_end

:list_action_end
echo ------------------------
echo.
pause
goto :menu


:: ================== ADD Action ==================
:add_action
echo DEBUG: Reached add_action >> "%debug_log%"
:: Check item exists and is valid
if not defined item goto :add_action_no_item

:: Validate pattern doesn't contain invalid characters
::echo !item!|findstr /r "[][<>|?*]" >nul
::if !errorlevel! equ 0 (
::    echo ERROR: Invalid characters in pattern "!item!" >> "%debug_log%"
::    echo ERROR: Pattern cannot contain: []<>|?*
::    goto :add_action_invalid
::)

echo DEBUG: ADD action - item IS defined: "!item!" >> "%debug_log%"
:: Additional validation for paths
if exist ""!item!"" (
    echo DEBUG: Path exists, adding as absolute exclusion >> "%debug_log%"
) else (
    echo DEBUG: Adding as pattern match >> "%debug_log%"
)
goto :add_action_check_exists

:add_action_invalid
echo.
pause
goto :menu

:add_action_no_item
    echo DEBUG: ADD action - item is NOT defined. >> "%debug_log%"
    echo ERROR: Please specify the item to add (e.g., ADD node_modules).
    echo.
    pause
    goto :menu

:add_action_check_exists
echo Adding exclusion: "!item!"
:: --- Check if item exists ---
findstr /i /x /c:"!item!" "%exclusion_file%" 1>nul 2>nul
set FINDSTR_ERROR=%ERRORLEVEL%
echo DEBUG: findstr check for existing item finished with errorlevel %FINDSTR_ERROR% >> "%debug_log%"
if "%FINDSTR_ERROR%"=="0" goto :add_action_item_already_exists
goto :add_action_do_append

:add_action_item_already_exists
    echo WARNING: "!item!" already exists. Not adding again. >> "%debug_log%"
    echo WARNING: "!item!" already exists. Not adding again.
    echo.
    pause
    goto :menu

:add_action_do_append
:: Append logic - Use GOTO to ensure only one append happens
echo DEBUG: Starting append logic for non-existing item >> "%debug_log%"
set add_newline=0
if exist "%exclusion_file%" (
    for %%Z in ("%exclusion_file%") do if %%~zZ GTR 0 set add_newline=1
)
echo DEBUG: add_newline flag set to %add_newline% >> "%debug_log%"

if "%add_newline%"=="1" goto :append_with_newline
goto :append_without_newline

:append_with_newline
    echo DEBUG: Appending with leading newline >> "%debug_log%"
    (echo.&echo !item!)>>"%exclusion_file%"
    goto :append_verify

:append_without_newline
    echo DEBUG: Appending without leading newline >> "%debug_log%"
    (echo !item!)>>"%exclusion_file%"
    goto :append_verify

:append_verify
:: Ignore errorlevel from echo >>, just verify with findstr
echo DEBUG: Append command executed. Now verifying write with findstr... >> "%debug_log%"
findstr /i /x /c:"!item!" "%exclusion_file%" 1>nul 2>nul
set FINDSTR_VERIFY_ERROR=%ERRORLEVEL%
echo DEBUG: findstr verify finished with errorlevel %FINDSTR_VERIFY_ERROR% >> "%debug_log%"

:: --- Report Success/Failure based *only* on findstr verification ---
if "%FINDSTR_VERIFY_ERROR%"=="0" (
    echo Successfully added "!item!" to the exclusion list. >> "%debug_log%"
    echo Successfully added "!item!" to the exclusion list.
) else (
    echo ERROR: Failed to verify write of "!item!" to "%exclusion_file%". Check file content, permissions or disk space. >> "%debug_log%"
    echo ERROR: Failed to verify write of "!item!" to "%exclusion_file%". Check file content, permissions or disk space.
)
echo.
pause
goto :menu

:: ================== REMOVE Action ==================
:remove_action
echo DEBUG: Reached remove_action >> "%debug_log%"
:: Check item using goto structure
if not defined item goto :remove_action_no_item
echo DEBUG: REMOVE action - item IS defined: "!item!" >> "%debug_log%"
goto :remove_action_check_exists

:remove_action_no_item
    echo DEBUG: REMOVE action - item is NOT defined. >> "%debug_log%"
    echo ERROR: Please specify the item to remove (e.g., REMOVE node_modules).
    echo.
    pause
    goto :menu

:remove_action_check_exists
echo Removing exclusion: "!item!"
:: --- Check if item exists ---
findstr /i /x /c:"!item!" "%exclusion_file%" 1>nul 2>nul
set FINDSTR_ERROR=%ERRORLEVEL%
echo DEBUG: findstr check for item to remove finished with errorlevel %FINDSTR_ERROR% >> "%debug_log%"
if not "%FINDSTR_ERROR%"=="0" goto :remove_action_item_not_found
goto :remove_action_do_rebuild

:remove_action_item_not_found
    echo WARNING: "!item!" was not found. Nothing to remove. >> "%debug_log%"
    echo WARNING: "!item!" was not found. Nothing to remove.
    echo.
    pause
    goto :menu

:remove_action_do_rebuild
:: Rebuild the file without the matching line
echo DEBUG: Rebuilding exclusion file to %temp_file% >> "%debug_log%"
echo Creating temporary file...
set removed_flag=0
(for /f "usebackq delims=" %%L in ("%exclusion_file%") do (
    if /i not "%%L"=="!item!" (
        echo %%L
    ) else (
        echo DEBUG: Found item '%%L' to remove - setting flag >> "%debug_log%"
        set removed_flag=1
    )
))>"%temp_file%"
set REBUILD_ERROR=%ERRORLEVEL%
echo DEBUG: Rebuild loop finished with errorlevel %REBUILD_ERROR% >> "%debug_log%"

if "%REBUILD_ERROR%"=="1" (
  echo ERROR: Failed creating temp file "%temp_file%". >> "%debug_log%"
  echo ERROR: Failed creating temp file "%temp_file%".
  if exist "%temp_file%" del "%temp_file%"
  pause
  goto :menu
)

echo DEBUG: removed_flag is !removed_flag! >> "%debug_log%"
if "%removed_flag%"=="0" (
  echo ERROR: Failed to find "!item!" during rebuild? Aborting. >> "%debug_log%"
  echo ERROR: Failed to find "!item!" during rebuild? Aborting.
  del "%temp_file%"
  pause
  goto :menu
)

echo DEBUG: Replacing original file with temp file >> "%debug_log%"
echo Replacing original file with updated list...
move /Y "%temp_file%" "%exclusion_file%" > nul
set MOVE_ERROR=%ERRORLEVEL%
echo DEBUG: Move finished with errorlevel %MOVE_ERROR% >> "%debug_log%"
if "%MOVE_ERROR%"=="1" (
  echo ERROR: Failed replacing "%exclusion_file%". Temp file is "%temp_file%". >> "%debug_log%"
  echo ERROR: Failed replacing "%exclusion_file%". Temp file is "%temp_file%".
  pause
  goto :menu
)

echo Successfully removed "!item!" from the exclusion list. >> "%debug_log%"
echo Successfully removed "!item!" from the exclusion list.
echo.
pause
goto :menu

:: ================== RUN BACKUP Action ==================
:run_backup_action
echo DEBUG: Reached run_backup_action >> "%debug_log%"
if not exist "%backup_script%" (
    echo ERROR: Backup script "%backup_script%" not found! Cannot run backup. >> "%debug_log%"
    echo ERROR: Backup script "%backup_script%" not found! Cannot run backup.
    pause
    goto :menu
)
echo --- Starting Backup Script ---
echo Press Ctrl+C in the backup window to cancel the backup script if needed.
echo Press any key here to start the backup...
pause
echo Calling "%backup_script%"... >> "%debug_log%"
call "%backup_script%"
echo --- Backup Script Finished ---
echo Returned control to exclusion manager. >> "%debug_log%"
echo Returned control to exclusion manager.
pause
goto :menu


:: ================== QUIT ==================
:help_action
echo.
echo [EXCLUSION PATTERN HELP]
echo   Directory: End with / or \ (e.g. node_modules/)
echo   File: Specific name or wildcard (e.g. *.log)
echo   Paths: Can be relative or absolute
echo   Note: The Backup_Suite folder is always excluded
echo.
pause
goto :menu

:quit
echo DEBUG: Reached quit >> "%debug_log%"
echo Exiting script.
goto :eof

:: ================== END ==================
:eof
endlocal