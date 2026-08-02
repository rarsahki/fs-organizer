<#
.SYNOPSIS
    Turn the fs-organizer watcher on (or off) for a folder.

.DESCRIPTION
    Installs a launcher at a fixed path under ~/.fs-organizer/ and starts it
    at logon from HKCU's Run key. Two deliberate choices:

    A fixed launcher path, because the plugin installs under a
    version-stamped directory that Claude Code reclaims once superseded.
    Pointing autostart straight at it would work until the first update and
    then stop without saying so.

    The Run key rather than a scheduled task, because
    `schtasks /sc onlogon` requires Administrator - it fails with "Access is
    denied" for a normal user - and an optional convenience should not put a
    UAC prompt in the way. The Run key is per-user, needs no elevation, and
    an entry is removed by deleting one value.

    Nested watches are not allowed to coexist. A watcher on Downloads and
    another on Downloads\Receipts dispatch twice for one file: the first
    session files a download into Receipts, the second watcher reads that as
    an arrival and repeats the work. So watching a folder ABSORBS any watch
    already registered inside it - carrying that scope's folder purposes into
    this one's index and retiring its state - and watching a folder already
    covered by an outer watch is refused.

.PARAMETER WatchDir
    Folder to watch. Defaults to this user's Downloads.

.PARAMETER Uninstall
    Stop watching this folder: removes its autostart entry and registration.
    Its index, logs and journals are left in place.

.PARAMETER Force
    Absorb nested watches without asking.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File fs-organizer-watch-setup.ps1
    powershell -ExecutionPolicy Bypass -File fs-organizer-watch-setup.ps1 -WatchDir "D:\Scans"
    powershell -ExecutionPolicy Bypass -File fs-organizer-watch-setup.ps1 -Uninstall
#>
param(
    [string]$WatchDir = (Join-Path $env:USERPROFILE 'Downloads'),
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$StateDir     = Join-Path $env:USERPROFILE '.fs-organizer'
$ScriptsDir   = $PSScriptRoot
$RunKey       = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$LauncherName = 'fs-organizer-watch-stable-launcher.vbs'
$ResolverName = 'fs-organizer-watch-resolve.ps1'
$Launcher     = Join-Path $StateDir $LauncherName

function Get-PythonExe {
    # Same reasoning as the watcher's Resolve-Executable: Windows 11 ships
    # 0-byte Store redirector stubs for python.exe, and the Python installer
    # leaves "Add to PATH" unchecked, so a bare name is not a program.
    foreach ($name in 'python', 'python3', 'py') {
        $c = Get-Command $name -All -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -and (Get-Item -LiteralPath $_.Path -ErrorAction SilentlyContinue).Length -gt 0 } |
             Select-Object -First 1
        if ($c) { return $c.Path }
    }
    foreach ($pattern in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'),
        (Join-Path $env:ProgramFiles 'Python3*\python.exe'))) {
        $hit = Get-Item -Path $pattern -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    throw ("no usable python.exe found. The Microsoft Store aliases in WindowsApps are " +
           "0-byte redirectors, not Python. Install Python with 'Add python.exe to PATH' " +
           "ticked, or put it on PATH.")
}

$Python = Get-PythonExe

function Invoke-Fsorg {
    <# Run one of the skill's Python entry points and return its stdout. #>
    param([string[]]$Arguments)
    $out = & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "python $($Arguments -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

function Get-ScopeIdFor {
    # Asked of Python rather than recomputed here, so the id can never drift
    # from the one the rest of the skill uses to name a scope's state.
    param([string]$Path)
    $script = @"
import sys
sys.path.insert(0, r'$ScriptsDir')
from fsorg_common import scope_id
print(scope_id(r'$Path'))
"@
    return (Invoke-Fsorg @('-c', $script) | Select-Object -Last 1).Trim()
}

function Get-RunValueName { param([string]$Path) return "fs-organizer-$(Get-ScopeIdFor $Path)" }

function Remove-Autostart {
    param([string]$Path)
    $name = Get-RunValueName $Path
    $existing = Get-ItemProperty -Path $RunKey -Name $name -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-ItemProperty -Path $RunKey -Name $name -Force
        return $true
    }
    return $false
}

$WatchDir = [System.IO.Path]::GetFullPath($WatchDir.TrimEnd('\', '/'))

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    Write-Host "Stopping the watch on $WatchDir..."
    if (Remove-Autostart $WatchDir) { Write-Host "  removed its autostart entry" }
    else { Write-Host "  no autostart entry for this folder" }
    [void](Invoke-Fsorg @((Join-Path $ScriptsDir 'watch_registry.py'), 'unregister', '--root', $WatchDir))
    Write-Host "  deregistered"
    Write-Host ""
    Write-Host "Its index, logs and journals under $StateDir were left alone."
    Write-Host "A watcher already running keeps running until you log out or end it."
    exit 0
}

# ------------------------------------------------------------------ install
if (-not (Test-Path -LiteralPath $WatchDir -PathType Container)) {
    throw "watch folder does not exist: $WatchDir"
}

$scopeId   = Get-ScopeIdFor $WatchDir
$indexFile = Join-Path (Join-Path $StateDir $scopeId) 'index.json'

Write-Host "Setting up the fs-organizer watcher for $WatchDir"
Write-Host "  scope id: $scopeId"

# --- nested watches -------------------------------------------------------
$overlapJson = Invoke-Fsorg @((Join-Path $ScriptsDir 'watch_registry.py'), 'check', '--root', $WatchDir)
$overlap = ($overlapJson -join "`n") | ConvertFrom-Json

if ($overlap.covered_by) {
    $parent = $overlap.covered_by.root
    throw ("$WatchDir is already covered by the watch on $parent. Nested watches run two " +
           "headless sessions over the same file, so this one is refused. The outer watch " +
           "already organizes this folder; to watch it separately, stop the outer one first: " +
           "-WatchDir `"$parent`" -Uninstall")
}

$absorb = @($overlap.absorbs)
if ($absorb.Count -gt 0) {
    Write-Host ""
    Write-Host "  These folders inside it are already watched separately:"
    $absorb | ForEach-Object { Write-Host "    $($_.root)" }
    Write-Host "  Nested watches dispatch twice for the same file, so they will be absorbed"
    Write-Host "  into this one. Their folder purposes are carried over; nothing is deleted."
    if (-not $Force) {
        $answer = Read-Host "  Absorb them? [y/N]"
        if ($answer -notmatch '^(y|yes)$') { Write-Host "  Cancelled - nothing changed."; exit 1 }
    }

    foreach ($child in $absorb) {
        $childRoot = $child.root
        $childIndex = Join-Path (Join-Path $StateDir $child.scope_id) 'index.json'
        if ((Test-Path $childIndex) -and (Test-Path $indexFile)) {
            $merged = Invoke-Fsorg @((Join-Path $ScriptsDir 'index_manager.py'), 'merge-scope',
                '--parent-index', $indexFile, '--parent-root', $WatchDir,
                '--child-index', $childIndex, '--child-root', $childRoot)
            Write-Host "    $merged"
        } elseif (Test-Path $childIndex) {
            Write-Host "    $childRoot has purposes but $WatchDir has no index yet -"
            Write-Host "      organize $WatchDir first if you want them carried over."
        }
        [void](Remove-Autostart $childRoot)
        [void](Invoke-Fsorg @((Join-Path $ScriptsDir 'watch_registry.py'), 'unregister', '--root', $childRoot))
        Write-Host "    absorbed $childRoot"
    }
}

# --- the stable launcher --------------------------------------------------
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
foreach ($name in $LauncherName, $ResolverName) {
    $source = Join-Path $ScriptsDir $name
    if (-not (Test-Path $source)) { throw "missing $source - is the plugin install complete?" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $StateDir $name) -Force
}
Write-Host "  installed the launcher into $StateDir"

# --- autostart ------------------------------------------------------------
$valueName = "fs-organizer-$scopeId"
$command = 'wscript.exe "{0}" "{1}"' -f $Launcher, $WatchDir
Set-ItemProperty -Path $RunKey -Name $valueName -Value $command -Force
Write-Host "  autostart registered (HKCU Run, no elevation needed)"

[void](Invoke-Fsorg @((Join-Path $ScriptsDir 'watch_registry.py'), 'register', '--root', $WatchDir))
Write-Host "  registered as watched"

if (-not (Test-Path $indexFile)) {
    Write-Host ""
    Write-Warning ("$WatchDir has no index yet. The watcher only organizes a folder that has " +
                   "been organized once interactively - until then it queues arrivals and " +
                   "skips dispatch. Ask Claude to 'organize $WatchDir' first.")
}

Write-Host ""
Write-Host "Watching  : $WatchDir"
Write-Host "Starts    : at logon"
Write-Host "Logs      : $(Join-Path $StateDir 'logs')"
Write-Host ""
Write-Host "Start it now without logging out:"
Write-Host "  wscript.exe `"$Launcher`" `"$WatchDir`""
Write-Host "Stop watching this folder:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -WatchDir `"$WatchDir`" -Uninstall"
