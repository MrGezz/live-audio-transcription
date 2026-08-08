@echo off
REM ============================================================
REM  Start whisper.cpp whisper-server with Vulkan GPU support
REM  (AMD Radeon Pro W5500 friendly - no CUDA/ROCm required)
REM
REM  Expected layout (see SETUP_AMD.md):
REM    _whisper.cpp\whisper-server.exe   (Vulkan build + DLLs)
REM    _models\ggml-small-q8_0.bin      (GGML model)
REM ============================================================

set WHISPER_DIR=%~dp0_whisper.cpp
REM  Override the model by passing a filename from _models\ as argument 1:
REM    start_whisper_server.bat ggml-base-q5_1.bin
set MODEL=%~dp0_models\%~1
if "%MODEL%"=="%~dp0_models\" set MODEL=%~dp0_models\ggml-small-q8_0.bin
set HOST=127.0.0.1
set PORT=8080

REM  Expansions inside these ( ) blocks MUST stay quoted: cmd substitutes
REM  %VAR% while parsing the whole block, so a ")" from a path such as
REM  C:\Program Files (x86)\... would close the block early and abort the
REM  entire script with "was unexpected at this time" before it runs.
if not exist "%WHISPER_DIR%\whisper-server.exe" (
    echo [ERROR] whisper-server.exe not found in "%WHISPER_DIR%"
    echo         Download a Vulkan build of whisper.cpp - see SETUP_AMD.md
    pause
    exit /b 1
)

if not exist "%MODEL%" (
    echo [ERROR] Model not found: "%MODEL%"
    echo         Models currently in "%~dp0_models":
    dir /b "%~dp0_models\*.bin" 2>nul
    echo         Pass one as an argument, or download it - see SETUP_AMD.md
    pause
    exit /b 1
)

echo Starting whisper-server on http://%HOST%:%PORT% ...
echo Model: %MODEL%
echo (Watch the startup log: it should list your AMD GPU as a Vulkan device)
echo.

"%WHISPER_DIR%\whisper-server.exe" -m "%MODEL%" --host %HOST% --port %PORT% -l auto

pause
