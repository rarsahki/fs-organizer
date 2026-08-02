<#
.SYNOPSIS
    Install the fs-organizer watcher as a scheduled task that starts at logon.

.DESCRIPTION
    Copies a small launcher and resolver into ~/.fs-organizer/ - a path that
    does not change - and registers a scheduled task against that. The
    installed plugin lives under a version-stamped directory that Claude Code
    reclaims when it is superseded, so pointing the task straight at it would
    work until the first update and then stop without saying so. Resolving the
    version at launch instead means updating the plugin needs no re-setup.

    Re-run this after updating the plugin ONLY if the launcher or resolver
    themselves changed; a normal plugin update needs nothing.

.PARAMETER WatchDir
    Folder to watch. Defaults to this user's Downloads.

.PARAMETER TaskName
    Scheduled task name. Defaults to "fs-organizer watch".

.PARAMETER Uninstall
    Remove the scheduled task and the stable launcher files.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File fs-organizer-watch-setup.ps1
    powershell -ExecutionPolicy Bypass -File fs-organizer-watch-setup.ps1 -WatchDir "D:\Scans"
    powershell -ExecutionPolicy Bypass -File fs-organizer-watch-setup.ps1 -Uninstall
#>
param(
    [string]$WatchDir = (Join-Path $env:USERPROFILE 'Downloads'),
    [string]$TaskName = 'fs-organizer watch',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$stateDir = Join-Path $env:USERPROFILE '.fs-organizer'
$launcherName = 'fs-organizer-watch-stable-launcher.vbs'
$resolverName = 'fs-organizer-watch-resolve.ps1'
$installedLauncher = Join-Path $stateDir $launcherName
$installedResolver = Join-Path $stateDir $resolverName

function Test-WatcherTask {
    # Redirection is done by cmd, not by PowerShell. Redirecting a native
    # command's stderr inside Windows PowerShell 5.1 wraps each line in an
    # ErrorRecord, which $ErrorActionPreference = 'Stop' turns into a
    # terminating error - and "no such task" is the expected answer here, not
    # a failure.
    cmd /c "schtasks /query /tn ""$TaskName"" >nul 2>nul"
    return ($LASTEXITCODE -eq 0)
}

function Remove-WatcherTask {
    if (-not (Test-WatcherTask)) { return $false }
    cmd /c "schtasks /delete /tn ""$TaskName"" /f >nul 2>nul"
    if ($LASTEXITCODE -ne 0) { throw "could not delete the existing task '$TaskName'" }
    Write-Host "  removed scheduled task '$TaskName'"
    return $true
}

if ($Uninstall) {
    Write-Host "Uninstalling the fs-organizer watcher..."
    if (-not (Remove-WatcherTask)) { Write-Host "  no scheduled task named '$TaskName'" }
    foreach ($f in $installedLauncher, $installedResolver) {
        if (Test-Path $f) { Remove-Item $f -Force; Write-Host "  removed $f" }
    }
    Write-Host "Done. Indexes, logs and journals under $stateDir were left alone."
    exit 0
}

# --- sanity checks before touching anything -------------------------------
$WatchDir = [System.IO.Path]::GetFullPath($WatchDir.TrimEnd('\', '/'))
if (-not (Test-Path -LiteralPath $WatchDir -PathType Container)) {
    throw "watch folder does not exist: $WatchDir"
}

$scopeName = Split-Path $WatchDir -Leaf
$indexFile = Join-Path (Join-Path $stateDir $scopeName) 'index.json'
if (-not (Test-Path $indexFile)) {
    Write-Warning ("$WatchDir has no index yet ($indexFile).")
    Write-Warning ("The watcher only organizes folders that have been organized once " +
                   "interactively - it will queue arrivals and skip dispatch until then. " +
                   "Ask Claude to 'organize $WatchDir' first.")
}

Write-Host "Installing the fs-organizer watcher..."
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

# --- copy the stable files ------------------------------------------------
foreach ($name in $launcherName, $resolverName) {
    $source = Join-Path $PSScriptRoot $name
    if (-not (Test-Path $source)) { throw "missing $source - is the plugin install complete?" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $stateDir $name) -Force
    Write-Host "  installed $name -> $stateDir"
}

# --- verify the resolver can actually find a watcher ----------------------
$pattern = Join-Path $env:USERPROFILE `
    '.claude\plugins\cache\*\fs-organizer\*\skills\fs-organizer\scripts\fs-organizer-watch.ps1'
$found = @(Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue)
if ($found) {
    Write-Host "  resolver sees $($found.Count) installed version(s); newest will be used"
} else {
    Write-Warning ("no installed watcher found under ~\.claude\plugins\cache. The task will " +
                   "be registered, but it cannot start anything until the plugin is installed " +
                   "(or FSORG_WATCHER points at a checkout).")
}

# --- register the task ----------------------------------------------------
[void](Remove-WatcherTask)
$action = 'wscript.exe "{0}" "{1}"' -f $installedLauncher, $WatchDir
# No stderr redirect on this one: /f already makes it idempotent, and the
# nested quoting the action needs is easier to get right without cmd in the
# middle.
schtasks /create /tn "$TaskName" /sc onlogon /rl limited /f /tr $action | Out-Null
if ($LASTEXITCODE -ne 0) { throw "schtasks failed to register '$TaskName' (exit $LASTEXITCODE)" }
if (-not (Test-WatcherTask)) { throw "schtasks reported success but '$TaskName' does not exist" }
Write-Host "  registered scheduled task '$TaskName' (at logon)"

Write-Host ""
Write-Host "Watching : $WatchDir"
Write-Host "Launcher : $installedLauncher   (stable - survives plugin updates)"
Write-Host "Logs     : $(Join-Path $stateDir 'logs')"
Write-Host ""
Write-Host "Start it now without logging out:"
Write-Host "  schtasks /run /tn `"$TaskName`""
Write-Host "Remove it:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
