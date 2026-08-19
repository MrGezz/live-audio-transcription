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
cd /d "%~dp0"
dotnet publish ui\LiveTranscription.Ui.csproj -c Release --self-contained false -o ui\runtime %*
