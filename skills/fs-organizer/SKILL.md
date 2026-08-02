---
name: fs-organizer
description: >
  Analyzes, renames, de-duplicates, and reorganizes files and folders using
  content understanding plus deterministic scripts, then executes the
  approved plan. Use this skill whenever the user mentions organizing,
  cleaning up, tidying, renaming, versioning, or de-duplicating files,
  folders, or directories, checking names against a naming convention, or
  asks "what should this file be called" — even if they don't explicitly
  name the skill. Accepts a single file, multiple files, or a directory.
allowed-tools: Bash(python:*), Bash(python3:*), PowerShell, Bash(powershell:*), Read, Glob, Agent
---

# fs-organizer

## Modes

fs-organizer runs in exactly one of two modes on any given invocation. What
triggers the run — not how much work it ends up doing — is what decides
which mode applies.

- **Organize mode.** Used whenever a human directly asks for the skill by
  typing a request: to organize, clean up, tidy, rename, version, or
  de-duplicate a file, multiple files, or a directory, or to check/propose
  names against the naming convention. This is the default mode, and the
  only mode a human ever invokes directly. The specific request made
  decides how much of the run happens — a full "organize" request analyzes,
  de-duplicates, renames, and reorganizes the given scope; a "name-only"
  request only proposes and applies names, and never restructures or moves
  anything into new folders. Both are Organize mode; "name-only" is a
  request type within it, not a separate mode.
- **Watcher mode.** Used exclusively when an OS-level watcher process
  invokes the skill headlessly because new file(s) landed in a watched
  folder. This headless, OS-triggered invocation is the *only* way to enter
  Watcher mode.

**How to tell which mode you're in:** if a human is typing the request in
an interactive session, you are in Organize mode — always, even if the
request only concerns a single file. If the invocation is a headless call
whose prompt explicitly identifies itself as the watcher and supplies one
or more newly-landed file paths, you are in Watcher mode. There is no other
way to enter Watcher mode, and a human can never trigger it directly.

## Input & Output

### Organize mode

**Input:**
- A path: a single file, multiple files, or a directory (the *scope*).
- The request type, taken from what the user actually asked for:
  - **organize** (default) — analyze, de-duplicate, rename, and restructure
    the scope.
  - **name-only** — check and propose names only; nothing is restructured
    or moved into a new folder.

**Output:**
- The scope's files and folders on disk, reorganized (or, for a name-only
  request, just renamed) to match the naming convention and whatever
  structure the run decided on.
- That scope's purpose index, created or updated to reflect the result.
- A session log recording every step the run performed.
- A summary reported back to the user describing what changed. If the
  scope had no index when the run started, the proposed structure is shown
  and confirmed once before anything is created; a scope that already has
  an index is organized without any confirmation.

### Watcher mode

**Input:**
- One or more newly-landed file paths in a watched folder, supplied by the
  OS-level watcher process that invoked the skill headlessly.
- The watched folder's existing index. Watcher mode only ever runs against
  a scope that already has one; a watched folder with no index is not
  organized headlessly at all, and waits for an interactive Organize-mode
  run instead.

**Output:**
- Each supplied file placed into its correct location — an existing folder,
  or a newly-created one — and renamed to match the naming convention.
  Nothing waits for confirmation: the scope is indexed, so the run
  completes on its own.
- The same kind of purpose-index and session-log updates as Organize mode,
  for the watched folder's scope.

## Rules

1. **Decide the mode type first.** Determine whether this is Organize mode
   or Watcher mode, using the "How to tell which mode you're in" test
   above, before doing anything else.
2. **Follow that mode's workflow steps sequentially.** Once the mode is
   decided, run its workflow steps in order, start to finish, with no step
   skipped and no two steps merged into one.

**Model:** everything in this skill runs on **Sonnet** — orchestration and
every judgment step, made inline. If a subagent is ever used (e.g. for a
very large scope), it must also run Sonnet; never delegate to a heavier
model.

Naming rules live in `references/naming-convention.md`. Read that file
before proposing any name and follow its grammar exactly — downstream
tooling regex-parses these names into search fields, so an off-grammar
name breaks search. Never hand-assemble or hand-parse names;
`scripts/name_assembler.py` exists so the grammar has exactly one
implementation.

## Storage — per-scope state

All persistent state for a scope lives in one folder named after the
scoped directory: `~/.fs-organizer/<scope-name>/` (e.g. the Downloads
scope's state is `~/.fs-organizer/Downloads/`). It holds:

- `index.json` — the scope's purpose index: a `{folder: purpose}` map,
  where a purpose is a 1–2 sentence description of what belongs in that
  folder, plus the content hashes used to detect byte-identical
  duplicates.
- `session-file-<YYYY-MM-DD>` — the scope's session log, described in the
  next section. Headless (Watcher mode) runs log here too, never to a
  shared global location.
- `journals/<timestamp>.json` — one file per `batch_executor` run,
  recording every operation attempted and its outcome. This is what makes
  a re-run idempotent and a mistake traceable; it belongs to the scope
  like everything else here, not to a shared pile.

A run for a scope never writes its state under another scope's folder or
at the `.fs-organizer` top level.

**Any scope, any machine.** A scope is just a directory — `Downloads` is
only the common example, not a requirement. `<scope-name>` is that
directory's own leaf name, so `D:\Scans\Receipts` keeps its state in
`~/.fs-organizer/Receipts/`. Nothing in this skill hardcodes a user name,
a drive, or an install location: resolve the state root from the current
user's home directory, and resolve the skill's own scripts relative to
this file. That is what lets the same skill run unchanged on any Windows
system and under any account.

**Working files are not records.** The scripts pass data to each other
through JSON files (`--from`/`--output`, the executor's plan file) because
that is how one script hands its result to the next. Write those to a
temporary working directory, and let them be discarded when the run ends —
they exist to plumb one script into the next, not to document the run.
Never read a working file back into context to "check" a step; the script
that consumed it already reported what happened. The session log is the
run's only record.

**Keep bulk data out of context.** Where consecutive steps are
deterministic, chain them into a single terminal call
(`cmd1 && cmd2 && cmd3`), so the scripts hand off through their working
files and only their one-line summaries come back. Read a working file's
full contents into context only where a step says its judgment needs
them.

## Session log

`~/.fs-organizer/<scope-name>/session-file-<YYYY-MM-DD>` — one file per
scope per date, appended to by every run that day, in both modes.

Write entries with `scripts/session_log.py`, **chained onto the step's own
command** with `&&` rather than as a separate call:

```
python scripts/<step-script>.py ... && python scripts/session_log.py --scope <scope> --mode <mode> --step "3-exact-duplicates" --command "<verbatim>" --input "<...>" --output "<...>"
```

A separate write per step costs one tool call per step — ten of them in a
nine-step run, a third of the run's turns. Chained, they cost none. The
script stamps its own UTC time, so no "what time is it" call is needed
either. For the two judgment steps, which run no script of their own,
fold their entries into the next step's chain.

Each entry records:

- the timestamp — UTC with an explicit offset, written by the script
  itself so a naive local time cannot get in;
- the mode and the step number and name;
- **the exact command line the step ran**, verbatim. This is what makes a
  headless run auditable after the fact and an interactive one
  reproducible later;
- the step's input and output artifacts, named with their locations —
  including working files (so a failure can be traced), the files
  actually moved, renamed, or deleted on disk, and the index file.

The run's first entry records its start (timestamp, mode, scope path);
its last records the outcome. A step with no entry is a step that did not
finish — an interrupted run starts over from the beginning, since no
intermediate state is preserved between runs. Re-running is safe: the
executor's journal makes filesystem operations idempotent, so anything
already applied is recorded as `already-applied` and skipped rather than
done twice.

The log is where this detail belongs — don't echo terminal output back to
the user. Tell them what happened in plain language ("12 files moved, 3
duplicates sent to the Recycle Bin"), and leave the commands and full
paths in the log for whoever needs them.

## Workflow

Each mode's workflow steps live in their own reference file. Read the file
for the mode you decided on, and execute its steps in order:

- **Organize mode:** `references/organize-workflow.md` (Steps 1–9).
- **Watcher mode:** `references/watcher-workflow.md` (Steps 1–9).

The two files share the same nine step names and the same per-step
template (Script/Reference, Input, Output); they differ in how each step
is carried out for their mode.

Everything executable lives in `scripts/` — the Python the steps call, and
the files that make Watcher mode possible: `fs-organizer-watch.ps1` (the
resident OS-level watcher), `fs-organizer-watch-launcher.vbs` (a hidden
launcher for running it from a checkout), and
`fs-organizer-watch-setup.ps1` (turns the watch on for a folder). The
watcher is not invoked by the workflow — it is what *starts* a
Watcher-mode run.

## Offering the watcher (Organize mode only)

At the end of a successful Organize-mode run, when all of these hold:

- the run was interactive (never in Watcher mode),
- the scope now has an index — which it does after any completed run, and
  which the watcher requires before it will dispatch anything,
- and the scope is not already watched, per
  `python scripts/watch_registry.py check --root <scope>` returning no
  `already` and no `covered_by`,

then tell the user the folder can be kept organized automatically as files
land, and ask. Ask once; if they decline, do not raise it again in the same
session. On yes:

```
powershell -ExecutionPolicy Bypass -File scripts/fs-organizer-watch-setup.ps1 -WatchDir "<scope>"
```

Report back what it printed — where it will log, and how to stop it.

The setup script owns every decision here, so do not reimplement any of
it: it refuses a folder already covered by an outer watch, absorbs any
watch nested inside this one (carrying that scope's folder purposes into
this one's index), installs its launcher at a fixed path so a plugin
update cannot break it, and starts it from HKCU's Run key so no
Administrator prompt is ever needed. If it exits non-zero, relay its
message rather than retrying or working around it — a refusal is the
script preventing two watchers from dispatching twice over one file.

Never run it unasked, and never in Watcher mode.

## Guardrails

These hold across every step of both workflows.

- **Never touch the filesystem by hand.** Every change to a file or folder
  goes through `scripts/batch_executor.py` — no improvised `Move-Item`,
  `ren`, `del`, `mkdir`, `robocopy`, or shell equivalent, in any language,
  ever. This is not a style preference: the executor is where scope
  enforcement, Recycle-Bin deletion, the journal, idempotent re-runs,
  case-only-rename handling on Windows, and per-file error isolation all
  live. A hand-written command has none of them, so a single typo'd path
  becomes an unrecoverable, unlogged, out-of-scope change. The same
  applies to the skill's other scripts: use `name_assembler.py` for names,
  `index_manager.py` for the index, and the file tools (Read, Glob, Grep)
  to inspect — reach for a raw shell command only when no script or tool
  covers what you need, and never to mutate.
- **An indexed scope runs fully automatically.** Once a scope has an
  index, every operation — moves, renames, new folders, and the deletions
  described below — executes as soon as it is decided, in either mode,
  with no confirmation.
- **A scope with no index is confirmed once, interactively.** The first
  run against a scope has no purposes to place files against, so it
  proposes the whole structure, shows it to the user, and creates it only
  after one confirmation. That run therefore cannot be headless: if the
  index is missing, the work belongs in an interactive Organize-mode
  session. After that first run the index exists, and every later run is
  automatic.
- **Only byte-identical copies are ever deleted.** If two files have the
  same SHA-256 they are the same bytes, so keeping the newest loses
  nothing — the rest are deleted without confirmation. Nothing else is
  ever removed. In particular, **files that merely share a name are not
  duplicates**: different content means a different version, so they are
  versioned and collected together rather than one replacing the other.
  Deletions go to the Windows Recycle Bin (never an unlink), which is what
  makes a wrong call recoverable; if that mechanism is unavailable the
  operation fails and is reported rather than falling back to a permanent
  delete. Recycling needs no third-party package — the executor calls the
  shell's own `SHFileOperationW` — so never treat a delete failure as
  "install something first".
- **Stay inside the given scope.** A file input scopes to its parent
  folder; a directory input scopes to its subtree. Always pass `--scope`
  to the executor: it hard-rejects any operation whose paths fall outside
  that directory, deletions included. Moving items to folders *outside*
  the scope needs a global index of the whole filesystem to do well —
  treat it as out of scope until such an index exists.
- **Skip excluded items** (hidden/system files, `.git`, `node_modules`,
  etc. — the list is in `scripts/fsorg_common.py`): renaming
  infrastructure breaks tools that depend on those exact names.
- **Send fingerprints to the model, never full file contents.** The
  user's files may be sensitive; the extractor caps what is read out of
  each file, and that cap is the privacy guarantee.
- **Report what could not be read.** The PDF, DOCX, and EXIF parsers are
  optional, and each falls back to naming from the filename and folder
  instead. That is a quieter, worse result, not a failure — so when
  `content_fingerprint_extractor` reports files named without reading
  their content, pass that on in the summary along with the one-line
  install command. Never present such a name as content-derived.
- **Never rename or delete an empty file.** A 0-byte file (usually a
  failed download or accidental save) has no content signal, so any name
  derived from it would launder a broken file into a legitimate-looking
  one, and it has no hash to make a duplicate judgment from. Leave it
  where it sits and flag it in the report for the user to deal with.
- **A folder's purpose is written only after that folder exists.** Never
  record a purpose for a folder that is still just a proposal, and keep
  a reused folder's name and purpose accurate for everything it holds
  after the run.
