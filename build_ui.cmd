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
cd /d "%~dp0"
dotnet publish ui\LiveTranscription.Ui.csproj -c Release --self-contained false -o ui\runtime %*
