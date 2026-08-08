# Watcher mode — workflow steps (1–9)

Run these steps in order for the newly-landed file(s) the watcher
supplied. No step is skipped, none are merged. Same nine step names and
same shape as Organize mode — only the inputs differ, because placement
here is judged against an index rather than a fresh scan.

**Everything you need to call these scripts is written below — never open
a script's source to look up a flag.** Reading source costs a turn *and*
parks thousands of tokens in context that are re-sent every later turn.

**Chain aggressively.** Consecutive script-only steps go in ONE terminal
call joined with `&&`, session-log entries included
(`scripts/session_log.py`), so logging costs no turns. Fold the two
judgment steps' entries into the next chain.

**Working files** (`<work>/…`) go in a temporary directory and are
discarded when the run ends. The session log at
`scope_state_dir(<scope>) / "session-file-<YYYY-MM-DD>"` is the only
record — the same file Organize-mode runs append to, never a shared
global location. `session_log.py` resolves that path itself; pass it the
scope and never compose the path.

---

## Steps 1–3 — Read context, fingerprint, look up exact duplicates

### Step 1 — Read context (precondition check)

In Watcher mode the context is the purpose index, not a directory scan.
The index is `scope_state_dir(<scope>) / "index.json"` — for Downloads,
`~/.fs-organizer/Downloads-24354063/index.json`. Ask
`fsorg_common.scope_state_dir` for it rather than composing the path: the
folder is the scope's leaf name plus a digest of its full path, so two
folders sharing a leaf name do not share an index.

- **Input:** the watched directory.
- **Output:** confirmation that the index file exists — nothing more.
  **Do not read it into context**; it holds every known file's hash and
  runs to hundreds of kilobytes. Step 3's lookup returns the
  `folder_purposes` map and `loose_files` list, which is the only part
  any judgment needs.

**If the index does not exist, stop here.** Do not build one, do not
fingerprint, do not move anything. A scope with no index has no purposes
to place files against, so its structure has never been confirmed by
anyone — deciding it unattended is exactly what Watcher mode must not do.
Log that the run stopped for a missing index, name the scope, and exit.
The files stay where they landed until someone runs Organize mode on that
folder interactively, which builds the index and confirms the initial
structure. Every later watcher run on that scope proceeds normally.

Do not rebuild an existing index routinely either — every run, in either
mode, updates it incrementally as its closing act.

Then note the run's start time (UTC with an explicit offset, same rule as
Organize mode) and run Steps 2 and 3 as one chained call:

```
python scripts/content_fingerprint_extractor.py <new-file> [<new-file> ...] --output <work>/fingerprints.json && \
python scripts/index_manager.py lookup <index-file> --file <new-file> && \
python scripts/classify_input.py --fingerprints <work>/fingerprints.json --exclude <any duplicate paths the lookup resolved> --output <work>/to-classify.json && \
python scripts/session_log.py --scope <watched-dir> --mode watcher --step "1-3-context-fingerprint-duplicates" --command "<the chain above>" --input "<new file paths>" --output "<work>/to-classify.json"
```

Run the `lookup` once per new file when several arrived.

### Step 2 — Fingerprint

- **Script:** `content_fingerprint_extractor.py [--from FROM_] [--output OUTPUT] [files ...]`
- **Input:** the newly-landed path(s) from the watcher's prompt — files
  **or folders**; the watcher queues both. A directory (an extracted
  archive, a copied project) fingerprints as `modality: "directory"` with
  entry names and counts — a shallow signal, never a per-file crawl.
- **Output:** `fingerprints.json` — one record per item; files carry a
  `sha256`, directories don't (byte identity has no meaning for one).
  **Write it to a file; do not let it into context.** A file that turns
  out to be a duplicate or 0-byte would otherwise sit in context for the
  rest of the run, re-sent every turn, for nothing. Step 4 reads a
  trimmed version instead.

### Step 3 — Exact duplicates

An index lookup here, not a scope scan.

- **Script:** `index_manager.py lookup <index_path> --file FILE`
- **Input:** each new file; the purpose index.
- **Output:** per file:
  - `exact_duplicate` — an existing file with the same `sha256`, verified
    live (the candidate is re-hashed from disk before being trusted; a
    stale index hash self-heals, reported as `index_repaired: true`). On
    a verified hit: **keep the newer copy, mark the older for deletion**,
    applied without confirmation when the plan runs.
  - `folder_purposes` — the full `{folder: purpose}` map, and
    `loose_files` — current top-level filenames. Both carry forward to
    the placement decision.
  - No sha256 match simply means new content; the file carries on as an
    ordinary distinct file. Byte identity is the only duplicate test in
    this workflow — there is no fuzzy similarity scoring anywhere in it.
  - Directories have no hash and skip duplicate detection entirely; run
    the `lookup` only for files.

  Then `classify_input.py --fingerprints FINGERPRINTS --output OUTPUT
  [--duplicates DUPLICATES] [--exclude [EXCLUDE ...]]` writes
  `to-classify.json` — the surviving, non-empty files with only the
  fields Step 4 needs. Pass resolved duplicates to `--exclude`.

## Step 4 — Classify

- **Reference:** `references/naming-convention.md`, "Decision questions"
  — re-read it on every watcher invocation; a fresh headless dispatch has
  no memory of a prior read, and nothing enforces the date-sourcing
  hierarchy mechanically.
- **Input:** `to-classify.json` (Step 3) — read this into context.
- **Output:** per file `{evergreen | time-bound, date?, keywords[2-4],
  content_identity}`. Judgment step, no script, inline — a watcher batch
  is 1–3 files, far under the size where delegating would pay for itself.

## Step 5 — Place & structure

- **Reference:** Step 3's `folder_purposes` map and `loose_files` list,
  plus `references/naming-convention.md`, "Folder naming".
- **Input:** Step 4's classification per file.
- **Output:** per file, a placement decision:
  - Fits an existing folder's stated purpose — a genuine semantic match,
    not a topical-adjacency stretch → move it there.
  - No fit, but the new file plus an existing loose file share a real,
    describable subject (≥ 2 files) → a new folder for both, with a 1–2
    sentence purpose. Create it; the scope is indexed, so this needs no
    confirmation.
  - Another version of something already here — same document, different
    version or date, **including an identical filename** → keep both and
    put the series in one folder for that subject, named per the
    convention, each file keeping its own version/date. A same-named
    arrival never replaces what is on disk: Step 3 established it isn't a
    byte-identical copy, so it is a new version, not a duplicate.
  - No match and nothing to cluster with → stays loose, renamed only.
  - **An arriving directory is placed as one unit** — judged by its name
    and entry sample, moved (and renamed to the folder convention) whole.
    Never unpack it or organize its contents here; that is an
    Organize-mode job the user asks for explicitly.
  - **A saved web page is two items that move as one**: `X.html` plus its
    `X_files/` asset folder (the folder may even be in the same batch —
    browsers write it first). Plan them together: html renamed to
    convention, folder renamed to `<new-html-stem>_files`, both to the
    same destination. Never place the asset folder alone, and never treat
    its contents as documents.

  Judgment step, **inline by Sonnet** — never delegate to a heavier
  model. Decide a new folder's purpose string here but treat it as part
  of the proposal: it is recorded only once that folder actually exists.

## Steps 6–7 — Name, then execute

```
python scripts/folder_context_resolver.py <ancestors...> [--keywords KW ...] && \
python scripts/name_assembler.py assemble <keywords...> [--date YYYY-MM-DD] [--version V] [--ext .pdf] && \
python scripts/batch_executor.py <work>/plan.json --scope <watched-dir> && \
python scripts/session_log.py --scope <watched-dir> --mode watcher --step "6-7-name-and-execute" --command "<...>" --output "<...>"
```

For a saved page, append the reference rewrite to the same chain, AFTER
the executor has moved and renamed the pair:

```
... batch_executor.py ... && \
python scripts/saved_page.py fix-refs <dest>/<new-stem>.html --old "<old folder name>" --new "<new-stem>_files" && \
python scripts/session_log.py ...
```

The html's hrefs reference the asset folder by literal name, so renaming
the folder without rewriting them breaks the page (Explorer breaks saved
pages exactly this way). `fix-refs` handles both the raw and URL-encoded
forms of the name.

### Step 6 — Name

- **Scripts:**
  - `folder_context_resolver.py ancestors [ancestors ...] [--keywords [KEYWORDS ...]]`
  - `name_assembler.py assemble <keywords...> [--date DATE] [--version VERSION] [--ext EXT] [--redundant ...]`
    — `name_assembler.py parse <name>` is the inverse.
- **Reference:** `references/naming-convention.md` — read in full, not
  skimmed: the grammar, the redundancy rule, the collision handling.
- **Input:** each file's Step 5 destination and Step 4 keywords/date.
- **Output:** the final destination-path-plus-name per file. Never
  hand-assemble or hand-parse a name.

### Step 7 — Execute

- **Script:** `batch_executor.py [--scope SCOPE] [--dry-run] plan_json`
- **Input:** the Step 5/6 decisions as a flat `plan.json` ops array, same
  format as Organize mode's Step 7.
- **Output:** the executor's journal. Every op auto-executes — moves into
  existing folders, new folders, renames, and Step 3's duplicate
  deletions, which go to the Windows Recycle Bin. Nothing is held for
  confirmation: Step 1 established this scope is indexed, and an indexed
  scope runs unattended end to end.

## Steps 8–9 — Update the index, report

```
python scripts/index_manager.py update <index-file> --file <current-path> --scope <watched-dir> [--folder <name>] [--previous <path-before-this-run>] && \
python scripts/index_manager.py set-purpose <index-file> --folder <name> --purpose "<1-2 sentences>" && \
python scripts/usage_reporter.py --since <run-start> --cwd <watched-dir> && \
python scripts/session_log.py --scope <watched-dir> --mode watcher --step "9-report" --command "<...>" --output "<outcome>"
```

### Step 8 — Update the purpose index

- **Script:** `index_manager.py update <index_path> --file FILE --scope SCOPE [--folder FOLDER] [--previous REL]`
  per file — **including files that stayed loose**, so later arrivals can
  find them as clustering partners. For a folder created at Step 7, also
  `index_manager.py set-purpose <index_path> --folder FOLDER --purpose PURPOSE`;
  nothing else ever writes that purpose.
- **Pass `--previous` whenever Step 7 moved or renamed a file that the
  index already knew about**, giving its path relative to the scope as it
  was BEFORE this run. `--file` is the post-move path, and the old entry
  cannot be found from that: `loose_files` holds bare names, and usually
  both the name and the folder changed. Organize mode gets away without it
  because Step 8 there rebuilds the whole index from disk; Watcher mode
  never rebuilds, so a file filed away without `--previous` leaves its old
  name in `loose_files` permanently, and later arrivals are clustered
  against a file that is no longer there.
- **Input:** each file's final location after Step 7.
- **Output:** the index, current with what's on disk. Always runs,
  whatever Step 7 did. If the run placed a **directory**, use
  `index_manager.py build --scope <watched-dir> --output <index-file>`
  instead of per-file `update` — a folder arrival changes folder
  membership in ways `update`'s single-file bookkeeping doesn't model,
  and `build` never clobbers purposes.

### Step 9 — Report

- **Script:** `usage_reporter.py --since SINCE [--project-dir DIR] [--cwd CWD] [--session-id ID]`
  — pass `--cwd <watched-dir>`, which is this run's own working directory.
  The watcher pins it there, so that transcript folder holds only watcher
  runs for this scope, and `--since <run start>` already narrows to this
  run. **Do not go looking for your own session id** — `--session-id` is
  available for the rare case of another session sharing the directory,
  not something a run needs to discover about itself.
- **Input:** the run's start timestamp; the Step 7 journal.
- **Output:** the run's outcome in the session log's final entry —
  placements made, folders created, deletions, flags — plus the
  usage/cost figures. Always runs, even when nothing changed.
