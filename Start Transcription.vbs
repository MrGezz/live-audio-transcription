' Start Transcription.vbs
' Double-click this to run the pipeline. It explicitly launches Command
' Prompt itself, so it works even if .cmd file associations on this
' PC are broken or hijacked (like the Start-Process issue seen earlier).

Dim shell, target
Set shell = CreateObject("WScript.Shell")
' Resolve the launcher next to this .vbs so the folder can be moved/cloned.
target = CreateObject("Scripting.FileSystemObject") _
    .GetParentFolderName(WScript.ScriptFullName) & "\run_pipeline.cmd"

shell.Run "cmd.exe /D /k """ & target & """", 1, False
