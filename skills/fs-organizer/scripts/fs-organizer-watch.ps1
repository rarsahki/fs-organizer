<#
.SYNOPSIS
    Resident watcher for the fs-organizer skill's "watcher mode" (SKILL.md's
    workflow). Two independent FileSystemWatchers, two very different
    response paths:
      1. Top-level (non-recursive), Created/Renamed: "a new file arrived" -
         debounced, batched, dispatched to a headless `claude -p` running
         the skill's Watcher mode (steps 1-9 in
         references/watcher-workflow.md: index-backed lookup, Sonnet
         throughout, everything auto-executed). Dispatch is skipped
         entirely when the scope has no index yet - that first pass has to
         be an interactive Organize run, so it is never done unattended.
      2. Whole tree (recursive), Changed only: "an already-organized file's
         content changed in place" - no LLM involved at all, just a direct
         `index_manager.py rehash` call to refresh that one file's stored
         hash, closing the gap where an in-place edit would otherwise only
         ever be noticed opportunistically (via a later hash collision).

.DESCRIPTION
    Why this shape at all: nothing inside Claude Code can react to an
    OS-level filesystem event from cold. Hooks are lifecycle events within
    an already-running session; /loop is timer-based polling inside an open
    interactive session; scheduled routines run in a cloud sandbox with no
    access to a local Windows path. So the only workable trigger is an
    external, OS-native watcher process (this script) shelling out to a
    headless `claude -p` that names the skill and the landed file paths.

    Architecture:
      - Watcher 1's event handlers do the minimum: filter temp/partial
        download extensions, wait for the file to stop growing (stability
        check), skip anything the index already knows about (self-trigger
        guard against the watcher reacting to its own prior moves/renames),
        then append the path to a small on-disk queue file.
      - A separate polling loop (this script's main body) checks that queue
        file every $PollSeconds; once $DebounceSeconds have passed with no
        new arrivals, it drains the queue and dispatches ONE claude -p call
        for the whole batch - this is what lets the placement step's clustering
        (>= 2 files) actually be reachable instead of every file arriving
        in isolation.
      - A simple file-based lock serializes queue reads/writes between
        Watcher 1's handlers (producers) and the main loop (consumer).
      - Watcher 2's handler is synchronous and self-contained per event -
        no queue, no debounce, no claude dispatch - deliberately excludes
        top-level files (Watcher 1's job) so the two never double-process
        the same arrival or fight over a file mid-download.
      - index_manager.py's own writes are what actually protect against the
        two watchers racing on index.json itself (a cross-process file
        lock inside the Python script, not anything coordinated here).

    Not meant to be run interactively for normal use. Install it as a Task
    Scheduler "at logon" task pointing at the .vbs launcher beside this
    file - the launcher exists because Task Scheduler's own
    "powershell.exe -WindowStyle Hidden" action still flashed a console
    window on this machine; WScript.Shell.Run's hidden window style does
    not. Configure the task to restart on failure: a silently-dead watcher
    process previously went unnoticed for 34+ minutes with nothing
    relaunching it. EnableRaisingEvents + the polling loop below keep this
    process alive indefinitely once started.

        schtasks /Create /TN "fs-organizer-watch" /SC ONLOGON ^
          /TR "wscript.exe \"%USERPROFILE%\.claude\skills\fs-organizer\scripts\fs-organizer-watch-launcher.vbs\""

    Nothing here is specific to one machine or user account: the watched
    folder defaults to this user's Downloads but can be any folder on any
    drive (-WatchDir), and the skill's own scripts are located relative to
    this file, so it runs as-is on any Windows system.
#>

param(
    # Folder to watch. Its state lives under .fs-organizer\<scope-id>\.
    [string]$WatchDir = (Join-Path $env:USERPROFILE 'Downloads')
)

$ErrorActionPreference = 'Stop'

function Get-ScopeId {
    <#
        Stable, collision-free identity for a scope directory.

        MUST match fsorg_common.scope_id in Python exactly, or the watcher
        and the scripts it calls would read and write two different state
        folders for the same watched directory. Only ASCII A-Z is lowered,
        for the same reason: full Unicode case folding differs between
        PowerShell and Python on characters like the German sharp s.

        The leaf name alone is not an identity - C:\Users\me\Downloads\Receipts
        and D:\Scans\Receipts would share one index, one queue and one lock -
        so a short digest of the whole path is appended to it.
    #>
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path.TrimEnd('\', '/'))
    $normalized = [regex]::Replace($full, '[A-Z]', { param($m) $m.Value.ToLowerInvariant() })
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($normalized))
    } finally {
        $sha.Dispose()
    }
    $hex = -join ($bytes | ForEach-Object { $_.ToString('x2') })

    # The leaf's real on-disk casing, not whatever casing the caller typed.
    # Python's Path.resolve() normalizes it and GetFullPath does not, so
    # "-WatchDir C:\Users\me\DOWNLOADS" would otherwise produce the id
    # "DOWNLOADS-2435..." here and "Downloads-2435..." in Python. NTFS treats
    # those as one folder, which is exactly what makes the mismatch easy to
    # miss until something compares the two ids as strings.
    # DirectoryInfo.Name just echoes the path string back, so the parent has
    # to be enumerated to learn the entry's real spelling - the same reason
    # batch_executor._ondisk_name scans a directory instead of trusting a
    # path. When the folder does not exist yet, both languages fall back to
    # the typed casing, so they still agree.
    $leaf = Split-Path $full -Leaf
    $parent = Split-Path $full -Parent
    if ($parent) {
        $entry = Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -eq $leaf } | Select-Object -First 1
        if ($entry) { $leaf = $entry.Name }
    }

    return '{0}-{1}' -f $leaf, $hex.Substring(0, 8)
}

$WatchDir           = [System.IO.Path]::GetFullPath($WatchDir.TrimEnd('\', '/'))
$ScopeName          = Get-ScopeId $WatchDir
$StateDir           = Join-Path $env:USERPROFILE '.fs-organizer'
# Per-scope state: everything for the watched scope (its purpose index, its
# runs, its queue) lives under .fs-organizer\<scope-id>\. Keeping the
# queue and lock per-scope as well means two watchers on different folders
# never drain each other's queue or block on each other's lock.
$ScopeStateDir      = Join-Path $StateDir $ScopeName
$QueueFile          = Join-Path $ScopeStateDir 'watch-queue.json'
$LockFile           = Join-Path $ScopeStateDir 'watch.lock'
$LogDir             = Join-Path $StateDir 'logs'
$LogFile            = Join-Path $LogDir "watcher-$ScopeName.log"
$IndexFile          = Join-Path $ScopeStateDir 'index.json'
# This file lives in the skill's scripts\ folder alongside the Python it
# calls, so both are located relative to it - the skill can live anywhere
# (a user-level ~\.claude\skills, a project's .claude\skills, a cloned
# repo) with no path in here to edit.
$ScriptsDir         = $PSScriptRoot
$SkillDir           = Split-Path $PSScriptRoot -Parent
$IndexManagerPy     = Join-Path $ScriptsDir 'index_manager.py'

$TempExtPattern         = '\.(crdownload|tmp|part|partial|download|opdownload)$'
$DebounceSeconds        = 15
$PollSeconds            = 5
$StabilityChecks        = 2     # consecutive matching samples required
$StabilityIntervalSec   = 1
$StabilityTimeoutSec    = 120   # give up waiting for a file to stabilize after this long

New-Item -ItemType Directory -Force -Path $StateDir      | Out-Null
New-Item -ItemType Directory -Force -Path $ScopeStateDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir        | Out-Null

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK'), $Message
    Add-Content -Path $LogFile -Value $line
}

# Singleton guard: Task Scheduler's RestartOnFailure has been observed to
# launch a new instance roughly once a minute even while the current one
# is alive and healthy (not a crash - MultipleInstancesPolicy=IgnoreNew
# alone did not prevent the overlap in practice). Without this guard, two
# live instances would each register their own FileSystemWatchers on the
# same folder. A second instance exits immediately, cleanly, rather than
# running in parallel.
$WatcherMutex = New-Object System.Threading.Mutex($false, "Local\FsOrganizerWatcherSingleton-$ScopeName")
if (-not $WatcherMutex.WaitOne(0)) {
    Write-Log "Another watcher instance already holds the singleton lock - exiting (PID $PID)."
    exit 0
}

function Resolve-Executable {
    <#
        Find a real executable, never a bare name handed to the OS.

        ProcessStartInfo with UseShellExecute=$false resolves a bare name
        through PATH, and on Windows that is not good enough for either
        program this watcher drives:

        - Windows 11 ships 0-byte App Execution Alias stubs for python.exe,
          python3.exe and pythonw.exe in WindowsApps. They exist whether or
          not Python is installed, and their job is to open the Microsoft
          Store. The Python installer leaves "Add python.exe to PATH"
          UNCHECKED by default, so on a normal install the stub is the only
          python.exe on PATH. Started with CreateNoWindow, it cannot show
          the Store and cannot run Python, so every dispatch failed with
          nothing to show for it.
        - claude.exe installs to per-user locations (~\.local\bin,
          %LOCALAPPDATA%\Programs\claude, an npm global prefix) that a
          process launched by Task Scheduler at logon may not have on PATH.

        Order: explicit override, then PATH minus the stubs, then the known
        install locations. Callers resolve once at startup and fail loudly,
        because a watcher that runs but can never dispatch looks identical
        to one with nothing to do.
    #>
    param(
        [string[]]$Names,
        [string[]]$Candidates = @(),
        [string]$OverrideEnvVar
    )

    function Test-RealExe([string]$Path) {
        if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
        $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
        if (-not $item -or $item.PSIsContainer) { return $false }
        # A 0-byte executable is an App Execution Alias, not a program.
        return $item.Length -gt 0
    }

    if ($OverrideEnvVar) {
        $override = [Environment]::GetEnvironmentVariable($OverrideEnvVar)
        if ($override) {
            if (Test-RealExe $override) { return (Get-Item -LiteralPath $override).FullName }
            Write-Log "WARN $OverrideEnvVar is set to '$override', which is not a usable executable - ignoring it."
        }
    }

    foreach ($name in $Names) {
        $found = Get-Command $name -All -ErrorAction SilentlyContinue |
                 Where-Object { $_.Path -and (Test-RealExe $_.Path) } |
                 Select-Object -First 1
        if ($found) { return $found.Path }
    }

    foreach ($candidate in $Candidates) {
        # Candidates may contain wildcards (Python's versioned directories).
        # Not named $matches: that is an automatic variable PowerShell
        # rewrites on every -match, so assigning to it corrupts callers.
        $hits = Get-Item -Path $candidate -ErrorAction SilentlyContinue |
                Where-Object { Test-RealExe $_.FullName } |
                Sort-Object FullName -Descending
        if ($hits) { return ($hits | Select-Object -First 1).FullName }
    }

    return $null
}

function Get-PythonCandidates {
    # Registry first (authoritative, set by every official installer even
    # when PATH was declined), then the default install locations.
    $paths = @()
    foreach ($hive in 'HKCU:', 'HKLM:') {
        $root = Join-Path $hive 'SOFTWARE\Python\PythonCore'
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $install = (Get-ItemProperty (Join-Path $_.PSPath 'InstallPath') -ErrorAction SilentlyContinue).'(default)'
            if ($install) { $paths += (Join-Path $install 'python.exe') }
        }
    }
    $paths += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python3*\python.exe'
    $paths += Join-Path $env:ProgramFiles 'Python3*\python.exe'
    $paths += 'C:\Python3*\python.exe'
    return $paths
}

function ConvertTo-EscapedArg {
    # Windows CommandLineToArgvW-compatible argument escaping, hand-rolled
    # because Windows PowerShell 5.1's ProcessStartInfo has no ArgumentList
    # property (that's .NET Core only) - only the raw string .Arguments,
    # which the caller is fully responsible for quoting correctly. Rules:
    # wrap in quotes if the arg contains whitespace/quotes; every literal
    # quote gets backslash-escaped; a run of backslashes immediately before
    # a quote (embedded, or the closing quote we're about to add) must be
    # doubled. Round-trip tested against multi-line, quoted,
    # backslash-heavy content before trusting it with the real prompt.
    param([string]$Arg)
    if ($Arg -eq '') { return '""' }
    if ($Arg -notmatch '[\s"]') { return $Arg }
    $result = '"'
    $backslashes = 0
    foreach ($ch in $Arg.ToCharArray()) {
        if ($ch -eq '\') {
            $backslashes++
        } elseif ($ch -eq '"') {
            $result += ('\' * $backslashes) + '\"'
            $backslashes = 0
        } else {
            $result += ('\' * $backslashes) + $ch
            $backslashes = 0
        }
    }
    $result += ('\' * $backslashes) + '"'
    return $result
}

function Invoke-HiddenProcess {
    # Runs ANY console-subsystem executable with ZERO console window, ever.
    # This is the general form of the fix for the actual root cause behind
    # the watcher's repeated deaths: `& someExe ...` from within a
    # -WindowStyle Hidden PowerShell host can still let Windows allocate a
    # NEW, VISIBLE console for the child (a known PowerShell/Task-Scheduler
    # gotcha) - and when output is captured into a variable rather than
    # printed, that window appears blank. The user (reasonably) closed it,
    # which killed the child and took the synchronously-waiting parent down
    # with it (matches the STATUS_CONTROL_C_EXIT signature observed).
    # CreateNoWindow=true is the authoritative Win32-level fix - it applies
    # regardless of output handling, unlike -WindowStyle on the child
    # (which only applies to PowerShell children anyway, not arbitrary
    # console exes like claude.exe or python.exe).
    #
    # EVERY call site that shells out to an external console executable
    # MUST go through this function, not the `&` call operator directly -
    # this was originally only applied to the claude dispatch and missed
    # the content-watcher's `& python ... rehash` call, which reintroduced
    # the exact same window-appears-blank-gets-closed-kills-the-watcher
    # failure mode via a different child process. If a future edit adds
    # another external process invocation, route it through here too.
    #
    # Output is read via the async event pattern
    # (BeginOutputReadLine/BeginErrorReadLine) rather than synchronous
    # ReadToEnd() on both streams, which can deadlock if both stdout and
    # stderr fill their OS pipe buffers at once.
    param(
        [string]$FileName,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory
    )
    $argStr = ($ArgumentList | ForEach-Object { ConvertTo-EscapedArg $_ }) -join ' '

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FileName
    $psi.Arguments = $argStr
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    # Pin the child's working directory rather than inheriting this
    # process's, which varies with wherever the watcher happened to be
    # started from. Claude Code derives a session's transcript folder from
    # its cwd (~/.claude/projects/<sanitized-cwd>/), so an inherited cwd
    # scattered transcripts across folders - they were hard to find, and
    # the run's own usage report looked in the wrong place and silently
    # reported nothing. Pinned to the watched folder, every run for a
    # scope lands in one predictable place that holds ONLY that scope's
    # watcher runs, which is also what makes `--since <run start>` enough
    # to scope a cost report without needing a session id.
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $stdoutBuilder = New-Object System.Text.StringBuilder
    $stderrBuilder = New-Object System.Text.StringBuilder
    $stdoutEvent = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action {
        if ($null -ne $EventArgs.Data) { $Event.MessageData.AppendLine($EventArgs.Data) | Out-Null }
    } -MessageData $stdoutBuilder
    $stderrEvent = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action {
        if ($null -ne $EventArgs.Data) { $Event.MessageData.AppendLine($EventArgs.Data) | Out-Null }
    } -MessageData $stderrBuilder

    try {
        $proc.Start() | Out-Null
        $proc.BeginOutputReadLine()
        $proc.BeginErrorReadLine()
        $proc.WaitForExit()
    } finally {
        Unregister-Event -SourceIdentifier $stdoutEvent.Name -ErrorAction SilentlyContinue
        Unregister-Event -SourceIdentifier $stderrEvent.Name -ErrorAction SilentlyContinue
    }

    return [PSCustomObject]@{
        ExitCode = $proc.ExitCode
        StdOut   = $stdoutBuilder.ToString()
        StdErr   = $stderrBuilder.ToString()
    }
}

function Invoke-ClaudeHeadless {
    param(
        [string]$Prompt,
        [string]$Model,
        [string]$AllowedTools,
        [string]$OutputFormat,
        [string]$WorkingDirectory
    )
    # $script:ClaudeExe is a resolved full path, not a bare name - see
    # Resolve-Executable for why PATH alone cannot be trusted here.
    return Invoke-HiddenProcess -FileName $script:ClaudeExe -ArgumentList @(
        '-p', $Prompt, '--model', $Model, '--allowedTools', $AllowedTools, '--output-format', $OutputFormat
    ) -WorkingDirectory $WorkingDirectory
}

function Expand-NestedQueue {
    <# Flatten a queue file corrupted by the pre-fix write path.

       Versions before the Read-JsonArray fix rewrote the queue as
       {"value": [...], "Count": n} wrappers nested one level deeper on
       every add. An install upgrading mid-window would otherwise keep
       reading its own corrupt file and keep losing the entries buried in
       it, so anything shaped like a wrapper is unwrapped here and the
       real entries recovered. #>
    param($Node)
    $out = @()
    foreach ($item in @($Node)) {
        if ($null -eq $item) { continue }
        if ($item -is [string]) {
            # Past its -Depth limit, ConvertTo-Json rendered entries as their
            # PowerShell string form instead of objects. The data survived,
            # so parse it back rather than discarding those arrivals.
            if ($item -match '^@\{\s*path=(?<p>.+?);\s*queued_at=(?<q>.*?)\s*\}$') {
                $out += [PSCustomObject]@{ path = $Matches['p']; queued_at = $Matches['q'] }
                $script:QueueWasRepaired = $true
            }
            continue
        }
        $names = @($item.PSObject.Properties.Name)
        # Each recursive result is ASSIGNED before being appended. This
        # function returns `,$out`, so its pipeline output is a single
        # object that happens to be an array; `$out += <call>` would append
        # that array as one element and rebuild the nesting being undone.
        if ($names -contains 'value' -and $names -contains 'Count' -and $names -notcontains 'path') {
            $script:QueueWasRepaired = $true
            $child = Expand-NestedQueue -Node $item.value
            $out += @($child)
        } elseif ($item -is [System.Collections.IEnumerable]) {
            $child = Expand-NestedQueue -Node $item
            $out += @($child)
        } else {
            $out += $item
        }
    }
    # Unary comma: `return $out` would unroll a one-element array back into a
    # bare object, and the caller's .Count would then be empty.
    return ,$out
}

function Read-JsonArray {
    <# Read a JSON array file as a flat PowerShell array.

       Windows PowerShell 5.1's ConvertFrom-Json emits a JSON array as a
       SINGLE object instead of enumerating it, so the obvious
       `@(Get-Content ... | ConvertFrom-Json)` returns a one-element array
       wrapping the real one. Adding to that produced a nested queue from
       the third file onward, which broke dedupe and left the dispatcher
       able to see only the last entry - every other file that arrived in
       the same debounce window was silently dropped. Assigning the result
       first and wrapping the VARIABLE is what unrolls correctly. #>
    param([string]$Path)
    if (-not (Test-Path $Path)) { return ,@() }
    try {
        $parsed = Get-Content $Path -Raw | ConvertFrom-Json
    } catch {
        Write-Log "WARN unreadable JSON at ${Path} - treating as empty."
        return ,@()
    }
    if ($null -eq $parsed) { return ,@() }
    # An explicit flag, not a count comparison: @($parsed) is 1 for ANY
    # multi-element array here - that is the very quirk being worked around
    # - so comparing counts reported a recovery on every ordinary read.
    $script:QueueWasRepaired = $false
    # Assign, THEN wrap. `@(Expand-NestedQueue ...)` would wrap the array
    # this function hands back into a new one-element array - the same
    # mistake, one level up, that corrupted the queue in the first place.
    $expanded = Expand-NestedQueue -Node $parsed
    $flat = @($expanded)
    if ($script:QueueWasRepaired) {
        Write-Log "Recovered $($flat.Count) entr(ies) from a queue file written by an older version."
    }
    return ,$flat
}

function Write-JsonArray {
    <# Write a JSON array file. -InputObject keeps ConvertTo-Json from
       enumerating the array away, which is the write-side half of the same
       5.1 quirk. #>
    param([string]$Path, $Items)
    ConvertTo-Json -InputObject @($Items) -Depth 5 | Set-Content -Path $Path -Encoding UTF8
}

function Get-QueueLock {
    param([int]$MaxRetries = 20, [int]$RetryDelayMs = 200)
    for ($i = 0; $i -lt $MaxRetries; $i++) {
        try {
            $fs = [System.IO.File]::Open($LockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
            $fs.Close()
            return $true
        } catch {
            Start-Sleep -Milliseconds $RetryDelayMs
        }
    }
    return $false
}

function Release-QueueLock {
    Remove-Item -Path $LockFile -ErrorAction SilentlyContinue
}

function Test-AlreadyIndexed {
    # Self-trigger guard: is this path something the index already accounts
    # for - the watcher's own move or rename echoing back - rather than a
    # genuinely new arrival?
    #
    # This became load-bearing when the watcher started covering the whole
    # tree. Every file the skill files into a subfolder now raises an
    # arrival event from its new location, and without this check each one
    # would start another headless session over work just completed: the
    # nested-watcher cascade, reproduced inside a single scope.
    #
    # All three parts of the index are consulted, since a file can be
    # accounted for in any of them:
    #   by_sha256   - paths relative to the scope root, which is where a
    #                 filed-away file is recorded
    #   loose_files - bare names of files still at the scope root
    #   folders     - relative folder paths, catching a folder's own rename
    #
    # Fails OPEN on any read error: a corrupt or half-written index must
    # never silently swallow a real new file.
    param([string]$RelPath)
    if (-not (Test-Path $IndexFile)) { return $false }
    try {
        $idx = Get-Content $IndexFile -Raw | ConvertFrom-Json
        $normalized = ($RelPath -replace '\\', '/').TrimStart('/')
        $leaf = Split-Path $normalized -Leaf

        if (@($idx.loose_files) -contains $leaf -and $normalized -eq $leaf) { return $true }

        if ($idx.folders) {
            if (@($idx.folders.PSObject.Properties.Name) -contains $normalized) { return $true }
        }

        if ($idx.by_sha256) {
            foreach ($property in $idx.by_sha256.PSObject.Properties) {
                # ConvertFrom-Json hands back the value as one object rather
                # than enumerating it (the 5.1 quirk Read-JsonArray exists
                # for), so wrap before comparing.
                if (@($property.Value) -contains $normalized) { return $true }
            }
        }
        return $false
    } catch {
        return $false
    }
}

function Wait-ForStableFile {
    # Poll until two consecutive unchanged samples: "landed". For a FILE
    # that is size + last-write-time (handles direct-write downloads still
    # growing, and temp-then-rename deliveries that arrive complete). For
    # a DIRECTORY - an extracted archive, a saved page's asset folder, a
    # copied project - it is the recursive file count + total size, since
    # the folder's own timestamps don't change while children stream in.
    param([string]$Path)
    $elapsed = 0
    $prevSig = $null
    $stableCount = 0
    while ($elapsed -lt $StabilityTimeoutSec) {
        if (-not (Test-Path $Path)) { return $false }
        if (Test-Path $Path -PathType Container) {
            $kids = @(Get-ChildItem $Path -Recurse -File -ErrorAction SilentlyContinue)
            $sig = "$($kids.Count)|$(($kids | Measure-Object Length -Sum).Sum)"
        } else {
            $item = Get-Item $Path
            $sig = "$($item.Length)|$($item.LastWriteTimeUtc.Ticks)"
        }
        if ($sig -eq $prevSig) {
            $stableCount++
            if ($stableCount -ge $StabilityChecks) { return $true }
        } else {
            $stableCount = 0
            $prevSig = $sig
        }
        Start-Sleep -Seconds $StabilityIntervalSec
        $elapsed += $StabilityIntervalSec
    }
    return $false
}

function Enqueue-File {
    param([string]$Path)

    if ($Path -match $TempExtPattern) { return }
    if (-not (Test-Path $Path)) { return }

    # Directories are arrivals too (extracted archives, copied projects,
    # saved-page asset folders) - with one carve-out: an asset folder
    # whose paired html already exists is NOT queued on its own. The html
    # is the item; the skill moves the pair as a unit. When the folder's
    # event fires before its html exists (browsers write the folder
    # first), it IS queued - and the html joins the same debounced batch
    # moments later, where the skill recognizes the pair.
    if (Test-Path $Path -PathType Container) {
        $leaf = [System.IO.Path]::GetFileName($Path)
        if ($leaf -match '_files$') {
            $stem = $leaf -replace '_files$', ''
            $parent = Split-Path $Path -Parent
            if ((Test-Path (Join-Path $parent "$stem.html")) -or (Test-Path (Join-Path $parent "$stem.htm"))) {
                Write-Log "SKIP (saved-page asset folder - travels with its html): $Path"
                return
            }
        }
    }

    # Relative to the scope root, not just the filename: the watch covers the
    # whole tree now, so "receipts/invoice-2024-01-31.pdf" is what the index
    # records and what the guard has to be asked about.
    $relPath = $Path
    if ($Path.StartsWith($WatchDir, [StringComparison]::OrdinalIgnoreCase)) {
        $relPath = $Path.Substring($WatchDir.Length).TrimStart('\', '/')
    }
    if (-not $relPath) { $relPath = [System.IO.Path]::GetFileName($Path) }
    if (Test-AlreadyIndexed -RelPath $relPath) {
        Write-Log "SKIP (already indexed - watcher's own move/rename, not a new arrival): $Path"
        return
    }

    if (-not (Wait-ForStableFile -Path $Path)) {
        Write-Log "SKIP (never stabilized within ${StabilityTimeoutSec}s, or vanished before landing): $Path"
        return
    }

    if (-not (Get-QueueLock)) {
        Write-Log "WARN could not acquire lock to enqueue, dropping: $Path"
        return
    }
    try {
        $queue = Read-JsonArray -Path $QueueFile
        # Dedupe by path: a single logical download commonly fires more than
        # one raw filesystem event (e.g. a Created event followed shortly by
        # a Renamed/metadata-touch event for the same final path) - without
        # this check the same file lands in the dispatch batch twice. Still
        # rewrite the queue file even on a duplicate, so its mtime refreshes
        # and the debounce window correctly extends on renewed activity.
        $alreadyQueued = @($queue | Where-Object { $_.path -eq $Path })
        if ($alreadyQueued.Count -eq 0) {
            $queue = @($queue) + [PSCustomObject]@{ path = $Path; queued_at = (Get-Date -Format 'o') }
        }
        Write-JsonArray -Path $QueueFile -Items $queue
        if ($alreadyQueued.Count -eq 0) {
            Write-Log "QUEUED: $Path"
        } else {
            Write-Log "QUEUED (dup event, debounce refreshed, not re-added): $Path"
        }
    } finally {
        Release-QueueLock
    }
}

function Invoke-Rehash {
    # Reactive counterpart to Enqueue-File: fires on a CONTENT change (not
    # arrival) to a file already living inside an organized SUBFOLDER - the
    # "someone edited an already-filed file in place" case (see the parent
    # conversation's design discussion; index_manager.py's lookup() only
    # catches this opportunistically, on a later hash collision - this is
    # the proactive side that closes that gap). No LLM/claude dispatch here
    # at all: refreshing a hash is pure mechanical bookkeeping, not a
    # judgment call, so it's a direct, cheap Python subprocess call.
    #
    # Deliberately scoped to SUBFOLDER files only (skips anything directly
    # at the watched folder's top level) - those are Enqueue-File's job
    # entirely; letting both watchers react to the same top-level file
    # mid-download would pollute the index with hashes of partial content
    # and waste subprocess spawns on a file that's about to be reprocessed
    # by the real pipeline anyway.
    param([string]$Path)

    if ($Path -match $TempExtPattern) { return }
    if (-not (Test-Path $Path -PathType Leaf)) { return }
    if ((Split-Path $Path -Parent) -eq $WatchDir) { return }  # top-level: not this watcher's job
    if (-not (Test-Path $IndexFile)) { return }  # nothing to reconcile against yet

    if (-not (Wait-ForStableFile -Path $Path)) {
        Write-Log "REHASH-SKIP (never stabilized): $Path"
        return
    }

    try {
        $r = Invoke-HiddenProcess -FileName $script:PythonExe -ArgumentList @(
            $IndexManagerPy, 'rehash', $IndexFile, '--file', $Path, '--scope', $WatchDir
        )
        Write-Log "REHASH (exit $($r.ExitCode)): $Path -> $($r.StdOut) $($r.StdErr)"
    } catch {
        Write-Log "ERROR rehashing $Path : $_"
    }
}

$OnEvent = {
    Enqueue-File -Path $Event.SourceEventArgs.FullPath
}

$OnContentChanged = {
    Invoke-Rehash -Path $Event.SourceEventArgs.FullPath
}

# A buffer overflow is not an error to shrug off: the OS drops the events
# it could not fit, so those files land and are never organized, and
# nothing about the folder afterwards says anything was missed. Recreating
# the watcher restores the event stream; the sweep is what recovers the
# files that arrived while it was down.
$OnWatcherError = {
    Write-Log "ERROR FileSystemWatcher: $($Event.SourceEventArgs.GetException().Message) - recovering."
    $script:WatcherNeedsRestart = $true
}

function Register-Watchers {
    <# Create both watchers and subscribe every event, including Error. #>
    # 64 KB is the largest size Windows will honour for a directory-change
    # buffer. The 8 KB default overflows on any bulk arrival - extracting an
    # archive into the watched folder is enough.
    $script:fsw = New-Object System.IO.FileSystemWatcher $WatchDir
    # The whole tree, not just the top level. One watcher now covers a scope
    # and everything nested inside it, because two watchers on nested folders
    # dispatch twice for the same file: the outer session moves a download
    # into a subfolder, the inner watcher sees an arrival, and a second
    # headless session runs over work that is already done. Since the outer
    # watch absorbs any nested one (see watch_registry.py), it has to see
    # arrivals in subfolders too - a file dropped straight into a subfolder
    # would otherwise be noticed by nobody.
    #
    # This makes Test-AlreadyIndexed load-bearing rather than a nicety: the
    # skill's own moves into subfolders now raise events here, and it is what
    # tells them apart from genuine arrivals.
    $script:fsw.IncludeSubdirectories = $true
    $script:fsw.InternalBufferSize = 65536
    $script:fsw.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::DirectoryName
    $script:fsw.EnableRaisingEvents = $true

    Register-ObjectEvent -InputObject $script:fsw -EventName Created -Action $OnEvent -SourceIdentifier 'FsOrgWatchCreated' | Out-Null
    Register-ObjectEvent -InputObject $script:fsw -EventName Renamed -Action $OnEvent -SourceIdentifier 'FsOrgWatchRenamed' | Out-Null
    Register-ObjectEvent -InputObject $script:fsw -EventName Error   -Action $OnWatcherError -SourceIdentifier 'FsOrgWatchError' | Out-Null

    # Watcher 2: whole tree, Changed only - "an already-organized file's
    # content changed in place" (Invoke-Rehash itself excludes top-level
    # files, so responsibilities stay cleanly split from Watcher 1).
    $script:fswContent = New-Object System.IO.FileSystemWatcher $WatchDir
    $script:fswContent.IncludeSubdirectories = $true
    $script:fswContent.InternalBufferSize = 65536
    $script:fswContent.NotifyFilter = [System.IO.NotifyFilters]::LastWrite
    $script:fswContent.EnableRaisingEvents = $true

    Register-ObjectEvent -InputObject $script:fswContent -EventName Changed -Action $OnContentChanged -SourceIdentifier 'FsOrgWatchContentChanged' | Out-Null
    Register-ObjectEvent -InputObject $script:fswContent -EventName Error   -Action $OnWatcherError -SourceIdentifier 'FsOrgWatchContentError' | Out-Null
}

function Unregister-Watchers {
    foreach ($id in 'FsOrgWatchCreated', 'FsOrgWatchRenamed', 'FsOrgWatchError',
                    'FsOrgWatchContentChanged', 'FsOrgWatchContentError') {
        Get-EventSubscriber -SourceIdentifier $id -ErrorAction SilentlyContinue |
            Unregister-Event -ErrorAction SilentlyContinue
    }
    foreach ($w in $script:fsw, $script:fswContent) {
        if ($w) { $w.EnableRaisingEvents = $false; $w.Dispose() }
    }
}

function Get-QueueCount {
    # Assigned before counting, for the same reason as everywhere else:
    # @(Read-JsonArray ...) would report 1 for a queue of any length.
    $entries = Read-JsonArray -Path $QueueFile
    return @($entries).Count
}

function Invoke-MissedFileSweep {
    <# Replay the arrivals whose events were dropped.

       Overflow means the OS discarded events, so the only way to learn
       what landed is to look. Each entry goes through Enqueue-File rather
       than any check of its own: that function already skips part-
       downloads, saved-page asset folders, and anything the index has
       already seen, waits for the file to stop growing, and dedupes
       against the queue. Reusing it means a sweep queues exactly what the
       lost events would have, and hidden entries stay excluded because
       Get-ChildItem skips them by default. #>
    $before = Get-QueueCount
    try {
        $entries = @(Get-ChildItem -LiteralPath $WatchDir -ErrorAction Stop)
    } catch {
        Write-Log "WARN sweep could not list ${WatchDir}: $_"
        return
    }
    foreach ($entry in $entries) { Enqueue-File -Path $entry.FullName }
    $added = (Get-QueueCount) - $before
    Write-Log ("Sweep after dropped events: examined {0} top-level entr(ies), queued {1} new." -f $entries.Count, $added)
}

try {
    # Both executables are resolved once, here, so a machine that cannot run
    # them says so at startup instead of accepting files forever and
    # silently dispatching nothing.
    $script:PythonExe = Resolve-Executable -Names @('python', 'python3', 'py') `
                                           -Candidates (Get-PythonCandidates) `
                                           -OverrideEnvVar 'FSORG_PYTHON'
    $script:ClaudeExe = Resolve-Executable -Names @('claude') -OverrideEnvVar 'FSORG_CLAUDE' -Candidates @(
        (Join-Path $env:USERPROFILE '.local\bin\claude.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe'),
        (Join-Path $env:APPDATA 'npm\claude.cmd')
    )

    if (-not $script:PythonExe) {
        Write-Log ("FATAL no usable python.exe found. The Microsoft Store alias stubs in " +
                   "WindowsApps are 0-byte redirectors, not Python. Install Python with " +
                   "'Add python.exe to PATH' ticked, or set FSORG_PYTHON to its full path. " +
                   "Watcher not started.")
        exit 1
    }
    if (-not $script:ClaudeExe) {
        Write-Log ("FATAL no usable claude.exe found on PATH or in the known install " +
                   "locations. Set FSORG_CLAUDE to its full path. Watcher not started.")
        exit 1
    }
    Write-Log "Using python: $($script:PythonExe)"
    Write-Log "Using claude: $($script:ClaudeExe)"

    $script:WatcherNeedsRestart = $false
    Register-Watchers

    Write-Log "Watcher started. Watching: $WatchDir (scope '$ScopeName', PID $PID)"

    # Main debounce/dispatch loop - runs forever, this IS the resident process.
    # Each iteration's body is its own try/catch: a transient error (e.g. a
    # race between Test-Path and Get-Item if the queue file is touched
    # mid-check) logs a warning and the loop continues, instead of an
    # uncaught exception silently killing the whole resident process the
    # way $ErrorActionPreference = 'Stop' would otherwise allow.
    while ($true) {
        try {
            Start-Sleep -Seconds $PollSeconds

            # Recovery runs on the loop thread, not in the Error handler: an
            # event action cannot safely dispose the watcher that raised it.
            if ($script:WatcherNeedsRestart) {
                $script:WatcherNeedsRestart = $false
                Write-Log "Restarting watchers after a dropped-events error."
                Unregister-Watchers
                Register-Watchers
                Invoke-MissedFileSweep
            }

            if (-not (Test-Path $QueueFile)) { continue }

            $lastWrite = (Get-Item $QueueFile).LastWriteTimeUtc
            $quietForSec = ((Get-Date).ToUniversalTime() - $lastWrite).TotalSeconds
            if ($quietForSec -lt $DebounceSeconds) { continue }

            if (-not (Get-QueueLock)) { continue }
            $batch = @()
            try {
                if (-not (Test-Path $QueueFile)) { continue }
                $batch = Read-JsonArray -Path $QueueFile
                if ($batch.Count -eq 0) { continue }
                Remove-Item $QueueFile -ErrorAction SilentlyContinue
            } finally {
                Release-QueueLock
            }

            # Select-Object -Unique as defense-in-depth: Enqueue-File already
            # dedupes on write, but this guarantees the dispatched prompt
            # itself can never list the same path twice regardless of how a
            # duplicate might have slipped into the queue file.
            $paths = @($batch | ForEach-Object { $_.path } | Where-Object { Test-Path $_ } | Select-Object -Unique)

            # Ask the index again, HERE, about every path in the batch.
            #
            # Enqueue-File asks the same question, but it asks the instant the
            # event fires - and a run moves a file first and updates the index
            # afterwards, so at that moment the index legitimately does not
            # know the file yet. Every file the watcher filed away therefore
            # queued itself straight back and earned a second headless session
            # over work already finished: the nested-watcher cascade, rebuilt
            # inside a single scope.
            #
            # By now that is no longer true. The dispatch below blocks the
            # loop until the session exits, so any events it caused piled up
            # in the queue while the index was being written, and this drain
            # is the first moment the index can answer accurately.
            $paths = @($paths | Where-Object {
                $rel = $_
                if ($_.StartsWith($WatchDir, [StringComparison]::OrdinalIgnoreCase)) {
                    $rel = $_.Substring($WatchDir.Length).TrimStart('\', '/')
                }
                if (Test-AlreadyIndexed -RelPath $rel) {
                    Write-Log "SKIP (indexed since it was queued - this run's own move): $_"
                    $false
                } else { $true }
            })
            if ($paths.Count -eq 0) { continue }

            $pathList = ($paths | ForEach-Object { "`"$_`"" }) -join ', '
            $runTs = Get-Date -Format 'yyyyMMddTHHmmss'

            # No index means this scope's structure has never been
            # confirmed by anyone, so there are no folder purposes to place
            # a file against. Deciding that unattended is precisely what
            # Watcher mode must not do - so don't dispatch at all. The
            # files stay where they landed; the next interactive Organize
            # run on this folder builds the index and confirms the initial
            # structure, and watcher dispatches resume normally after that.
            # The queue was already drained above, which is intended: these
            # paths are found by the interactive run's own directory scan,
            # not by replaying a stale queue.
            if (-not (Test-Path $IndexFile)) {
                Write-Log ("SKIP DISPATCH (no index at {0}) - {1} file(s) left in place for an " +
                           "interactive Organize run: {2}" -f $IndexFile, $paths.Count, $pathList)
                continue
            }

            Write-Log "DISPATCH batch of $($paths.Count) -> run $runTs-$ScopeName-watch: $pathList"

            # No run directory is named: step artifacts are disposable
            # working files, and the session log is the run's only record
            # (see SKILL.md). Naming one here is what previously caused
            # runs to leave fingerprints/plan/run JSON behind for good.
            $prompt = "Use the Skill tool to invoke the fs-organizer skill, " +
                "Watcher mode, for these new file(s) in ${ScopeName}: $pathList. " +
                "Write working files to a temporary directory; the session " +
                "log is the only record to keep."

            try {
                # PowerShell is allowed because this is a Windows host and the
                # run would otherwise burn turns on denied calls before
                # falling back to Bash.
                $r = Invoke-ClaudeHeadless -Prompt $prompt -Model 'sonnet' `
                    -AllowedTools 'Skill,Bash,PowerShell,Read,Write,Glob,Grep' -OutputFormat 'json' `
                    -WorkingDirectory $WatchDir
                Write-Log "RESULT (exit $($r.ExitCode)): $($r.StdOut) $($r.StdErr)"
            } catch {
                Write-Log "ERROR dispatching claude -p: $_"
            }
        } catch {
            Write-Log "WARN main loop iteration error (continuing): $($_.Exception.Message)"
        }
    }
} catch {
    Write-Log "FATAL: watcher process crashing: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
    throw
} finally {
    Write-Log "Watcher process exiting (PID $PID)."
    # One teardown, shared with the restart path, so a subscription added
    # to Register-Watchers can never be left behind by only one of them.
    Unregister-Watchers
    try { $WatcherMutex.ReleaseMutex() } catch {}
    $WatcherMutex.Dispose()
}
