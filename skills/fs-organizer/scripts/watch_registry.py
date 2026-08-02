"""watch_registry - the list of folders under watch, and how they overlap.

Two watchers on nested folders double-dispatch. A file landing in Downloads
wakes the Downloads watcher, which moves it into Downloads\\Receipts, which
wakes the Receipts watcher, which runs a second headless session over the
same file. Each watcher consults only its OWN scope's index, so neither can
tell the arrival was the other one's work - the guard that would catch a
self-caused event fails open across scopes by construction. The two sessions
can also race, renaming and re-placing the same file and writing indexes
that disagree.

The fix is that nested watches are not allowed to coexist: the outermost
folder wins and covers the whole tree, and any watch nested inside it is
absorbed - its folder purposes migrated into the parent's index, its own
state retired. This module owns the registry that makes that detectable, and
the overlap arithmetic; `index_manager merge-scope` does the migration, and
the setup script drives both.

Registry (~/.fs-organizer/watched.json):
{
  "watched": [
    {"root": "C:\\\\Users\\\\me\\\\Downloads",
     "scope_id": "Downloads-1a2b3c4d",
     "registered_at": "<ISO 8601 UTC>"}
  ]
}

Usage as module: from watch_registry import load, register, unregister, find_overlap
Usage as CLI:
  python watch_registry.py list
  python watch_registry.py check --root <dir>      # what would happen, as JSON
  python watch_registry.py register --root <dir>
  python watch_registry.py unregister --root <dir>
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from fsorg_common import STATE_ROOT, scope_id

REGISTRY_PATH = STATE_ROOT / "watched.json"


def _norm(path: str | Path) -> Path:
    return Path(str(path).rstrip("\\/")).resolve()


def load(registry_path: str | Path | None = None) -> list[dict]:
    """Every currently-registered watch. Missing or corrupt file reads empty."""
    p = Path(registry_path) if registry_path else REGISTRY_PATH
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return []
    return [e for e in data.get("watched", []) if e.get("root")]


def save(entries: list[dict], registry_path: str | Path | None = None) -> Path:
    p = Path(registry_path) if registry_path else REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"watched": entries}, indent=2), encoding="utf-8")
    return p


def _is_within(child: Path, parent: Path) -> bool:
    """True if *child* is *parent* or lives underneath it."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def find_overlap(root: str | Path, registry_path: str | Path | None = None) -> dict:
    """How *root* relates to what is already watched.

    Returns:
      covered_by: an existing watch that already contains *root* (so watching
        *root* would be redundant and would double-dispatch), or None.
      absorbs: existing watches nested inside *root*, which must be merged
        into it before *root* can be watched.
      already: the existing entry for exactly this root, if any.
    """
    target = _norm(root)
    covered_by = None
    absorbs = []
    already = None

    for entry in load(registry_path):
        existing = _norm(entry["root"])
        if existing == target:
            already = entry
        elif _is_within(target, existing):
            covered_by = entry          # an ancestor is watched
        elif _is_within(existing, target):
            absorbs.append(entry)       # a descendant is watched

    return {"covered_by": covered_by, "absorbs": absorbs, "already": already}


def register(root: str | Path, registry_path: str | Path | None = None) -> dict:
    """Record *root* as watched, replacing any existing entry for it."""
    target = _norm(root)
    entries = [e for e in load(registry_path) if _norm(e["root"]) != target]
    entry = {
        "root": str(target),
        "scope_id": scope_id(target),
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    entries.append(entry)
    entries.sort(key=lambda e: e["root"].lower())
    save(entries, registry_path)
    return entry


def unregister(root: str | Path, registry_path: str | Path | None = None) -> bool:
    """Drop *root* from the registry. True if it was there."""
    target = _norm(root)
    entries = load(registry_path)
    kept = [e for e in entries if _norm(e["root"]) != target]
    if len(kept) == len(entries):
        return False
    save(kept, registry_path)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("check", "register", "unregister"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "list":
        print(json.dumps(load(), indent=2))
    elif args.cmd == "check":
        print(json.dumps(find_overlap(args.root), indent=2))
    elif args.cmd == "register":
        print(json.dumps(register(args.root), indent=2))
    else:
        print(json.dumps({"removed": unregister(args.root)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
