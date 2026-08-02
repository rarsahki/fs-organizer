"""saved_page — keeps a saved web page's html and asset folder paired.

A browser's "save page" writes two things side by side: `X.html` and
`X_files/`, with the html referencing the folder by its literal name in
hundreds of hrefs. They are one logical item, but plain file moves don't
know that — even Windows Explorer breaks a saved page if you rename the
html, because nothing rewrites the references. This skill renames pages
to its naming convention as a matter of course, so it has to do the job
properly:

  1. move/rename the html AND the folder together (batch_executor ops —
     this script moves nothing);
  2. rename the folder to `<new-html-stem>_files`, so the name-based
     pairing (see fsorg_common.saved_page_html_for) survives the rename
     and future scans keep treating the folder as an attachment;
  3. rewrite the references inside the html to the folder's new name —
     this script's job, the one step that is neither a move nor a rename.

Both the raw folder name and its URL-encoded form are rewritten; browsers
emit either depending on the characters in the title.

Usage as module:   from saved_page import companion_for, fix_refs
Usage as CLI:
  python saved_page.py companion <html-path>
      -> prints the asset folder's path, or nothing (exit 1) if none
  python saved_page.py fix-refs <html-path> --old "<old folder name>" --new "<new folder name>"
      -> rewrites references in the html, prints how many changed
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path

from fsorg_common import saved_page_html_for


def companion_for(html_path: str | Path) -> Path | None:
    """The `<stem>_files` folder belonging to *html_path*, if it exists."""
    p = Path(html_path)
    for suffix in ("_files",):
        candidate = p.parent / (p.stem + suffix)
        if candidate.is_dir() and saved_page_html_for(candidate) == p:
            return candidate
    return None


def fix_refs(html_path: str | Path, old_name: str, new_name: str) -> int:
    """Rewrite folder references inside the html. Returns replacements made.

    Handles both the literal folder name and its URL-encoded form
    (spaces as %20 etc.). Reads and writes UTF-8 with errors preserved as
    replacement chars only in memory comparisons — the file is rewritten
    only if something actually changed.
    """
    p = Path(html_path)
    text = p.read_text(encoding="utf-8", errors="surrogateescape")

    count = 0
    for old in {old_name, urllib.parse.quote(old_name)}:
        new = new_name if old == old_name else urllib.parse.quote(new_name)
        count += text.count(old)
        text = text.replace(old, new)

    if count:
        p.write_text(text, encoding="utf-8", errors="surrogateescape")
    return count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("companion")
    c.add_argument("html")

    f = sub.add_parser("fix-refs")
    f.add_argument("html")
    f.add_argument("--old", required=True, help="asset folder's old name")
    f.add_argument("--new", required=True, help="asset folder's new name")

    args = ap.parse_args(argv)
    if args.cmd == "companion":
        found = companion_for(args.html)
        if found is None:
            return 1
        print(found)
        return 0

    n = fix_refs(args.html, args.old, args.new)
    print(f"rewrote {n} reference(s) in {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
