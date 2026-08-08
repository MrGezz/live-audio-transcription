@echo off
REM ============================================================
REM  Start whisper.cpp whisper-server with Vulkan GPU support
REM  (AMD Radeon Pro W5500 friendly - no CUDA/ROCm required)
REM
REM  Expected layout (see SETUP_AMD.md):
REM    _whisper.cpp\whisper-server.exe   (Vulkan build + DLLs)
REM    _models\ggml-small-q5_1.bin      (GGML model)
REM ============================================================

set WHISPER_DIR=%~dp0_whisper.cpp
set MODEL=%~dp0_models\%~1
if "%MODEL%"=="%~dp0_models\" set MODEL=%~dp0_models\ggml-small-q5_1.bin
set HOST=127.0.0.1
set PORT=8080

if not exist "%WHISPER_DIR%\whisper-server.exe" (
    echo [ERROR] whisper-server.exe not found in %WHISPER_DIR%
    echo         Download a Vulkan build of whisper.cpp - see SETUP_AMD.md
    pause
    exit /b 1
)

if not exist "%MODEL%" (
    echo [ERROR] Model not found: %MODEL%
    echo         Download ggml-small-q5_1.bin - see SETUP_AMD.md
    pause
    exit /b 1
)

echo Starting whisper-server on http://%HOST%:%PORT% ...
echo Model: %MODEL%
echo (Watch the startup log: it should list your AMD GPU as a Vulkan device)
echo.

"%WHISPER_DIR%\whisper-server.exe" -m "%MODEL%" --host %HOST% --port %PORT% -l auto

pause
