@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Live Transcription - Setup ^& Run
color 0B

REM ============================================================
REM  Easy launcher: pick the script and flags, bring up the venv
REM  and the whisper server, then run it.
REM
REM  Style note: every read of a variable that was set earlier in
REM  this script uses !VAR!, and the control flow is kept flat with
REM  labels instead of nested ( ) blocks. %VAR% is substituted when
REM  a block is PARSED - before the block has run - which is what
REM  silently dropped --translate and --save in the old version.
REM ============================================================

set "VENV_PY=venv\Scripts\python.exe"
set "SERVER=start_whisper_server.bat"

:menu
cls
echo ============================================================
echo   LIVE TRANSCRIPTION - EASY LAUNCHER
echo ============================================================
echo.
echo   Choose the script, capture mode and flags, then confirm.
echo.
echo ------------------------------------------------------------
echo  Whisper server (the transcription engine)
echo ------------------------------------------------------------
choice /C YN /N /M "Start the whisper server now? (choose N if it is already running)  [Y/N]: "
if errorlevel 2 (set "START_SERVER=0") else (set "START_SERVER=1")

echo.
echo ------------------------------------------------------------
echo  Python environment (first run, or after updates)
echo ------------------------------------------------------------
choice /C YN /N /M "Force a clean reinstall of the Python environment?  [Y/N]: "
if errorlevel 2 (set "REINSTALL=0") else (set "REINSTALL=1")

echo.
echo ------------------------------------------------------------
echo  Script version
echo ------------------------------------------------------------
echo   [1] Standard - live_transcription.py, overlay + full arguments
echo   [2] Lite     - live_transcription_lite.py, console only, hardcoded
echo                  needs a recording device (Stereo Mix / mic), no loopback
choice /C 12 /N /M "Select script version [1/2]: "
if errorlevel 2 (set "SCRIPT_FILE=live_transcription_lite.py") else (set "SCRIPT_FILE=live_transcription.py")

set "TRANSLATE=0"
set "CAPTURE_MODE=loopback"
set "SAVE=0"
set "OUTPUT="
set "IS_LITE=0"
if "!SCRIPT_FILE!"=="live_transcription_lite.py" set "IS_LITE=1"
if "!IS_LITE!"=="1" goto summary

echo.
echo ------------------------------------------------------------
echo  Translation
echo ------------------------------------------------------------
choice /C YN /N /M "Translate speech to English as it transcribes?  [Y/N]: "
if errorlevel 2 (set "TRANSLATE=0") else (set "TRANSLATE=1")

echo.
echo ------------------------------------------------------------
echo  Audio capture method
echo ------------------------------------------------------------
echo   [1] Loopback - capture what an output device is playing (WASAPI)
echo   [2] Input    - capture a recording device (Stereo Mix / microphone)
choice /C 12 /N /M "Select audio capture mode [1/2]: "
if errorlevel 2 (set "CAPTURE_MODE=input") else (set "CAPTURE_MODE=loopback")

echo.
echo ------------------------------------------------------------
echo  Save transcript to a file
echo ------------------------------------------------------------
choice /C YN /N /M "Save the transcript to a text file?  [Y/N]: "
if errorlevel 2 (set "SAVE=0") else (set "SAVE=1")
if not "!SAVE!"=="1" goto summary

echo.
echo   Enter a name for the transcript file, e.g. my_transcript.txt
echo   Or press Enter to auto-name it with the current date and time.
set /p "OUTPUT=File name (or press Enter to skip): "
REM Quoting a name with spaces is normal Windows habit (and "Copy as path"
REM always quotes), but the quotes would survive into --output ""my file.txt""
REM and argparse would reject it - after the server and venv are already up.
if defined OUTPUT set OUTPUT=!OUTPUT:"=!

:summary
set "S_SERVER=NO, assuming it is already running"
if "!START_SERVER!"=="1" set "S_SERVER=YES"
set "S_REINSTALL=NO"
if "!REINSTALL!"=="1" set "S_REINSTALL=YES"
set "S_TRANSLATE=NO"
if "!TRANSLATE!"=="1" set "S_TRANSLATE=YES"
set "S_SAVE=NO"
if "!SAVE!"=="1" set "S_SAVE=YES, auto-named file"
if "!SAVE!"=="1" if defined OUTPUT set "S_SAVE=YES, !OUTPUT!"

echo.
echo ============================================================
echo   SUMMARY
echo ============================================================
echo   Start whisper server ......... !S_SERVER!
echo   Clean reinstall .............. !S_REINSTALL!
echo   Script ....................... !SCRIPT_FILE!
if "!IS_LITE!"=="1"     echo   Configuration ................ built-in defaults (Lite)
if not "!IS_LITE!"=="1" echo   Translate to English ......... !S_TRANSLATE!
if not "!IS_LITE!"=="1" echo   Audio capture mode ........... !CAPTURE_MODE!
if not "!IS_LITE!"=="1" echo   Save transcript .............. !S_SAVE!
echo ============================================================
echo.
choice /C YN /N /M "Does this look right? Y to start, N to choose again  [Y/N]: "
if errorlevel 2 goto menu

echo.
echo ============================================================
echo  Step 1: Whisper server
echo ============================================================
if not "!START_SERVER!"=="1" echo Skipping - assuming it is already running.
if not "!START_SERVER!"=="1" goto venv
if not exist "!SERVER!" (
    echo [ERROR] !SERVER! not found in this folder.
    pause
    exit /b 1
)
echo Opening the whisper server in its own window...
start "Whisper Server" cmd /k call "!SERVER!"
echo Waiting a few seconds for it to start up...
timeout /t 5 /nobreak >nul

:venv
echo.
echo ============================================================
echo  Step 2: Python environment
echo ============================================================
if "!REINSTALL!"=="1" if exist "venv" (
    echo Removing the existing environment...
    rmdir /s /q venv
)

if not exist "!VENV_PY!" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python was not found on PATH. Install Python 3, tick
        echo         "Add python.exe to PATH" during setup, then try again.
        pause
        exit /b 1
    )
    echo Creating the Python environment...
    python -m venv venv
)

if not exist "!VENV_PY!" (
    echo [ERROR] Could not create the Python environment.
    pause
    exit /b 1
)

echo Installing/checking required packages...
if "!REINSTALL!"=="1" (
    "!VENV_PY!" -m pip install --upgrade --force-reinstall -r requirements.txt
) else (
    "!VENV_PY!" -m pip install -r requirements.txt >nul
)
if errorlevel 1 (
    echo [ERROR] Something went wrong installing packages.
    echo         Re-run and answer Y to "clean reinstall" to see the full output.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Step 3: Starting live transcription
echo ============================================================
set "PYARGS="
if "!IS_LITE!"=="1" goto run
if "!TRANSLATE!"=="1" set "PYARGS=!PYARGS! --translate"
if "!SAVE!"=="1" set "PYARGS=!PYARGS! --save"
if "!SAVE!"=="1" if defined OUTPUT set PYARGS=!PYARGS! --output "!OUTPUT!"
set "PYARGS=!PYARGS! --capture !CAPTURE_MODE!"

:run
echo Running: !SCRIPT_FILE! !PYARGS!
echo.
"!VENV_PY!" "!SCRIPT_FILE!" !PYARGS!

echo.
echo ============================================================
echo  Finished. It is safe to close this window.
echo ============================================================
pause
endlocal
