<#
    The full watcher loop, against a REAL running watcher process.

    Slower than the other suites (about 90 seconds - it waits out real
    debounce windows) and it starts a background process, so it is kept
    separate rather than folded into them:

        powershell -ExecutionPolicy Bypass -File tests\test_watcher_live.ps1

    It covers the one thing the other tests only reach in pieces: that a
    file landing leads to exactly ONE dispatch, and that the run's own move
    of that file into a subfolder does not earn it a second one.

    That second dispatch was real. Enqueue-File asks the index whether it
    already knows a path, but it asks the moment the event fires - and a run
    moves a file first and updates the index afterwards, so the answer is
    legitimately "no" at that instant. Every file the watcher filed away
    queued itself straight back and started another headless session over
    finished work. Only running the loop for real showed it.

    The claude dispatch is pointed at where.exe through FSORG_CLAUDE: a
    real, tiny, non-interactive executable. What is under test is the
    watcher's loop, not what Claude does once handed a prompt, so the
    dispatch failing is expected and irrelevant - that it happens exactly
    once, for the right paths, is the point.
#>
param([string]$Sandbox = (Join-Path $env:TEMP ('fsorg-live-' + [guid]::NewGuid().ToString('N').Substring(0, 8))))

$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$Scripts = @(
    (Join-Path $root 'scripts'),
    (Join-Path $root 'skills\fs-organizer\scripts')
) | Where-Object { Test-Path (Join-Path $_ 'fs-organizer-watch.ps1') } | Select-Object -First 1
if (-not $Scripts) { throw "cannot locate the skill's scripts/ under $root" }
$Watcher = Join-Path $Scripts 'fs-organizer-watch.ps1'
$StateRoot = Join-Path $env:USERPROFILE '.fs-organizer'

$script:Failures = @()
function Check([string]$Name, $Got, $Expected) {
    if ($Got -eq $Expected) { Write-Host "  PASS  $Name" }
    else {
        Write-Host "  FAIL  $Name`n          got:      $Got`n          expected: $Expected"
        $script:Failures += $Name
    }
}

$watchDir = Join-Path $Sandbox 'Downloads'
$sub = Join-Path $watchDir 'receipts'
New-Item -ItemType Directory -Force -Path $sub | Out-Null

$scopeId = (& python -c "import sys; sys.path.insert(0, r'$Scripts')
from fsorg_common import scope_id
print(scope_id(r'$watchDir'))").Trim()
$stateDir = Join-Path $StateRoot $scopeId
$indexFile = Join-Path $stateDir 'index.json'
$logFile = Join-Path (Join-Path $StateRoot 'logs') "watcher-$scopeId.log"
Remove-Item $logFile, $stateDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

# The watcher only dispatches for a folder that already has an index.
'existing content' | Set-Content (Join-Path $sub 'already-filed-receipt.txt')
& python (Join-Path $Scripts 'index_manager.py') build --scope $watchDir --output $indexFile | Out-Null

function Wait-ForLog([string]$Pattern, [int]$TimeoutSec = 75) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $logFile) {
            $hit = Select-String -Path $logFile -Pattern $Pattern -ErrorAction SilentlyContinue
            if ($hit) { return $hit[-1].Line }
        }
        Start-Sleep -Milliseconds 500
    }
    return $null
}
function CountLog([string]$Pattern) {
    if (-not (Test-Path $logFile)) { return 0 }
    return @(Select-String -Path $logFile -Pattern $Pattern -ErrorAction SilentlyContinue).Count
}

$env:FSORG_CLAUDE = "$env:SystemRoot\System32\where.exe"
$proc = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Watcher, '-WatchDir', $watchDir `
    -WindowStyle Hidden -PassThru

try {
    Write-Host "`nwatcher startup"
    Check "starts" ([bool](Wait-ForLog 'Watcher started' 45)) $true
    Check "resolved a real python" ([bool](Wait-ForLog 'Using python: .+python\.exe' 5)) $true

    Write-Host "`na file landing at the top level"
    'Invoice from Acme, 2024-03-11, total 412.00' | Set-Content (Join-Path $watchDir 'new download.txt')
    Check "is queued" ([bool](Wait-ForLog 'QUEUED: .*new download')) $true
    Check "is dispatched" ([bool](Wait-ForLog 'DISPATCH batch.*new download')) $true

    Write-Host "`nthe run's own move into a subfolder"
    # Exactly the production ordering: the file moves first, the index is
    # updated afterwards. Being QUEUED here is expected - the enqueue-time
    # check cannot know yet. Being DISPATCHED is the defect.
    $moved = Join-Path $sub 'invoice-acme-2024-03-11.txt'
    Move-Item (Join-Path $watchDir 'new download.txt') $moved
    & python (Join-Path $Scripts 'index_manager.py') update $indexFile `
        --file $moved --scope $watchDir --folder 'receipts' --previous 'new download.txt' | Out-Null
    Start-Sleep -Seconds 28   # a full debounce window plus a drain
    # Either guard may fire, depending on whether the index update landed
    # before or after the filesystem event was processed - a race between two
    # correct behaviours, so both are accepted. Which one caught it does not
    # matter; that the file is never dispatched does.
    # Parenthesised: `CountLog 'x' -ge 1` would pass -ge and 1 to CountLog as
    # further arguments instead of comparing anything.
    $suppressed = (CountLog 'SKIP \(indexed since it was queued') + (CountLog 'SKIP \(already indexed')
    Check "is recognised as this run's own work" ($suppressed -ge 1) $true
    Check "is never dispatched" (CountLog 'DISPATCH batch.*invoice-acme') 0
    Check "total dispatches still one" (CountLog 'DISPATCH batch') 1

    Write-Host "`na genuinely new file afterwards"
    'Bank statement January 2024' | Set-Content (Join-Path $watchDir 'statement jan.txt')
    Check "is queued" ([bool](Wait-ForLog 'QUEUED: .*statement jan')) $true
    Check "is dispatched" ([bool](Wait-ForLog 'DISPATCH batch.*statement jan')) $true

    Write-Host "`na file dropped straight into a subfolder"
    # Top-level-only watching would miss this entirely, which is why the
    # watch covers the tree once nested watches are absorbed into it.
    'Receipt from the corner shop' | Set-Content (Join-Path $sub 'dropped straight in.txt')
    Check "is seen by the tree-wide watch" ([bool](Wait-ForLog 'QUEUED: .*dropped straight in')) $true
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item Env:\FSORG_CLAUDE -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    Remove-Item $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $stateDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $logFile -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "$($script:Failures.Count) FAILED: $($script:Failures -join ', ')"
    exit 1
}
Write-Host "all checks passed"
exit 0
