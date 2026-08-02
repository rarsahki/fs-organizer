<#
    Regression suite for the watcher's version resolver.

    The installed plugin lives under a version-stamped directory that Claude
    Code reclaims when superseded, so a scheduled task registered against it
    would break on the first update - silently, because a watcher that never
    starts looks exactly like one with nothing to do. The resolver exists to
    make the task's path stable; these checks keep it that way.

        powershell -ExecutionPolicy Bypass -File tests\test_watcher_resolve.ps1
#>
param(
    [string]$Sandbox = (Join-Path $env:TEMP ('fsorg-resolve-' + [guid]::NewGuid().ToString('N').Substring(0, 8)))
)

$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
$Resolver = @(
    (Join-Path $root 'scripts\fs-organizer-watch-resolve.ps1'),
    (Join-Path $root 'skills\fs-organizer\scripts\fs-organizer-watch-resolve.ps1')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Resolver) { throw "cannot locate fs-organizer-watch-resolve.ps1 under $root" }

$script:Failures = @()
function Check([string]$Name, $Got, $Expected) {
    if ($Got -eq $Expected) { Write-Host "  PASS  $Name" }
    else {
        Write-Host "  FAIL  $Name`n          got:      $Got`n          expected: $Expected"
        $script:Failures += $Name
    }
}

function Write-ResolveLog { param([string]$Message) $script:LogLines += $Message }
$script:LogLines = @()

$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Resolver, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "resolver has parse errors: $($parseErrors -join '; ')" }
$fn = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
      Where-Object { $_.Name -eq 'Resolve-Watcher' } | Select-Object -First 1
if (-not $fn) { throw "resolver no longer defines Resolve-Watcher" }
Invoke-Expression $fn.Extent.Text

# A fake USERPROFILE, so the real plugin cache is never read or written.
$fakeHome = Join-Path $Sandbox 'home'
$cache = Join-Path $fakeHome '.claude\plugins\cache\fs-organizer-tool\fs-organizer'
$realHome = $env:USERPROFILE

function Add-FakeVersion([string]$Version, [string]$Marketplace = 'fs-organizer-tool') {
    $dir = Join-Path $fakeHome ".claude\plugins\cache\$Marketplace\fs-organizer\$Version\skills\fs-organizer\scripts"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Set-Content -Path (Join-Path $dir 'fs-organizer-watch.ps1') -Value "# fake $Version"
}

try {
    $env:USERPROFILE = $fakeHome

    Write-Host "`nversion resolution"
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    Check "nothing installed returns null" ($null -eq (Resolve-Watcher)) $true

    Add-FakeVersion '0.1.0'
    Check "single version is found" ((Resolve-Watcher) -like '*\0.1.0\*') $true

    # Text sorting would put 0.9.0 above 0.10.0; version sorting must not.
    Add-FakeVersion '0.9.0'
    Add-FakeVersion '0.10.0'
    Check "picks 0.10.0 over 0.9.0 and 0.1.0" ((Resolve-Watcher) -like '*\0.10.0\*') $true

    Add-FakeVersion '1.0.0'
    Check "picks 1.0.0 once present" ((Resolve-Watcher) -like '*\1.0.0\*') $true

    # A junk directory name must not throw, and must not outrank a real version.
    Add-FakeVersion 'not-a-version'
    Check "unparseable version does not win" ((Resolve-Watcher) -like '*\1.0.0\*') $true

    # The same plugin installed from a fork under a different marketplace name.
    Add-FakeVersion '2.0.0' -Marketplace 'someone-elses-marketplace'
    Check "finds the plugin under any marketplace" ((Resolve-Watcher) -like '*\2.0.0\*') $true

    Write-Host "`noverride"
    $env:FSORG_WATCHER = $Resolver
    Check "FSORG_WATCHER wins over the cache" (Resolve-Watcher) $Resolver
    $env:FSORG_WATCHER = Join-Path $Sandbox 'nope\missing.ps1'
    Check "a bad override falls back to the cache" ((Resolve-Watcher) -like '*\2.0.0\*') $true
    Remove-Item Env:\FSORG_WATCHER -ErrorAction SilentlyContinue
} finally {
    $env:USERPROFILE = $realHome
    Remove-Item Env:\FSORG_WATCHER -ErrorAction SilentlyContinue
    Remove-Item $Sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "$($script:Failures.Count) FAILED: $($script:Failures -join ', ')"
    exit 1
}
Write-Host "all checks passed"
exit 0
