# Organize mode — workflow steps (1–9)

Run these steps in order. No step is skipped, none are merged.

**Everything you need to call these scripts is written below — never open
a script's source to look up a flag.** Each step gives its exact command
line. Reading source costs a turn *and* parks a few thousand tokens in
context that are re-sent on every later turn.

**Chain aggressively.** Consecutive script-only steps go in ONE terminal
call joined with `&&`; only their one-line summaries come back. Every
extra call is another turn, and every turn re-sends the whole context.
Session-log entries chain on too (`scripts/session_log.py`), so logging
costs no turns at all — fold the two judgment steps' entries into the
next chain.

**Working files** (`<work>/…`) go in a temporary directory and are
discarded when the run ends. They are plumbing between scripts, not
records; the session log is the only record. Read one into context only
where a step says to.

---

## Steps 1–3 — Read context, fingerprint, find exact duplicates

One chained call. Note the run's start time first — UTC with an explicit
offset, e.g. `2026-08-02T10:00:00+00:00`; a naive local time shifts the
cutoff the closing usage report measures from.

```
python scripts/path_context_reader.py <scope> --recursive --root <scope> --output <work>/context.json && \
python scripts/content_fingerprint_extractor.py --from <work>/context.json --output <work>/fingerprints.json && \
python scripts/duplicate_candidate_scanner.py --from <work>/fingerprints.json --output <work>/duplicates.json && \
python scripts/classify_input.py --fingerprints <work>/fingerprints.json --duplicates <work>/duplicates.json --output <work>/to-classify.json && \
python scripts/session_log.py --scope <scope> --mode organize --step "1-3-context-fingerprint-duplicates" --command "<the chain above>" --input "<scope>" --output "<work>/duplicates.json, <work>/to-classify.json"
```

### Step 1 — Read context

- **Script:** `path_context_reader.py [--root ROOT] [--recursive] [--output OUTPUT] path`
  — `--recursive` walks a directory subtree; omit it for a single file.
- **Input:** the scope path.
- **Output:** `context.json` — `{scope, batches: [{folder, ancestors,
  files, excluded_files}]}`, folders deepest-first.

### Step 2 — Fingerprint

- **Script:** `content_fingerprint_extractor.py [--from FROM_] [--output OUTPUT] [files ...]`
  — `--from context.json` for a whole scope; bare file paths for one or
  two, which prints to stdout instead.
- **Input:** `context.json` (Step 1).
- **Output:** `fingerprints.json` — one record per file (title, first
  paragraph, word count, `sha256`, `modality`). Per-file read errors are
  isolated into `errors` and never abort the batch. **Do not read this
  file into context** — Step 3 consumes it, and Step 4 gets a trimmed
  version.

### Step 3 — Exact duplicates

- **Script:** `duplicate_candidate_scanner.py [--from FROM_] [--output OUTPUT] [folder]`
- **Input:** `fingerprints.json` (Step 2).
- **Output:** `duplicates.json` — two fields:
  - `exact_groups` — files sharing an identical `sha256`, grouped
    globally across the scope, so identical files in different folders
    are still caught. Each group names the newest copy to `keep` and the
    rest to `delete`. Carry those deletions into the plan; they apply
    without confirmation.
  - `survivors` — every file carrying on: those with no duplicate, plus
    each group's kept copy.

  Byte identity is the only duplicate test here. There is no fuzzy
  similarity scoring, so a file that isn't a byte-identical copy is just
  a distinct file — whether it *belongs* with another is decided at Step
  5, semantically, against folder purposes.

  Then `classify_input.py --fingerprints FINGERPRINTS --output OUTPUT
  [--duplicates DUPLICATES] [--exclude [EXCLUDE ...]]` writes
  `to-classify.json`: the surviving, non-empty files, carrying only the
  fields Step 4 needs. Dropping records for files that are about to be
  deleted is the point — in a duplicate-heavy scope that is most of them.

## Step 4 — Classify

- **Reference:** `references/naming-convention.md`, "Decision questions".
- **Input:** `to-classify.json` (Step 3) — **read this one into context**;
  it is the smallest honest form of what this judgment needs.
- **Output:** per file `{evergreen | time-bound, date?, keywords[2-4],
  content_identity}`, carried in context to Steps 5 and 6. Judgment step,
  no script. Draw keywords from content, never the old filename.

  **If `to-classify.json` holds more than ~100 files, delegate this step
  to a Sonnet subagent** (never a heavier model) that reads the file,
  classifies, and returns only `{path → keywords, date, evergreen}`. Past
  roughly that size the raw records cost more carried through Steps 5–9
  — context is re-sent every turn — than a subagent's cold start. Under
  ~50 files, always inline. In between, either is defensible; prefer
  inline.

## Step 5 — Place & structure

- **Reference:** the scope's purpose index at
  the scope's index (ask `fsorg_common.scope_state_dir(<scope>)` for the
  folder; never compose the path), plus
  `references/naming-convention.md`, "Folder naming".
- **Input:** Step 4's classifications, and the index's `{folder: purpose}`
  map — read the index here; it is the judgment this step exists to make.
- **Output:** `target_plan.json` — every file's destination (existing
  folder, new folder, or stays put); new/renamed folders each with a 1–2
  sentence purpose. If the scope had **no index** when the run started,
  mark the plan `first_run: true`.

  Judgment step, **inline by Sonnet** (a subagent, if used, is Sonnet
  too). Decide placement by matching each file's content understanding
  against the stated purposes of existing folders:
  - A file joins an existing folder only if that folder's purpose
    accurately describes it — don't force weak matches.
  - **Versions of one document are collected, never dropped.** Two or
    more files that are the same document at different versions or dates
    get a folder of their own, named per the convention. Look across the
    **whole scope** — copies are often at different depths. Each keeps
    its own version/date in its name. This holds even for identical
    filenames: Step 3 proved they aren't duplicates, so both survive.
  - A folder needs ≥ 2 files; anything without a genuine group stays at
    top level.
  - A reused folder's name and purpose must stay accurate for ALL its
    post-add contents — rename and reword if needed.
  - **A saved web page is two items that move as one**: `X.html` plus its
    `X_files/` asset folder. The scan already excludes the folder's
    contents (they're assets, not documents); plan the pair together —
    html renamed to convention, folder renamed to `<new-html-stem>_files`,
    both to the same destination — then rewrite the html's internal
    references with
    `saved_page.py fix-refs <html> --old "<old folder name>" --new "<new-stem>_files"`
    chained after the executor. Renaming the pair without the rewrite
    breaks the page: its hrefs name the folder literally.

## Steps 6–7 — Name, then execute

One chained call per batch of names, then the executor.

```
python scripts/folder_context_resolver.py <ancestors...> [--keywords KW ...] && \
python scripts/name_assembler.py assemble <keywords...> [--date YYYY-MM-DD] [--version V] [--ext .pdf] [--redundant TOKEN ...] && \
python scripts/session_log.py --scope <scope> --mode organize --step "6-name" --command "<...>" --input "<...>" --output "<...>"
```

### Step 6 — Name

- **Scripts:**
  - `folder_context_resolver.py ancestors [ancestors ...] [--keywords [KEYWORDS ...]]`
    — returns the redundancy set for the destination, and the stripped
    keywords if `--keywords` is given.
  - `name_assembler.py assemble <keywords...> [--date DATE] [--version VERSION] [--ext EXT] [--redundant ...]`
    — and `name_assembler.py parse <name>` for the inverse.
- **Reference:** `references/naming-convention.md` — read in full;
  downstream tooling regex-parses these names, so off-grammar breaks
  search.
- **Input:** `target_plan.json` (Step 5) for destination ancestors; Step
  4's keywords/date/version, already in context.
- **Output:** `names.json` — `{path: destination_with_new_name}` per file.
  On a collision between distinct files prefer an extra distinguishing
  keyword from content; `disambiguate()` is the last resort.
- Never hand-assemble or hand-parse a name.

### Step 7 — Execute

- **Script:** `batch_executor.py [--scope SCOPE] [--dry-run] plan_json`
- **Input:** `target_plan.json` + `names.json`, merged into `plan.json` —
  a **flat JSON array** of ops, never a wrapped object:
  `{"op":"mkdir","path":...}`, `{"op":"rename"|"move","src":...,"dst":...}`,
  `{"op":"delete","path":...}`. A move carries new folder AND new name in
  one `dst`. Order child renames before parent-folder renames. Always
  pass `--scope`.
- **Output:** the executor's journal plus an applied/failed summary. Every
  op auto-executes — moves, renames, new folders, and Step 3's deletions,
  which go to the Windows Recycle Bin. The one exception is a plan marked
  `first_run: true`: show the user the proposed structure, get one
  confirmation, then run the whole plan. Nothing is confirmed op-by-op.

## Steps 8–9 — Update the index, report

One chained call, plus a `set-purpose` per new or substantively changed
folder.

```
python scripts/index_manager.py build --scope <scope> --output <index-file> && \
python scripts/index_manager.py set-purpose <index-file> --folder <name> --purpose "<1-2 sentences>" && \
python scripts/usage_reporter.py --since <run-start> --cwd <workspace> --session-id <this session> && \
python scripts/session_log.py --scope <scope> --mode organize --step "9-report" --command "<...>" --output "<outcome>"
```

### Step 8 — Update the purpose index

- **Script:** `index_manager.py {build,lookup,update,set-purpose,rehash}`
  - `build --scope SCOPE --output OUTPUT` — refreshes hashes/membership;
    never clobbers existing purposes.
  - `set-purpose <index_path> --folder FOLDER --purpose PURPOSE` — for
    every folder Step 5 created, or whose contents changed enough that
    its old purpose no longer covers them.
  - `update <index_path> --file FILE --scope SCOPE [--folder FOLDER]`,
    `lookup <index_path> --file FILE`, `rehash <index_path> --file FILE --scope SCOPE`.
- **Input:** the post-Step-7 state of the scope; Step 5's purpose strings.
- **Output:** the index, current with what's on disk. The index file is
  `scope_state_dir(<scope>) / "index.json"`. Never build that path by
  hand: the folder is the scope's leaf name plus a digest of its full
  path, so two folders sharing a leaf name do not share state.

### Step 9 — Report

- **Script:** `usage_reporter.py --since SINCE [--project-dir DIR] [--cwd CWD] [--session-id ID]`
  — pass `--cwd` as the directory this session is running in. In Organize
  mode a human may well have other Claude sessions open in that same
  directory; if the figure looks inflated, that is why, and
  `--session-id` narrows it. Don't hunt for your own id to satisfy the
  flag — report the number and note the caveat the script prints.
- **Input:** the run's start timestamp; the Step 7 journal.
- **Output:** the summary to the user — what moved, what was renamed,
  which folders were created, what was deleted (and that it went to the
  Recycle Bin), plus flags such as empty or unreadable files — and the
  token/cost figures, always with the caveat that costs are
  API-list-equivalent, not a subscription charge. Close the session log
  with a final entry. Always runs, even if nothing changed.
