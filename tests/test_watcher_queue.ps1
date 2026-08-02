<#
    Regression suite for the watcher's PowerShell-side edge cases.

    Every check here corresponds to a defect found by driving the watcher
    against realistic Windows conditions. They exist so a future change
    cannot quietly reintroduce one. Run directly:

        powershell -ExecutionPolicy Bypass -File tests\test_watcher_queue.ps1

    Exits non-zero if anything fails.
#>
param(
    [string]$Sandbox = (Join-Path $env:TEMP ('fsorg-watcher-tests-' + [guid]::NewGuid().ToString('N').Substring(0, 8)))
)

$ErrorActionPreference = 'Stop'

# Installed, the skill is flat: <skill>\tests and <skill>\scripts. Packaged as
# a plugin, tests live at the repo root while the skill sits under
# skills\fs-organizer\. The same file has to run in both.
$root = Split-Path $PSScriptRoot -Parent
$SkillPs1 = @(
    (Join-Path $root 'scripts\fs-organizer-watch.ps1'),
    (Join-Path $root 'skills\fs-organizer\scripts\fs-organizer-watch.ps1')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $SkillPs1) { throw "cannot locate fs-organizer-watch.ps1 under $root" }

$script:Failures = @()
function Check([string]$Name, $Got, $Expected) {
    if ($Got -eq $Expected) {
        Write-Host "  PASS  $Name"
    } else {
        Write-Host "  FAIL  $Name`n          got:      $Got`n          expected: $Expected"
        $script:Failures += $Name
    }
}
function Section([string]$Title) { Write-Host "`n$Title" }

# The real Write-Log appends to a file and emits nothing. A stub that wrote
# to the output stream would merge into every caller's return value.
$script:LogLines = @()
function Write-Log { param([string]$Message) $script:LogLines += $Message }

# Load the real functions out of the shipped script, by AST, so these tests
# exercise the shipped code rather than a copy that can drift from it.
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SkillPs1, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "watcher script has parse errors: $($parseErrors -join '; ')" }
$fnAsts = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($name in 'Expand-NestedQueue', 'Read-JsonArray', 'Write-JsonArray', 'Resolve-Executable') {
    $fn = $fnAsts | Where-Object { $_.Name -eq $name } | Select-Object -First 1
    if (-not $fn) { throw "watcher script no longer defines $name" }
    Invoke-Expression $fn.Extent.Text
}

$dir = (New-Item -ItemType Directory -Force -Path $Sandbox).FullName
$QueueFile = Join-Path $dir 'watch-queue.json'

try {
    # ---------------------------------------------------------------- queue
    Section "queue round-trip"

    # The shipped enqueue body, minus the lock and logging.
    function Add-ToQueue([string]$Path) {
        $queue = Read-JsonArray -Path $QueueFile
        $already = @($queue | Where-Object { $_.path -eq $Path })
        if ($already.Count -eq 0) {
            $queue = @($queue) + [PSCustomObject]@{ path = $Path; queued_at = (Get-Date -Format 'o') }
        }
        Write-JsonArray -Path $QueueFile -Items $queue
        return ($already.Count -ne 0)
    }

    # PowerShell 5.1's ConvertFrom-Json hands back a JSON array as ONE
    # object rather than enumerating it. The obvious @(... | ConvertFrom-Json)
    # therefore wraps it, and appending to that wrapper nested the queue from
    # the third file onward: dedupe stopped matching and the dispatcher could
    # see only the last entry, so every other file that arrived in the same
    # debounce window was silently dropped.
    1..10 | ForEach-Object { [void](Add-ToQueue "C:\w\file-$_.txt") }
    $batch = Read-JsonArray -Path $QueueFile
    $paths = @($batch | ForEach-Object { $_.path })
    Check "ten arrivals all survive" $batch.Count 10
    Check "dispatcher sees every path" $paths.Count 10
    Check "first arrival not lost" ($paths -contains 'C:\w\file-1.txt') $true
    Check "last arrival not lost" ($paths -contains 'C:\w\file-10.txt') $true
    Check "queue file stays flat" ((Get-Content $QueueFile -Raw) -match '"Count":') $false
    Check "no spurious recovery logging" $script:LogLines.Count 0

    Check "duplicate event is deduped" (Add-ToQueue "C:\w\file-5.txt") $true
    Check "duplicate does not grow the queue" (Read-JsonArray -Path $QueueFile).Count 10

    Section "queue edge cases"
    Remove-Item $QueueFile -ErrorAction SilentlyContinue
    Check "missing file reads empty" (Read-JsonArray -Path $QueueFile).Count 0
    '' | Set-Content $QueueFile
    Check "empty file reads empty" (Read-JsonArray -Path $QueueFile).Count 0
    'not json at all {{{' | Set-Content $QueueFile
    Check "unparseable file reads empty" (Read-JsonArray -Path $QueueFile).Count 0
    Write-JsonArray -Path $QueueFile -Items @([PSCustomObject]@{ path = 'C:\w\solo.txt' })
    $one = Read-JsonArray -Path $QueueFile
    Check "single entry stays an array" $one.Count 1
    Check "single entry keeps its path" $one[0].path 'C:\w\solo.txt'

    Section "recovery of a queue written by an older version"
    Remove-Item $QueueFile -ErrorAction SilentlyContinue
    # Reproduce the old corruption by running the old write path verbatim.
    function Add-ToQueueOld([string]$Path) {
        $queue = @()
        if (Test-Path $QueueFile) {
            try { $queue = @(Get-Content $QueueFile -Raw | ConvertFrom-Json) } catch { $queue = @() }
        }
        $already = @($queue | Where-Object { $_.path -eq $Path })
        if ($already.Count -eq 0) { $queue = @($queue) + [PSCustomObject]@{ path = $Path; queued_at = 'old' } }
        $queue | ConvertTo-Json -Depth 5 | Set-Content -Path $QueueFile -Encoding UTF8
    }
    1..5 | ForEach-Object { Add-ToQueueOld "C:\w\legacy-$_.txt" }
    Check "the old path really did corrupt the file" `
        (((Get-Content $QueueFile -Raw) -match '"Count":') -as [bool]) $true
    $script:LogLines = @()
    $recovered = Read-JsonArray -Path $QueueFile
    $recoveredPaths = @($recovered | ForEach-Object { $_.path } | Where-Object { $_ })
    # Entries past ConvertTo-Json's -Depth were written as their PowerShell
    # string form, so recovery has to parse those back too.
    Check "every buried entry is recovered" $recoveredPaths.Count 5
    Check "deepest entry recovered" ($recoveredPaths -contains 'C:\w\legacy-1.txt') $true
    Check "recovery is reported" ($script:LogLines.Count -gt 0) $true

    # ------------------------------------------------------------ executables
    Section "executable resolution"

    $stubDir = Join-Path $dir 'WindowsApps'
    New-Item -ItemType Directory -Force -Path $stubDir | Out-Null
    # A 0-byte App Execution Alias, exactly as Windows ships for python.exe.
    New-Item -ItemType File -Force -Path (Join-Path $stubDir 'python.exe') | Out-Null
    $realDir = Join-Path $dir 'RealPython'
    New-Item -ItemType Directory -Force -Path $realDir | Out-Null
    Set-Content -Path (Join-Path $realDir 'python.exe') -Value 'not empty'

    $savedPath = $env:PATH
    try {
        $env:PATH = $stubDir
        # The Python installer leaves "Add to PATH" unchecked by default, so
        # on a normal install the Store stub is the only python.exe on PATH.
        # Launched with CreateNoWindow it can neither open the Store nor run
        # Python, and every headless dispatch failed with nothing to show.
        $resolved = Resolve-Executable -Names @('python') -Candidates @((Join-Path $realDir 'python.exe'))
        Check "0-byte Store stub is rejected" ($resolved -eq (Join-Path $stubDir 'python.exe')) $false
        Check "falls through to a real interpreter" $resolved (Join-Path $realDir 'python.exe')

        $missing = Resolve-Executable -Names @('no-such-exe-anywhere') -Candidates @('C:\nope\none.exe')
        Check "returns null when nothing is usable" ($null -eq $missing) $true

        $env:FSORG_PYTHON = Join-Path $realDir 'python.exe'
        Check "explicit override wins" `
            (Resolve-Executable -Names @('python') -OverrideEnvVar 'FSORG_PYTHON') (Join-Path $realDir 'python.exe')
    } finally {
        $env:PATH = $savedPath
        Remove-Item Env:\FSORG_PYTHON -ErrorAction SilentlyContinue
    }
} finally {
    Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "$($script:Failures.Count) FAILED: $($script:Failures -join ', ')"
    exit 1
}
Write-Host "all checks passed"
exit 0
