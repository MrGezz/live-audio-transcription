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

set "VENV_PY=.venv\Scripts\python.exe"
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
echo   [1] Standard  - live_transcription.py, overlay + full arguments
echo   [2] Lite      - live_transcription_lite.py, console only, hardcoded
echo                   needs a recording device (Stereo Mix / mic), no loopback
echo   [3] Benchmark - benchmark.py, time inference on THIS machine and print
echo                   the --buffer / --slide that actually hold real time
choice /C 123 /N /M "Select what to run [1/2/3]: "
REM  Two traps here, and the second one is silent:
REM   - "if errorlevel N" means N OR HIGHER, so 3 also satisfies the test for
REM     2. The tests must run in DESCENDING order.
REM   - a successful "set" resets ERRORLEVEL to 0, so branching has to happen
REM     BEFORE the first assignment. Seeding a default first and overriding it
REM     looks tidier and silently sends every choice down the same path.
if errorlevel 3 goto mode_bench
if errorlevel 2 goto mode_lite
set "SCRIPT_FILE=live_transcription.py"
goto mode_done
:mode_lite
set "SCRIPT_FILE=live_transcription_lite.py"
goto mode_done
:mode_bench
set "SCRIPT_FILE=benchmark.py"
:mode_done

set "TRANSLATE=0"
set "CAPTURE_MODE=loopback"
set "VAD_ARGS="
set "S_VAD=speech (default)"
set "SAVE=0"
set "OUTPUT="
set "LANGUAGE="
set "BENCHWAV="
set "BENCHSIZES="
set "IS_LITE=0"
set "IS_BENCH=0"
if "!SCRIPT_FILE!"=="live_transcription_lite.py" set "IS_LITE=1"
if "!SCRIPT_FILE!"=="benchmark.py" set "IS_BENCH=1"
if "!IS_LITE!"=="1" goto summary
if "!IS_BENCH!"=="1" goto benchopts

echo.
echo ------------------------------------------------------------
echo  Translation
echo ------------------------------------------------------------
choice /C YN /N /M "Translate speech to English as it transcribes?  [Y/N]: "
if errorlevel 2 (set "TRANSLATE=0") else (set "TRANSLATE=1")

echo.
echo ------------------------------------------------------------
echo  Spoken language
echo ------------------------------------------------------------
echo   Press Enter to auto-detect it on every buffer. Pin it when
echo   you know it - detection can disagree with itself on short
echo   or noisy windows, and the transcript follows.
echo   Codes: en es fr de it pt ru ja ko zh ms id hi ar ...
set /p "LANGUAGE=Language code (or press Enter for auto): "
REM Same quote-stripping as OUTPUT below: --language ""ms"" would be rejected
REM by argparse only after the server and venv are already up.
if defined LANGUAGE set LANGUAGE=!LANGUAGE:"=!

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
echo  Speech detection
echo ------------------------------------------------------------
echo   Buffers with no speech in them are skipped, so fans and room
echo   tone stop producing invented captions. Sung vocals score far
echo   lower than speech, so music needs a looser setting.
echo.
echo   [1] Speech - talking, meetings, videos, podcasts
echo   [2] Music  - also transcribe sung vocals (--vad-min-speech-ms 60)
echo   [3] Off    - transcribe everything, including fans and music
REM Tested descending, and BEFORE any set: `set` resets ERRORLEVEL to 0, and
REM `if errorlevel N` means "N or higher", so ascending tests all match 1.
choice /C 123 /N /M "Select speech detection [1/2/3]: "
if errorlevel 3 goto vad_off
if errorlevel 2 goto vad_music
goto vad_done
:vad_off
set "VAD_ARGS=--no-vad"
set "S_VAD=off - transcribe everything"
goto vad_done
:vad_music
set "VAD_ARGS=--vad-min-speech-ms 60"
set "S_VAD=music - sung vocals too"
:vad_done

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
REM  Explicit jump: without it the standard path would fall straight into the
REM  benchmark prompts below on its way to :summary.
goto summary

:benchopts
echo.
echo ------------------------------------------------------------
echo  Benchmark options
echo ------------------------------------------------------------
echo   A WAV of real speech gives numbers you can quote. Without
echo   one the tool uses synthetic tones, which decode to almost
echo   no text and so measure only the encoder floor.
echo   Use a clip at least as long as your biggest buffer size.
set /p "BENCHWAV=WAV file (or press Enter for synthetic): "
if defined BENCHWAV set BENCHWAV=!BENCHWAV:"=!

echo.
echo   Buffer lengths to time, in seconds, comma-separated.
set /p "BENCHSIZES=Sizes (or press Enter for 4,8,16,24): "
if not defined BENCHSIZES set "BENCHSIZES=4,8,16,24"
REM  Spaces would split this into extra argv entries and argparse would take
REM  the tail as positional arguments it does not have.
set "BENCHSIZES=!BENCHSIZES: =!"

:summary
set "S_SERVER=NO, assuming it is already running"
if "!START_SERVER!"=="1" set "S_SERVER=YES"
set "S_REINSTALL=NO"
if "!REINSTALL!"=="1" set "S_REINSTALL=YES"
set "S_TRANSLATE=NO"
if "!TRANSLATE!"=="1" set "S_TRANSLATE=YES"
set "S_LANGUAGE=auto-detect"
if defined LANGUAGE set "S_LANGUAGE=!LANGUAGE! (pinned)"
set "S_SAVE=NO"
if "!SAVE!"=="1" set "S_SAVE=YES, auto-named file"
if "!SAVE!"=="1" if defined OUTPUT set "S_SAVE=YES, !OUTPUT!"
set "S_BENCHWAV=synthetic tones (encoder floor only)"
if defined BENCHWAV set "S_BENCHWAV=!BENCHWAV!"
REM  cmd has no AND, so the "is this the standard script" test is collapsed
REM  into one flag rather than repeated as a pair of conditions per line.
set "SHOWOPTS=1"
if "!IS_LITE!"=="1" set "SHOWOPTS=0"
if "!IS_BENCH!"=="1" set "SHOWOPTS=0"

echo.
echo ============================================================
echo   SUMMARY
echo ============================================================
echo   Start whisper server ......... !S_SERVER!
echo   Clean reinstall .............. !S_REINSTALL!
echo   Script ....................... !SCRIPT_FILE!
if "!IS_LITE!"=="1"    echo   Configuration ................ built-in defaults (Lite)
if "!IS_BENCH!"=="1"   echo   Audio source ................. !S_BENCHWAV!
if "!IS_BENCH!"=="1"   echo   Buffer sizes to time ......... !BENCHSIZES!
if "!SHOWOPTS!"=="1"   echo   Translate to English ......... !S_TRANSLATE!
if "!SHOWOPTS!"=="1"   echo   Spoken language .............. !S_LANGUAGE!
if "!SHOWOPTS!"=="1"   echo   Audio capture mode ........... !CAPTURE_MODE!
if "!SHOWOPTS!"=="1"   echo   Speech detection ............. !S_VAD!
if "!SHOWOPTS!"=="1"   echo   Save transcript .............. !S_SAVE!
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
if "!REINSTALL!"=="1" if exist ".venv" (
    echo Removing the existing environment...
    rmdir /s /q .venv
)

REM The environment used to be created as "venv". Renaming it is not safe -
REM pip.exe and the activate scripts bake in the absolute path - so say what
REM happened rather than leaving someone wondering why every package is being
REM downloaded again, and why 2 GB of nothing is still sitting in the folder.
if not exist "!VENV_PY!" if exist "venv\Scripts\python.exe" (
    echo Note: this project now uses ".venv" instead of "venv".
    echo       Creating the new one; the old "venv" folder is no longer used
    echo       and can be deleted whenever you like.
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
    python -m venv .venv
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
echo  Step 3: Starting !SCRIPT_FILE!
echo ============================================================
set "PYARGS="
if "!IS_LITE!"=="1" goto run
if "!IS_BENCH!"=="1" goto benchargs
if "!TRANSLATE!"=="1" set "PYARGS=!PYARGS! --translate"
if "!SAVE!"=="1" set "PYARGS=!PYARGS! --save"
if "!SAVE!"=="1" if defined OUTPUT set PYARGS=!PYARGS! --output "!OUTPUT!"
if defined LANGUAGE set PYARGS=!PYARGS! --language "!LANGUAGE!"
set "PYARGS=!PYARGS! --capture !CAPTURE_MODE!"
if defined VAD_ARGS set "PYARGS=!PYARGS! !VAD_ARGS!"
goto run

:benchargs
set "PYARGS=--buffers !BENCHSIZES!"
if defined BENCHWAV set PYARGS=!PYARGS! --wav "!BENCHWAV!"

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
