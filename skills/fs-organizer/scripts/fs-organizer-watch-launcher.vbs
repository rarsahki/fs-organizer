' Hidden launcher for the fs-organizer watcher.
'
' Task Scheduler's own "powershell.exe -WindowStyle Hidden -File ..." action
' was observed to still show a visible console window on this machine
' (Windows 11 build 26200, no explicit terminal-delegation override set -
' HKCU\Console\%%Startup has no DelegationConsole/DelegationTerminal values,
' so new console processes route through Windows Terminal's default
' delegation, which has a known history of not honoring -WindowStyle Hidden
' for Task-Scheduler-launched processes). WScript.Shell.Run's window-style
' parameter (0 = hidden) uses the classic ShellExecute/SW_HIDE path instead,
' which is not subject to that delegation, so route the launch through here.
'
' The watcher script is located relative to THIS file rather than by an
' absolute path, so the skill works on any Windows machine and under any
' user account without editing.
'
' Optional argument: the folder to watch. Omitted, the watcher defaults to
' the current user's Downloads folder.
'   wscript.exe fs-organizer-watch-launcher.vbs "D:\Scans"

Set objFSO = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
ps1Path = objFSO.BuildPath(scriptDir, "fs-organizer-watch.ps1")

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & ps1Path & """"
If WScript.Arguments.Count > 0 Then
    cmd = cmd & " -WatchDir """ & WScript.Arguments(0) & """"
End If

objShell.Run cmd, 0, False
