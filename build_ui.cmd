@echo off
REM Build the desktop panel's View assembly into ui\runtime.
REM
REM cd /d "%~dp0" keeps every path relative to this file, so the project can be
REM moved, cloned or put on another drive with nothing to edit.
REM
REM --self-contained false: the output needs only the Microsoft.WindowsDesktop.App
REM 8.0 runtime, which rolls forward to a newer one. The published
REM runtimeconfig.json is also the file pythonnet needs to host the CLR - a
REM hand-written one filtered to Microsoft.NETCore.App is the documented way to
REM get FileNotFoundException on PresentationFramework, because that framework
REM has no WPF in it.
REM
REM global.json (repo root) pins the SDK to the 8.0 band - rollForward
REM latestFeature, so any installed 8.0.x is accepted. Without it dotnet takes
REM the newest SDK on the machine, and a 10.x SDK rewrites the committed
REM ui\runtime\*.deps.json and *.runtimeconfig.json (it prunes framework-
REM provided packages and swaps a configProperties key) - a diff with no source
REM change behind it. An 8.0 SDK reproduces the committed metadata byte-for-byte,
REM so after a source edit the only file here that should change is
REM LiveTranscription.Ui.dll.
REM
REM After a successful publish, stamp ui\runtime with a hash of the sources
REM that went into it. ui\runtime is committed, so a stale one is otherwise
REM invisible - every panel test loads the shipped assembly rather than
REM building one, so editing a .cs and skipping this script leaves the whole
REM suite passing against the previous build. tests\test_ui_stamp.py compares
REM the stamp to the working tree and fails when they disagree.
REM
REM Written only when dotnet publish succeeded: a stamp is a claim that this
REM assembly was built from these sources, and a failed build did not build
REM one. Leaving the old stamp in place is correct - it still describes the
REM DLL that is actually sitting there.
cd /d "%~dp0"
dotnet publish ui\LiveTranscription.Ui.csproj -c Release --self-contained false -o ui\runtime %*
if errorlevel 1 exit /b 1

set "STAMP_PY=.venv\Scripts\python.exe"
if not exist "%STAMP_PY%" set "STAMP_PY=python"
"%STAMP_PY%" ui\tools\uihash.py --write
if errorlevel 1 (
    echo [WARN] Could not stamp ui\runtime - tests\test_ui_stamp.py will
    echo        report it stale until this runs. Is Python on PATH?
    exit /b 1
)
