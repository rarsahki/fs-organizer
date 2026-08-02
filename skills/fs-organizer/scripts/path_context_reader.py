"""path_context_reader — Step 1 (Read context) of the fs-organizer workflow.

Given a path, returns the context the rest of the pipeline needs:
ancestor folder names (up to an optional root), sibling metadata, and the
target's own metadata. Pure filesystem read; mutates nothing.

Two entry points:
- `read_context(path, root)`: single item — one file/folder's own
  metadata plus its siblings. Fine for a single-file input or a small
  flat folder.
- `read_context_tree(directory, root)`: bulk form for a directory scope
  — walks the subtree bottom-up in one call and returns one batch
  per non-empty folder (its direct files + ancestor names), instead of
  requiring one `read_context` call per folder.

Usage as module:   from path_context_reader import read_context, read_context_tree
Usage as CLI (single item):  python path_context_reader.py <path> [--root ROOT]
Usage as CLI (bulk):         python path_context_reader.py <directory> --recursive [--root ROOT] --output context.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from fsorg_common import is_excluded, split_name


def _meta(p: Path) -> dict:
    st = p.stat()
    return {
        "name": p.name,
        "ext": split_name(p)[1] if p.is_file() else "",
        "is_dir": p.is_dir(),
        "size": st.st_size if p.is_file() else None,
        "ctime": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


def _ancestors_of(p: Path, root_p: Path | None) -> list[str]:
    """Folder names from *root_p* (exclusive) down to p's parent, outermost
    first. If root_p is None or not an ancestor, all parents up to the
    filesystem anchor are returned. Shared by read_context (ancestors of a
    file) and read_context_tree (ancestors of a folder itself)."""
    ancestors: list[str] = []
    for parent in reversed(p.parents):
        if root_p is not None:
            try:
                parent.relative_to(root_p)
            except ValueError:
                continue  # outside root
            if parent == root_p:
                continue  # root itself is excluded
        else:
            if parent == parent.anchor or parent == Path(parent.anchor):
                continue  # skip drive anchor like C:\
        ancestors.append(parent.name)
    return ancestors


def read_context(path: str | Path, root: str | Path | None = None) -> dict:
    """Return {target_meta, ancestors, siblings, excluded_siblings} for *path*.

    ancestors: folder names from *root* (exclusive) down to the parent,
    outermost first. If root is None or not an ancestor, all parents up to
    the filesystem anchor are returned.
    siblings: metadata for entries sharing the target's parent (target
    excluded), with organizer-excluded entries listed separately so callers
    can see them without ever proposing changes to them.
    """
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"path does not exist: {p}")

    root_p = Path(root).resolve() if root else None
    ancestors = _ancestors_of(p, root_p)

    siblings, excluded = [], []
    for entry in sorted(p.parent.iterdir(), key=lambda e: e.name.lower()):
        if entry == p:
            continue
        try:
            meta = _meta(entry)
        except OSError:
            continue  # a sibling we cannot stat is a sibling we cannot organize
        (excluded if is_excluded(entry) else siblings).append(meta)

    return {
        "target_meta": _meta(p),
        "path": str(p),
        "parent": str(p.parent),
        "ancestors": ancestors,
        "siblings": siblings,
        "excluded_siblings": excluded,
    }


def read_context_tree(directory: str | Path, root: str | Path | None = None) -> dict:
    """Bulk form: walk *directory* bottom-up in one call, returning one
    batch per non-empty folder — its direct files' metadata plus its own
    ancestor chain — instead of requiring one read_context() call per
    folder. Folders under an excluded ancestor (.git, node_modules, ...)
    are skipped entirely; excluded files within an included folder are
    still reported, under `excluded_files`, same as read_context does.

    Returns {scope, batches: [{folder, ancestors, files, excluded_files}]},
    deepest folders first (bottom-up), matching the order the rest of the
    pipeline expects to process them in.
    """
    root_p = Path(directory).resolve()
    if not root_p.is_dir():
        raise NotADirectoryError(f"not a directory: {root_p}")
    scope_p = Path(root).resolve() if root else root_p

    # os.walk with in-place pruning, not rglob: an excluded subtree must
    # never be descended into rather than walked and then discarded, and a
    # junction aimed at its own ancestor makes an unpruned walk recurse
    # forever. Only names *below* the scope are ever judged — the scope's
    # own ancestors are not, or pointing the skill at a folder that happens
    # to sit under a hidden one (anything inside AppData, say) would
    # exclude every single file in it.
    kept_folders = []
    for dirpath, dirnames, _ in os.walk(root_p):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not is_excluded(here / d)]
        kept_folders.append(here)

    batches = []
    for folder in sorted(kept_folders, key=lambda p: len(p.parts), reverse=True):
        try:
            children = list(folder.iterdir())
        except OSError:
            continue  # denied, or removed while we walked
        files, excluded_files = [], []
        for c in children:
            try:
                if not c.is_file():
                    continue
                meta = _meta(c)
            except OSError:
                continue
            (excluded_files if is_excluded(c) else files).append(meta)
        if not files:
            continue  # no in-scope files here; nothing for later steps to batch
        # ancestors "down to the parent" of a file inside *folder* means the
        # chain must include folder's own name (folder IS that parent) —
        # matches what read_context() returns for a file living in it.
        ancestors = _ancestors_of(folder, scope_p)
        if folder != scope_p:
            ancestors = ancestors + [folder.name]
        batches.append({
            "folder": str(folder),
            "ancestors": ancestors,
            "files": files,
            "excluded_files": excluded_files,
        })

    return {"scope": str(root_p), "batches": batches}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path")
    ap.add_argument("--root", default=None, help="treat this dir as the top; ancestors above it are omitted")
    ap.add_argument("--recursive", action="store_true",
                    help="bulk form: walk <path> as a directory subtree in one call")
    ap.add_argument("--output", default=None,
                    help="write result here instead of stdout (typical with --recursive)")
    args = ap.parse_args(argv)
    try:
        if args.recursive:
            result = read_context_tree(args.path, args.root)
        else:
            result = read_context(args.path, args.root)
    except (FileNotFoundError, NotADirectoryError) as e:
        print(str(e), file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        if args.recursive:
            total_files = sum(len(b["files"]) for b in result["batches"])
            print(f"Wrote {len(result['batches'])} folder batches, "
                  f"{total_files} files total, to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
