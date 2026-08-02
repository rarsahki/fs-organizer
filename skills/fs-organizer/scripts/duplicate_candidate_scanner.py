"""duplicate_candidate_scanner — Step 3 (Exact duplicates) of the fs-organizer workflow.

Byte identity is the only duplicate test in this workflow: files are
grouped by SHA-256, and each group keeps its newest member and deletes the
rest. Nothing else here is a duplicate judgment.

There is deliberately NO fuzzy similarity scoring — no near-duplicate
shortlisting, no content-overlap ratio, no "ambiguous pair" bucket. An
earlier version of this script scored every plausible pair with
difflib.SequenceMatcher to guess at same-conceptual-file relationships.
That was removed on purpose, for two reasons:

  1. It was O(n^2) work whose output nothing consumed. Deciding that two
     non-identical files belong together is a semantic judgment, and the
     workflow now makes it directly at Step 5 by matching each file's
     content understanding against the folders' stated purposes — which
     catches relationships a lexical ratio never could, and misses fewer.
  2. A similarity score near a threshold is exactly the kind of signal
     that reads as authoritative while being arbitrary. Byte identity has
     no such ambiguity: two files either are the same bytes or they
     aren't.

So a file that is not a byte-identical copy of another is simply a
distinct file, and carries on through the remaining steps as one. Zero LLM
calls happen here.

Two entry points:
- `scan()` / `scan_folder()`: single-folder form; hashes the folder's own
  files directly.
- `scan_all()`: bulk form, operating on Step 2's fingerprints.json.
  Grouping runs GLOBALLY across the whole scope using each fingerprint's
  precomputed `sha256` — no re-hashing, and no folder boundary, so
  identical files living in different folders are still caught.

Usage as module:   from duplicate_candidate_scanner import scan, scan_all
Usage as CLI (single folder):  python duplicate_candidate_scanner.py <folder>
Usage as CLI (bulk):           python duplicate_candidate_scanner.py --from fingerprints.json --output duplicates.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fsorg_common import is_excluded, sha256_file


def _mtime(path: Path) -> float:
    return path.stat().st_mtime


def _group(by_hash: dict[str, list], path_of, mtime_of) -> tuple[list, list]:
    """Split hash buckets into exact-duplicate groups and lone survivors.

    Shared by both entry points so "keep the newest, delete the rest" has
    exactly one implementation. *path_of* / *mtime_of* adapt the two item
    shapes (Path objects vs. fingerprint dicts).
    """
    exact_groups, survivors = [], []
    for group in by_hash.values():
        if len(group) == 1:
            survivors.append(group[0])
            continue
        try:
            ordered = sorted(group, key=mtime_of, reverse=True)
        except FileNotFoundError:
            # A file vanished since it was hashed; keep arbitrary order
            # rather than failing the whole batch over it.
            ordered = group
        exact_groups.append({
            "files": [path_of(i) for i in ordered],
            "keep": path_of(ordered[0]),
            "delete": [path_of(i) for i in ordered[1:]],
        })
        survivors.append(ordered[0])
    return exact_groups, survivors


def scan(files: list[str | Path]) -> dict:
    """Group *files* by content hash (single-folder form).

    Returns:
      exact_groups: [{files: [paths newest-first], keep: path, delete: [paths]}]
      survivors:    [every path that carries forward — files with no
                     duplicate, plus the kept member of each group]
    """
    paths = [Path(f) for f in files]
    # 0-byte files are excluded from dedup entirely, not just grouped
    # separately: sha256("") is the same constant for every empty file, so
    # hashing them would falsely cluster unrelated empty files as "exact
    # duplicates" (mirrors extract_fingerprint's modality: "empty" guard).
    paths = [p for p in paths
             if p.is_file() and not is_excluded(p) and p.stat().st_size > 0]

    by_hash: dict[str, list[Path]] = {}
    for p in paths:
        by_hash.setdefault(sha256_file(p), []).append(p)

    exact_groups, survivors = _group(by_hash, path_of=str, mtime_of=_mtime)
    return {
        "exact_groups": exact_groups,
        "survivors": [str(p) for p in survivors],
    }


def scan_folder(folder: str | Path) -> dict:
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"not a directory: {folder}")
    return scan([p for p in folder.iterdir() if p.is_file()])


def scan_all(fingerprints: list[dict]) -> dict:
    """Bulk form: group an entire scope by content hash, using the sha256
    Step 2 already computed — no file is read again here.

    Grouping is global across the scope (see module docstring). Files with
    `modality: "empty"` carry no sha256 and are skipped entirely.
    """
    by_hash: dict[str, list[dict]] = {}
    for fp in fingerprints:
        h = fp.get("sha256")
        if h:
            by_hash.setdefault(h, []).append(fp)

    exact_groups, survivors = _group(
        by_hash,
        path_of=lambda fp: fp["path"],
        mtime_of=lambda fp: Path(fp["path"]).stat().st_mtime,
    )
    return {
        "exact_groups": exact_groups,
        "survivors": [fp["path"] for fp in survivors],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("folder", nargs="?", help="single-folder form")
    ap.add_argument("--from", dest="from_", default=None,
                    help="Step 2 fingerprints.json to scan across the whole scope (bulk form)")
    ap.add_argument("--output", default=None,
                    help="write result here instead of stdout (required with --from)")
    args = ap.parse_args(argv)

    if args.from_:
        data = json.loads(Path(args.from_).read_text(encoding="utf-8-sig"))
        result = scan_all(data["fingerprints"])
    elif args.folder:
        result = scan_folder(args.folder)
    else:
        ap.error("pass a folder (single-folder form) or --from fingerprints.json (bulk form)")
        return 2

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
        to_delete = sum(len(g["delete"]) for g in result["exact_groups"])
        print(f"exact_groups={len(result['exact_groups'])} "
              f"({to_delete} file(s) to delete) "
              f"survivors={len(result['survivors'])} -> {args.output}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
