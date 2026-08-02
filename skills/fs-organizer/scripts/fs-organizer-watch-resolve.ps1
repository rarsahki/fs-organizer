<#
.SYNOPSIS
    Find the installed fs-organizer watcher and start it.

.DESCRIPTION
    A copy of this file lives at a STABLE path (~/.fs-organizer/), and the
    scheduled task points there. The watcher itself does not: plugins install
    under a version-stamped directory -

        ~\.claude\plugins\cache\<marketplace>\fs-organizer\0.1.0\skills\...

    - and Claude Code reclaims versions that are no longer in use. A task
    registered against that path would therefore keep working until the day
    the plugin is updated, then stop, silently: a watcher that never starts
    is indistinguishable from one with nothing to do, which is exactly the
    failure this skill works hardest to avoid everywhere else.

    So the version is resolved here, at every launch, instead of being baked
    into the task. Updating the plugin needs no re-registration.

    Set FSORG_WATCHER to a fs-organizer-watch.ps1 of your own to override the
    search entirely - useful when running from a git checkout.
#>
param(
    [string]$WatchDir
)

$ErrorActionPreference = 'Stop'

$stateDir = Join-Path $env:USERPROFILE '.fs-organizer'
$logDir = Join-Path $stateDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'watch-resolve.log'

function Write-ResolveLog {
    param([string]$Message)
    Add-Content -Path $log -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Message)
}

function Resolve-Watcher {
    if ($env:FSORG_WATCHER) {
        if (Test-Path -LiteralPath $env:FSORG_WATCHER) {
            Write-ResolveLog "Using FSORG_WATCHER override: $env:FSORG_WATCHER"
            return (Get-Item -LiteralPath $env:FSORG_WATCHER).FullName
        }
        Write-ResolveLog "WARN FSORG_WATCHER is set to '$env:FSORG_WATCHER' but that path does not exist - ignoring it."
    }

    # <cache>\<marketplace>\<plugin>\<version>\skills\fs-organizer\scripts\...
    # The marketplace is wildcarded too: the plugin is the same whether it was
    # installed from this marketplace or a fork under another name.
    $pattern = Join-Path $env:USERPROFILE `
        '.claude\plugins\cache\*\fs-organizer\*\skills\fs-organizer\scripts\fs-organizer-watch.ps1'
    $found = @(Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue)
    if (-not $found) { return $null }

    # Newest version wins. Sorted as [version] rather than as text, so 0.10.0
    # is not judged older than 0.9.0; anything unparseable sorts last instead
    # of throwing.
    $ranked = $found | ForEach-Object {
        $versionText = $_.Directory.Parent.Parent.Parent.Name
        $parsed = $null
        [void][version]::TryParse($versionText, [ref]$parsed)
        [PSCustomObject]@{
            Path    = $_.FullName
            Version = $parsed
            Text    = $versionText
        }
    } | Sort-Object @{ Expression = { $null -ne $_.Version }; Descending = $true },
                    @{ Expression = { $_.Version }; Descending = $true }

    $best = $ranked | Select-Object -First 1
    Write-ResolveLog ("Resolved fs-organizer {0} -> {1}" -f $best.Text, $best.Path)
    return $best.Path
}

$watcher = Resolve-Watcher
if (-not $watcher) {
    Write-ResolveLog ("FATAL no installed fs-organizer watcher found under " +
        "$env:USERPROFILE\.claude\plugins\cache. Is the plugin still installed? " +
        "Re-run fs-organizer-watch-setup.ps1, or set FSORG_WATCHER to a checkout.")
    exit 1
}

$arguments = @{}
if ($WatchDir) { $arguments['WatchDir'] = $WatchDir }
& $watcher @arguments
