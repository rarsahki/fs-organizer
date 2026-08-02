"""classify_input — narrows fingerprints down to what Step 4 must read.

Step 4 (Classify) is the first step whose input enters the model's
context, and context is re-sent on every later turn — so whatever lands
here is paid for again at Step 5, 6, 7, 8 and 9. This script exists to
make that payload as small as it can honestly be:

  - files already resolved are dropped entirely: 0-byte files (nothing to
    classify) and the copies Step 3 marked for deletion (they are about
    to stop existing). Loading a record for a file that is being deleted
    is pure waste, and in a duplicate-heavy folder that is most of them.
  - surviving records keep only the fields classification actually uses -
    path, ext, size, title, first_paragraph, exif_date, note - dropping
    sha256, modality and word_count, which Step 3 has already finished
    with.

The field trimming is the smaller half (~17% per record); skipping
resolved files is where the real saving is.

Both modes use this, so Step 4 reads the same shape either way: Organize
passes Step 3's duplicates.json, Watcher passes the duplicate paths its
index lookup found.

Usage as module:   from classify_input import build
Usage as CLI:
  python classify_input.py --fingerprints <fp.json> --output <to-classify.json>
      [--duplicates <duplicates.json>] [--exclude <path> ...]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# What Step 4 reads. Everything else in a fingerprint has done its job by
# the time this runs. The last three fields only appear on directory
# fingerprints (a folder arriving as a unit in watcher mode).
CLASSIFY_FIELDS = ("path", "ext", "size", "title", "first_paragraph",
                   "exif_date", "note", "modality", "file_count", "entries")


def build(fingerprints: list[dict], drop: set[str] | None = None) -> dict:
    """Return {items, dropped} - items being the trimmed records to classify."""
    drop = {str(p) for p in (drop or set())}
    items, skipped_empty, skipped_dup = [], 0, 0

    for fp in fingerprints:
        if fp.get("modality") == "empty":
            skipped_empty += 1
            continue
        if fp.get("path") in drop:
            skipped_dup += 1
            continue
        items.append({k: fp[k] for k in CLASSIFY_FIELDS if fp.get(k) is not None})

    return {
        "items": items,
        "dropped": {"empty": skipped_empty, "duplicate": skipped_dup},
    }


def _duplicate_paths(duplicates_path: str | Path) -> set[str]:
    """Every path Step 3 marked for deletion in an exact-duplicate group."""
    data = json.loads(Path(duplicates_path).read_text(encoding="utf-8-sig"))
    return {p for g in data.get("exact_groups", []) for p in g.get("delete", [])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fingerprints", required=True,
                    help="Step 2's fingerprints.json")
    ap.add_argument("--output", required=True,
                    help="where to write the trimmed to-classify list")
    ap.add_argument("--duplicates", default=None,
                    help="Organize mode: Step 3's duplicates.json; its "
                         "exact_groups[].delete paths are dropped")
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="Watcher mode: explicit paths to drop (duplicates "
                         "the index lookup resolved)")
    args = ap.parse_args(argv)

    raw = json.loads(Path(args.fingerprints).read_text(encoding="utf-8-sig"))
    fingerprints = raw["fingerprints"] if isinstance(raw, dict) else (
        raw if isinstance(raw, list) else [raw])

    drop = set(args.exclude)
    if args.duplicates:
        drop |= _duplicate_paths(args.duplicates)

    result = build(fingerprints, drop)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    d = result["dropped"]
    print(f"to classify: {len(result['items'])} file(s) "
          f"(dropped {d['empty']} empty, {d['duplicate']} duplicate) -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
