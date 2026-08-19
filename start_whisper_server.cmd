@echo off
REM ============================================================
REM  A COMPONENT, not the launcher. run_pipeline.cmd starts this for
REM  you; run it directly only when you want the GPU server on its
REM  own, or with a different model.
REM
REM  Start whisper.cpp whisper-server (Vulkan or CUDA build)
REM  (AMD/Intel -> Vulkan; NVIDIA -> CUDA. The app is backend-agnostic.)
REM
REM  Expected layout (see SETUP_AMD.md):
REM    _whisper.cpp\whisper-server.exe   (Vulkan or CUDA build + DLLs)
REM    _models\ggml-base-q5_1.bin      (GGML model)
REM ============================================================

set WHISPER_DIR=%~dp0_whisper.cpp
REM  Override the model by passing a filename from _models\ as argument 1:
REM    start_whisper_server.cmd ggml-base-q5_1.bin
set MODEL=%~dp0_models\%~1
if "%MODEL%"=="%~dp0_models\" set MODEL=%~dp0_models\ggml-base-q5_1.bin
set HOST=127.0.0.1
set PORT=8080

REM  Expansions inside these ( ) blocks MUST stay quoted: cmd substitutes
REM  %VAR% while parsing the whole block, so a ")" from a path such as
REM  C:\Program Files (x86)\... would close the block early and abort the
REM  entire script with "was unexpected at this time" before it runs.
if not exist "%WHISPER_DIR%\whisper-server.exe" (
    echo [ERROR] whisper-server.exe not found in "%WHISPER_DIR%"
    echo         Download a Vulkan or CUDA build of whisper.cpp - see SETUP_AMD.md
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
echo (Watch the startup log: it should list your GPU as a Vulkan or CUDA device)
echo.

"%WHISPER_DIR%\whisper-server.exe" -m "%MODEL%" --host %HOST% --port %PORT% -l auto
set EXITCODE=%ERRORLEVEL%

REM  Flat goto flow, and EXITCODE is read outside any ( ) block: cmd expands
REM  %VAR% when it PARSES a block, so the value inside one is the value from
REM  before the exe ran.
if %EXITCODE% equ -1073741795 goto illegal_instruction
if %EXITCODE% neq 0 goto other_error
goto end

:illegal_instruction
echo.
echo [ERROR] whisper-server died with STATUS_ILLEGAL_INSTRUCTION (0xC000001D).
echo         It exits right after "using ... backend", with no error of its own.
echo.
echo         The binaries in _whisper.cpp were compiled for CPU instructions this
echo         machine does not have - usually AVX-512. It is NOT a GPU or driver
echo         problem: it happens with --no-gpu too, and the model loads fine
echo         first because that part is plain code.
echo.
echo         Fix: rebuild whisper.cpp on THIS machine (SETUP_AMD.md, section 1,
echo         Option B - cmake targets the host CPU), or download a prebuilt
echo         Windows build that targets your CPU rather than a newer one.
goto end

:other_error
echo.
echo [ERROR] whisper-server exited with code %EXITCODE%.
echo         Check the log above - the last lines usually say why.
goto end

:end
pause
