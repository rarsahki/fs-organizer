"""index_manager — persistent scope index for watcher mode.

v2: purpose-based, not token-bag-based. The original design stored a
content-derived token bag per file and per folder, built by fingerprinting
every file's content. Two problems with that: (1) it required re-parsing
every file's actual content (pypdf/PIL etc.) just to build a lexical
matching signal, and (2) token-overlap is a weak proxy for "does this file
belong here" - it misses synonyms and false-positives on generic shared
words. A folder's PURPOSE - a short human/LLM-authored description of what
belongs there - is both cheaper to maintain and a genuinely better signal,
since matching a new file against it is a semantic judgment an LLM can just
make directly (the list of purposes is small enough to read in full), not
something a keyword-overlap script should approximate.

What's still deterministic and script-owned: sha256-based exact-duplicate
detection. There's no way around persisting content hashes for that - a
purpose description can't tell you two files are byte-identical. But a
stored hash can go stale if a file already in the index is edited in
place after being indexed (rare for the kinds of scope this watches -
mostly write-once documents/receipts/photos, where versioning happens via
new files with vNN suffixes rather than in-place edits - but not
impossible). `lookup()`
re-verifies a hash hit live (one re-hash, not a full rescan) before
trusting it, and self-heals the index if the stored hash no longer
matches. This catches the false-positive case (a new file wrongly flagged
as a duplicate of something that has since changed); it is NOT a
comprehensive staleness tracker - it only fires opportunistically, when a
new file's hash happens to collide with a now-wrong stored one. The real
correctness backstop for edited-in-place files nobody's downloaded a
duplicate of since is Step 8, which runs a full `build` (re-hashes
everything) at the end of every Organize-mode run.

Index format (~/.fs-organizer/<scope-name>/index.json by convention — one
per scope, stored under that scope's own state folder, e.g.
~/.fs-organizer/Downloads/index.json):
{
  "scope": "<scope root>",
  "updated_at": "<ISO 8601 UTC>",
  "by_sha256": {"<hash>": ["<relative path>", ...], ...},
  "folders": {
    "<folder path relative to scope - may contain '/' for a nested
      folder, e.g. 'real-estate-alaknanda' or, if nesting is ever used,
      'real-estate-alaknanda/documents'. The path string itself is how
      parent/child relationships are conveyed - there's no separate tree
      structure, a '/' in the key means "nested under">": {
      "purpose": "<1-2 sentence description of what belongs here, authored
                   by an LLM during Step 5 (Place & structure) - null until
                   set>",
      "member_count": int
    }, ...
  },
  "loose_files": ["<filename>", ...]   (top-level files not in any folder;
                                          just names - already descriptive
                                          post-naming-convention, cheap to
                                          read directly, no per-file
                                          precomputed signal needed)
}

A purpose is written whenever a folder is created or substantively changed
- Step 5 decides it, Step 8 writes it, in either mode (both modes can
create folders, subject to the new-folder confirmation gate).
`write_folder_purpose` is the one function that touches the "purpose"
field; nothing else does, so there's exactly one path by which a purpose
can go stale or wrong, and it's always an explicit LLM judgment, not a
byproduct of file-count churn.

`rehash_file` closes the gap `lookup()`'s repair-on-hit can't: a file
already in the index that gets edited in place is only ever noticed if
some later download's hash happens to collide with its now-wrong stored
value. `rehash_file` is the REACTIVE counterpart - triggered by the
watcher's separate whole-tree content-modify watch, not by a lookup. It's
pure bookkeeping (re-hash one file, refresh its `by_sha256` entry) with no
LLM involvement at all - no `claude -p` dispatch, just a direct script call
- since there's no judgment in "does this file's content still match what
we recorded," only in deciding where a NEW file belongs.

Concurrency: with two independent writers now possible (a headless
watcher dispatch's own `update`/`set-purpose` calls, and the separate
content-watcher's `rehash` calls, potentially running at the same time),
every mutation acquires a simple cross-process file lock
(`index.json.lock`, exclusive-create with retry - same pattern the
watcher's PowerShell side already uses for its queue file) AND reloads
the index fresh from disk under that lock before mutating, so a slightly
stale in-memory copy never clobbers a concurrent writer's changes.

Usage as module:
  from index_manager import build_index, load_index, lookup, update_index, write_folder_purpose, rehash_file
Usage as CLI:
  python index_manager.py build --scope <dir> --output index.json
  python index_manager.py lookup index.json --file <new-file-path>
  python index_manager.py update index.json --file <path> --scope <dir> [--folder <name>]
  python index_manager.py set-purpose index.json --folder <name> --purpose "<text>"
  python index_manager.py rehash index.json --file <path> --scope <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from content_fingerprint_extractor import extract_fingerprint
from fsorg_common import is_excluded, sha256_file

_LOCK_RETRIES = 30
_LOCK_RETRY_DELAY_SEC = 0.1


class _IndexLock:
    """Cross-process file lock via exclusive file creation. Best-effort:
    if the lock can't be acquired after retrying, proceeds anyway rather
    than hanging or failing outright - a lost update here is recoverable
    via the next full `build` rebuild, and that's a better failure mode
    than an index write that never happens at all."""

    def __init__(self, index_path: str | Path):
        self.lock_path = Path(str(index_path) + ".lock")

    def __enter__(self):
        for _ in range(_LOCK_RETRIES):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                time.sleep(_LOCK_RETRY_DELAY_SEC)
        return self

    def __exit__(self, *exc):
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


def _mutate_locked(index_path: str | Path, mutate_fn):
    """Acquire the index lock, reload fresh from disk, apply
    mutate_fn(index) in place, save, release. mutate_fn's return value is
    passed through as-is so callers can report what actually changed."""
    with _IndexLock(index_path):
        index = load_index(index_path)
        result = mutate_fn(index)
        index["updated_at"] = datetime.now(timezone.utc).isoformat()
        Path(index_path).write_text(json.dumps(index, indent=2), encoding="utf-8")
        return index, result


def _rel(path: Path, scope: Path) -> str:
    """Path relative to *scope*, posix form. Raises ValueError if outside.

    Tries the textual form first so a junction is not silently followed:
    resolve() rewrites a path that reaches through one into its real
    location outside the scope, which then fails relative_to and took the
    whole index build down with an uncaught ValueError.
    """
    p, s = Path(path), Path(scope)
    try:
        return p.relative_to(s).as_posix()
    except ValueError:
        return p.resolve().relative_to(s.resolve()).as_posix()


def build_index(scope: str | Path, output_path: str | Path | None = None) -> dict:
    """Full rebuild: walk *scope* bottom-up, hash every in-scope file once
    (sha256 comes free from extract_fingerprint - needed only for
    exact-duplicate detection, nothing else), and record which folder (if
    any) each file lives in, keyed by its full path relative to scope -
    NOT just the top-level segment, so a nested folder gets its own entry
    distinct from its parent (e.g. a file directly in "a/b" registers "a/b"
    as a folder; "a" only gets registered if something is directly inside
    "a" too). Does NOT touch existing `purpose` strings if *output_path*
    already exists - purposes are precious, LLM-authored data that a
    mechanical rebuild must never clobber.
    """
    scope = Path(scope).resolve()
    by_sha: dict[str, list[str]] = {}
    folder_counts: dict[str, int] = {}
    loose_files: list[str] = []

    existing_purposes: dict[str, str] = {}
    if output_path and Path(output_path).exists():
        try:
            old = json.loads(Path(output_path).read_text(encoding="utf-8"))
            existing_purposes = {name: meta.get("purpose")
                                  for name, meta in old.get("folders", {}).items()
                                  if meta.get("purpose")}
        except (json.JSONDecodeError, OSError):
            pass

    # os.walk rather than rglob so excluded subtrees can be pruned *before*
    # being descended into. rglob offers no prune hook, so it walked the
    # whole of .git and node_modules only to discard the results, and a
    # junction pointing at its own ancestor made it recurse without end.
    for dirpath, dirnames, filenames in os.walk(scope):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not is_excluded(here / d)]
        for filename in filenames:
            path = here / filename
            if is_excluded(path):
                continue
            try:
                rel = _rel(path, scope)
                parent_rel = _rel(here, scope)
            except ValueError:
                continue  # reached through a link out of the scope; not ours
            try:
                fp = extract_fingerprint(path)
            except OSError:
                continue  # locked, denied, or vanished mid-walk: skip, don't abort

            if fp.get("sha256"):
                by_sha.setdefault(fp["sha256"], []).append(rel)

            if parent_rel == ".":
                loose_files.append(path.name)
            else:
                folder_counts[parent_rel] = folder_counts.get(parent_rel, 0) + 1

    index = {
        "scope": str(scope),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "by_sha256": by_sha,
        "folders": {
            name: {"purpose": existing_purposes.get(name), "member_count": count}
            for name, count in folder_counts.items()
        },
        "loose_files": sorted(loose_files),
    }
    if output_path:
        Path(output_path).write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def load_index(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _verify_and_repair_hash_bucket(index: dict, scope: Path, sha: str) -> list[str]:
    """Re-hash every path recorded under *sha*, live from disk. A path
    whose live hash no longer matches (edited in place since indexing) is
    moved to its correct bucket; a path that no longer exists is dropped.
    Returns the list of paths still validly under *sha* after repair
    (possibly empty). Mutates *index* in place; caller is responsible for
    persisting it if anything changed."""
    paths = index["by_sha256"].get(sha, [])
    still_valid = []
    for rel in list(paths):
        abs_path = scope / rel
        if not abs_path.is_file():
            paths.remove(rel)
            continue
        live_sha = sha256_file(abs_path)
        if live_sha == sha:
            still_valid.append(rel)
        else:
            paths.remove(rel)
            index["by_sha256"].setdefault(live_sha, [])
            if rel not in index["by_sha256"][live_sha]:
                index["by_sha256"][live_sha].append(rel)
    if not index["by_sha256"].get(sha):
        index["by_sha256"].pop(sha, None)
    return still_valid


def lookup(index: dict, fingerprint: dict, index_path: str | Path | None = None) -> dict:
    """Compare one new file's fingerprint against the index. No filesystem
    access beyond what already produced *fingerprint* AND, on a sha256 hit
    only, one live re-hash per candidate to guard against a stale stored
    hash (see module docstring) - never a full rescan.

    Returns:
      exact_duplicate: relative path of an existing file with the same
        sha256, verified live, or None.
      index_repaired: true if a stale hash was found and corrected during
        this lookup (informational - worth logging, not acting on).
      folder_purposes: {name: purpose} for every folder that HAS a purpose
        set - deliberately the full list, not a pre-scored subset. There
        are only a few dozen folders and each purpose is a sentence or two,
        so it's cheap enough for the calling LLM (Step 5) to just read
        directly and judge fit itself, rather than have a script guess at
        relevance with a lexical heuristic.
      folders_missing_purpose: folder names with no purpose set yet -
        surfaced so a caller doing a full organize-mode pass knows which
        folders still need one written.
      loose_files: the current top-level loose file list, unfiltered -
        same reasoning: small enough to hand to the LLM directly for the
        "would this + an existing loose file justify a new folder"
        judgment (Step 5), not something to pre-score mechanically.
    """
    sha = fingerprint.get("sha256")
    exact = None
    repaired = False

    if sha and sha in index.get("by_sha256", {}):
        if index_path:
            def _mutate(fresh_index):
                if sha not in fresh_index.get("by_sha256", {}):
                    return [], False  # resolved by a concurrent writer already
                scope = Path(fresh_index["scope"])
                before = list(fresh_index["by_sha256"].get(sha, []))
                valid = _verify_and_repair_hash_bucket(fresh_index, scope, sha)
                return valid, (valid != before)
            index, (valid, repaired) = _mutate_locked(index_path, _mutate)
            if valid:
                exact = valid[0]
        else:
            scope = Path(index["scope"])
            valid = _verify_and_repair_hash_bucket(index, scope, sha)
            if valid:
                exact = valid[0]

    folders = index.get("folders", {})
    return {
        "exact_duplicate": exact,
        "index_repaired": repaired,
        "folder_purposes": {name: meta["purpose"] for name, meta in folders.items()
                             if meta.get("purpose")},
        "folders_missing_purpose": sorted(name for name, meta in folders.items()
                                           if not meta.get("purpose")),
        "loose_files": list(index.get("loose_files", [])),
    }


def update_index(index_path: str | Path, rel_path: str, fingerprint: dict,
                  folder: str | None, previous_rel: str | None = None) -> dict:
    """Add or overwrite one file's sha256/placement entry (post-placement).
    Never touches `purpose` - that's write_folder_purpose's job only.
    Call this at Step 8 so the index reflects the new file, including
    when it stays loose (folder=None).

    *previous_rel* is where the file was BEFORE this run moved or renamed
    it. Without it the old entry cannot be found: this function is called
    with the post-move path, `loose_files` holds bare filenames, and both
    the folder and the name usually changed, so a file organized out of the
    scope root left its old name in `loose_files` permanently. Organize
    mode's closing full rebuild papered over that; watcher mode has no
    rebuild, so the ghosts simply accumulated.
    """
    def _mutate(index):
        stale = {rel_path, previous_rel} - {None}
        for sha, paths in list(index["by_sha256"].items()):
            for gone in stale & set(paths):
                paths.remove(gone)
            if not paths:
                del index["by_sha256"][sha]
        for gone in stale:
            name = Path(gone).name
            if name in index["loose_files"]:
                index["loose_files"].remove(name)

        # Self-heal: loose_files lists only the scope root, so confirming
        # each entry still exists is a handful of stat calls, and it clears
        # anything removed or moved by something other than this skill.
        root = Path(index.get("scope", ""))
        if root.is_dir():
            index["loose_files"] = [n for n in index["loose_files"]
                                    if (root / n).exists()]

        sha = fingerprint.get("sha256")
        if sha:
            index["by_sha256"].setdefault(sha, [])
            if rel_path not in index["by_sha256"][sha]:
                index["by_sha256"][sha].append(rel_path)

        if folder:
            # member_count is informational only (not consumed by lookup()),
            # so an exact recount isn't worth tracking full per-file folder
            # membership for - this function represents one file's placement
            # event, so a plain increment is correct for the common case and
            # only drifts on a same-folder re-placement, which a `build`
            # rebuild (bottom-up disk scan) would correct.
            meta = index["folders"].setdefault(folder, {"purpose": None, "member_count": 0})
            meta["member_count"] = meta.get("member_count", 0) + 1
        else:
            name = Path(rel_path).name
            if name not in index["loose_files"]:
                index["loose_files"].append(name)
            index["loose_files"].sort()

    index, _ = _mutate_locked(index_path, _mutate)
    return index


def write_folder_purpose(index_path: str | Path, folder: str, purpose: str) -> dict:
    """The one function that sets a folder's purpose string. Called at
    Step 8, in either mode, for a folder that was just created or whose
    accurate-for-all-contents description changed."""
    def _mutate(index):
        meta = index["folders"].setdefault(folder, {"purpose": None, "member_count": 0})
        meta["purpose"] = purpose

    index, _ = _mutate_locked(index_path, _mutate)
    return index


def rehash_file(index_path: str | Path, rel_path: str, scope: str | Path) -> dict:
    """Re-hash ONE already-indexed file live from disk and refresh its
    `by_sha256` entry. The reactive counterpart to lookup()'s repair-on-hit
    - triggered by the watcher's separate whole-tree content-modify watch
    (an existing file changed in place), not by a lookup collision. Never
    touches folders/purpose/loose_files: the file's LOCATION hasn't
    changed, only its content, so nothing about where it lives needs
    revisiting. If the file no longer exists, just drops its old entry -
    a deletion, not this function's concern to flag (the skill never
    deletes, so a vanished file was removed by something outside the
    skill entirely)."""
    scope_p = Path(scope)
    abs_path = scope_p / rel_path

    def _mutate(index):
        for sha, paths in list(index["by_sha256"].items()):
            if rel_path in paths:
                paths.remove(rel_path)
                if not paths:
                    del index["by_sha256"][sha]
        if abs_path.is_file():
            # Excluded files - and anything under an excluded ancestor,
            # like a saved page's `X_files` asset folder - are never
            # indexed by build, so a content-change event on one must not
            # sneak it in here either.
            scope_root = scope_p.resolve()
            if is_excluded(abs_path) or any(
                is_excluded(a) for a in abs_path.resolve().parents
                if a != scope_root and a.is_relative_to(scope_root)
            ):
                return
            new_sha = sha256_file(abs_path)
            index["by_sha256"].setdefault(new_sha, [])
            if rel_path not in index["by_sha256"][new_sha]:
                index["by_sha256"][new_sha].append(rel_path)

    index, _ = _mutate_locked(index_path, _mutate)
    return index


def merge_scope(parent_index_path: str | Path, parent_root: str | Path,
                child_index_path: str | Path, child_root: str | Path) -> dict:
    """Absorb a nested scope's folder purposes into its parent's index.

    Called when a folder is put under watch and a folder inside it was
    already watched separately. The two must not stay separate - nested
    watchers dispatch twice for the same file - but the child's purposes are
    LLM-authored judgments that a plain rebuild of the parent would silently
    discard, and re-deriving them means reading all that content again.

    So each purpose is re-keyed to its path relative to the PARENT root and
    carried across. Hashes and loose files are deliberately NOT merged: the
    parent's own `build` re-derives those from disk, correctly and cheaply,
    whereas merging them would mean rewriting every relative path and hoping
    nothing moved in between. A purpose the parent already holds wins, since
    the parent is the scope that survives.
    """
    parent_root = Path(parent_root).resolve()
    child_root = Path(child_root).resolve()
    prefix = child_root.relative_to(parent_root).as_posix()

    child = load_index(child_index_path)
    carried: list[str] = []
    kept_existing: list[str] = []

    def _mutate(index):
        folders = index.setdefault("folders", {})
        for name, meta in child.get("folders", {}).items():
            purpose = meta.get("purpose")
            if not purpose:
                continue
            merged = prefix if name == "." else f"{prefix}/{name}"
            if folders.get(merged, {}).get("purpose"):
                kept_existing.append(merged)
                continue
            entry = folders.setdefault(merged, {"purpose": None, "member_count": 0})
            entry["purpose"] = purpose
            entry["member_count"] = entry.get("member_count") or meta.get("member_count", 0)
            carried.append(merged)

    _mutate_locked(parent_index_path, _mutate)
    return {"prefix": prefix, "carried": sorted(carried),
            "kept_existing": sorted(kept_existing)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ms = sub.add_parser("merge-scope",
                        help="absorb a nested scope's purposes into its parent")
    ms.add_argument("--parent-index", required=True)
    ms.add_argument("--parent-root", required=True)
    ms.add_argument("--child-index", required=True)
    ms.add_argument("--child-root", required=True)

    b = sub.add_parser("build")
    b.add_argument("--scope", required=True)
    b.add_argument("--output", required=True)

    lu = sub.add_parser("lookup")
    lu.add_argument("index_path")
    lu.add_argument("--file", required=True, help="path to the new file (will be fingerprinted)")

    up = sub.add_parser("update")
    up.add_argument("index_path")
    up.add_argument("--file", required=True, help="new file's CURRENT (post-move) path")
    up.add_argument("--scope", required=True, help="scope root, to compute the relative path")
    up.add_argument("--folder", default=None, help="folder name relative to scope, omit if left loose")
    up.add_argument("--previous", default=None,
                    help="the file's path relative to scope BEFORE this run "
                         "moved/renamed it, so its old entry can be cleared")

    sp = sub.add_parser("set-purpose")
    sp.add_argument("index_path")
    sp.add_argument("--folder", required=True)
    sp.add_argument("--purpose", required=True)

    rh = sub.add_parser("rehash")
    rh.add_argument("index_path")
    rh.add_argument("--file", required=True, help="already-indexed file that changed in place")
    rh.add_argument("--scope", required=True, help="scope root, to compute the relative path")

    args = ap.parse_args(argv)

    if args.cmd == "merge-scope":
        result = merge_scope(args.parent_index, args.parent_root,
                             args.child_index, args.child_root)
        print(f"Merged {args.child_root} into {args.parent_root} as "
              f"'{result['prefix']}': carried {len(result['carried'])} purpose(s), "
              f"kept {len(result['kept_existing'])} the parent already had")
    elif args.cmd == "build":
        index = build_index(args.scope, args.output)
        missing = sum(1 for m in index["folders"].values() if not m.get("purpose"))
        print(f"Indexed {sum(len(p) for p in index['by_sha256'].values())} hashed files, "
              f"{len(index['folders'])} folders ({missing} missing a purpose), "
              f"{len(index['loose_files'])} loose files -> {args.output}")
    elif args.cmd == "lookup":
        index = load_index(args.index_path)
        fp = extract_fingerprint(args.file)
        result = lookup(index, fp, index_path=args.index_path)
        print(json.dumps(result, indent=2))
    elif args.cmd == "update":
        fp = extract_fingerprint(args.file)
        scope = Path(args.scope).resolve()
        rel = _rel(Path(args.file), scope)
        update_index(args.index_path, rel, fp, args.folder, args.previous)
        print(f"Updated index: {rel} -> folder={args.folder!r}")
    elif args.cmd == "set-purpose":
        write_folder_purpose(args.index_path, args.folder, args.purpose)
        print(f"Set purpose for {args.folder!r}")
    elif args.cmd == "rehash":
        scope = Path(args.scope).resolve()
        rel = _rel(Path(args.file), scope)
        rehash_file(args.index_path, rel, scope)
        print(f"Rehashed: {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
