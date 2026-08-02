' Hidden launcher for the fs-organizer watcher, for the STABLE copy that
' lives in ~/.fs-organizer/ and that the scheduled task points at.
'
' Why a launcher at all: Task Scheduler's own
' "powershell.exe -WindowStyle Hidden -File ..." action was observed to still
' show a visible console window (Windows 11 build 26200, console handling
' delegated to Windows Terminal, which has a history of not honoring
' -WindowStyle Hidden for Task-Scheduler-launched processes).
' WScript.Shell.Run's window-style parameter (0 = hidden) uses the classic
' ShellExecute/SW_HIDE path, which is not subject to that delegation.
'
' Why a SEPARATE launcher from fs-organizer-watch-launcher.vbs: that one
' starts the watcher sitting next to it, which is right when running from a
' checkout. This one starts the RESOLVER next to it, because the installed
' watcher lives under a version-stamped plugin directory that changes on
' every update. Resolving late is what lets the scheduled task be registered
' once and keep working.
'
' Optional argument: the folder to watch. Omitted, the watcher defaults to
' the current user's Downloads folder.
'   wscript.exe fs-organizer-watch-stable-launcher.vbs "D:\Scans"

Set objFSO = CreateObject("Scripting.FileSystemObject")
Set objShell = CreateObject("WScript.Shell")

scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
resolverPath = objFSO.BuildPath(scriptDir, "fs-organizer-watch-resolve.ps1")

If Not objFSO.FileExists(resolverPath) Then
    ' No console to complain to, so leave a trace where the watcher's own
    ' logs live rather than failing silently.
    logDir = objFSO.BuildPath(scriptDir, "logs")
    If Not objFSO.FolderExists(logDir) Then objFSO.CreateFolder(logDir)
    Set logFile = objFSO.OpenTextFile(objFSO.BuildPath(logDir, "watch-resolve.log"), 8, True)
    logFile.WriteLine "[" & Now & "] FATAL launcher could not find " & resolverPath & _
        " - re-run fs-organizer-watch-setup.ps1."
    logFile.Close
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & resolverPath & """"
If WScript.Arguments.Count > 0 Then
    cmd = cmd & " -WatchDir """ & WScript.Arguments(0) & """"
End If

objShell.Run cmd, 0, False
