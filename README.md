# fs-organizer

**Windows only.** A Claude Code skill that organizes a folder: it reads what
each file actually contains, renames it to one consistent grammar, groups
related files, and removes byte-identical copies to the Recycle Bin.

Point it at Downloads, a scans folder, a decade of loose documents — anything
where the filenames stopped meaning much.

```
Invoice #4471 (final) .pdf          ->  invoice-acme-hosting-2024-03-11.pdf
Screenshot 2024-03-11 at 14.22.png  ->  screenshot-login-error-2024-03-11.png
報告書 2024.txt                       ->  報告書-2024.txt
résumé café.txt                     ->  resume-cafe.txt
receipt.pdf, receipt copy.pdf       ->  one kept, the copy recycled
```

## What it will and will not do

This skill moves and deletes real files, so its limits are worth stating
plainly before you install it.

- **Only byte-identical copies are ever deleted.** Two files with the same
  SHA-256 are the same bytes, so the newest is kept and the rest go to the
  Recycle Bin. Files that merely share a *name* are treated as versions and
  collected together — never overwritten, never removed.
- **Deletion is always to the Recycle Bin**, never an unlink. If the Recycle
  Bin cannot be reached the operation fails and is reported; there is no
  permanent-delete fallback.
- **Every change goes through one executor**, which rejects any operation
  whose paths fall outside the folder you named, refuses to overwrite an
  existing file, isolates per-file errors, and journals what it did so a
  re-run is a no-op rather than a second pass.
- **It stays inside the folder you point it at.** Junctions and symlinks are
  never followed, so a link inside the folder cannot pull in — or act on —
  files that live elsewhere.
- **Hidden and system files are left alone**, along with `.git`,
  `node_modules`, `__pycache__`, virtualenvs, and Office `~$` lock files.
- **Empty (0-byte) files are never renamed or deleted.** They carry no
  content to name from, so they are flagged for you instead.
- **Only fingerprints reach the model.** A capped excerpt — a title, a first
  paragraph, an EXIF date — is extracted locally per file. Whole documents are
  never sent.

## Install

```
/plugin marketplace add rarsahki/fs-organizer
/plugin install fs-organizer@fs-organizer-tool
```

Then just ask:

```
organize my Downloads folder
what should this file be called?
clean up D:\Scans\Receipts
```

The first run against a new folder proposes a structure and waits for your
confirmation. After that the folder has an index and later runs proceed on
their own.

## Requirements

Python 3.9+ on PATH. That is the whole hard requirement — **there is nothing
to `pip install`** to get a working skill, and the plugin system does not
install packages for you.

Optional packages only improve the *names* the skill proposes, by letting it
read a file instead of judging it by its filename and folder:

```
pip install -r requirements.txt
```

| Package | What it adds | Without it |
|---|---|---|
| `pypdf` | reads PDF text | PDFs named from filename + folder context |
| `python-docx` | reads DOCX text | Word docs named from filename + folder context |
| `Pillow` | reads EXIF | photos dated from the filesystem, not the shutter |

A run tells you when this happened, so quality never degrades silently:

```
note: named without reading content — 2 file(s) need python-docx.
Install with: pip install python-docx
```

## Naming convention

One grammar for everything the skill produces:

```
keyword-keyword[-more-keywords][-YYYY-MM-DD][-vNN|-draft|-final].ext
```

2–4 content words, a date only for time-bound items, a version only when a
conceptual duplicate exists. Names are parsed back by the same module that
builds them, so downstream search can rely on the shape.

Keywords may be written in **any script**. `報告書 2024.txt` becomes
`報告書-2024.txt`, not `2024.txt`. Latin accents are folded (`café` → `cafe`)
because Latin text is routinely searched without them; other scripts keep
their own characters, including the combining marks that carry the word in
Devanagari, Tamil, Thai, Arabic and Hebrew. Dates and version tags stay ASCII,
since those fields are parsed rather than read.

Full rules: [`skills/fs-organizer/references/naming-convention.md`](skills/fs-organizer/references/naming-convention.md).

## Optional: organize new downloads automatically

A resident watcher can organize files as they land, without you asking. It is
**not** enabled by installing the plugin — it is a separate, deliberate setup
step, and the folder must already have been organized once interactively so an
index exists.

**The easiest way is to say yes when asked.** After organizing a folder, the
skill offers to keep watching it — that ordering is not arbitrary, since the
watcher only dispatches for a folder that already has an index.

To set it up by hand, from a clone of this repo:

```
powershell -ExecutionPolicy Bypass -File skills\fs-organizer\scripts\fs-organizer-watch-setup.ps1 -WatchDir "D:\Scans"
```

`-Uninstall` stops watching a folder; `-Force` skips the absorb prompt.

**No Administrator prompt.** It starts from HKCU's `Run` key rather than a
scheduled task, because `schtasks /sc onlogon` fails with "Access is denied"
for a normal user, and an optional convenience should not require elevation.

**Nested watches are not allowed.** Watching `Downloads` and
`Downloads\Receipts` separately runs *two* headless sessions over one file:
the first files a download into Receipts, the second reads that as an arrival
and repeats the work. So watching a folder absorbs any watch already
registered inside it — carrying that folder's purposes into the outer index —
and watching a folder already covered by an outer watch is refused with an
explanation.

**Plugin updates don't break it.** Plugins install under a version-stamped
directory (`...\fs-organizer\0.1.0\...`) that Claude Code reclaims once
superseded, so autostart pointing straight at it would work until the first
update and then stop silently. Setup installs a launcher at `~/.fs-organizer/`
that resolves the current version at every launch.

Start it without logging out:

```
wscript.exe "%USERPROFILE%\.fs-organizer\fs-organizer-watch-stable-launcher.vbs" "%USERPROFILE%\Downloads"
```

The watcher resolves `python.exe` and `claude.exe` at startup and refuses to
run if it cannot find real ones, rather than accepting files forever and
dispatching nothing. `FSORG_PYTHON`, `FSORG_CLAUDE` and `FSORG_WATCHER`
override that search.

Logs: `~/.fs-organizer/logs/watcher-<scope>.log`, plus `watch-resolve.log`
for which version the launcher picked.

## Where state lives

Everything is per-folder, under that folder's own state directory:

```
~/.fs-organizer/<folder-name>/
├── index.json                    what each subfolder is for, plus content hashes
├── session-file-<YYYY-MM-DD>     every step of every run, with exact commands
└── journals/<timestamp>.json     every filesystem operation and its outcome
```

Nothing is written into the folder being organized.

## Tests

```
python tests\test_windows_edges.py
powershell -ExecutionPolicy Bypass -File tests\test_watcher_queue.ps1
powershell -ExecutionPolicy Bypass -File tests\test_watcher_resolve.ps1
powershell -ExecutionPolicy Bypass -File tests\test_watcher_live.ps1   # ~90s
```

No test framework needed. Each check corresponds to a real defect — junction
traversal, 0-byte Store stubs shadowing Python, PowerShell 5.1 dropping queue
entries, MAX_PATH, case-only renames, combining marks, locked files, scopes
sharing state because they shared a leaf name — so they exist to keep those
from returning.

Two of them are deliberately not mocked, because mocking would have hidden the
bug they exist for. The deletion tests really recycle their temp files, since
faking the Recycle Bin proves nothing about recoverability. And
`test_watcher_live.ps1` starts a real watcher process and waits out real
debounce windows — which is the only reason the self-move re-dispatch was
found: the watcher moved a file into a subfolder, saw its own move as an
arrival, and started a second session over finished work. Every unit-level
test of that guard passed.

## License

MIT — see [LICENSE](LICENSE).
