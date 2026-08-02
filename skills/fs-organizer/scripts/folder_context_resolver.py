"""folder_context_resolver — builds the redundancy set for a destination.

The redundancy set is every word token already conveyed by a file's
containing folder path (project names, role words like tests/docs/src).
Any keyword in this set must be omitted from the file's own name — the
"don't repeat what the path already says" rule.

Always computed against the DESTINATION ancestors (where the file will
live after the plan), not its messy origin.

Usage as module:   from folder_context_resolver import resolve_redundancy_set
Usage as CLI:      python folder_context_resolver.py fs-organizer tests
"""
from __future__ import annotations

import argparse
import json

from fsorg_common import singularize, tokenize


def resolve_redundancy_set(ancestor_names: list[str]) -> set[str]:
    """Tokens (plus naive singular forms) conveyed by the ancestor folder names."""
    redundant: set[str] = set()
    for name in ancestor_names:
        for token in tokenize(name):
            redundant.add(token)
            redundant.add(singularize(token))
    return redundant


def strip_redundant(keywords: list[str], redundancy_set: set[str]) -> list[str]:
    """Drop keywords already conveyed by the path; preserve order.

    Purely the redundancy rule — it may legitimately return fewer than the
    convention's 2-keyword minimum, or nothing at all. Enforcing the
    keyword count is `name_assembler.assemble`'s job, because that is the
    one implementation of the grammar; putting a floor here too would
    split the same rule across two files.
    """
    return [
        kw for kw in keywords
        if kw.lower() not in redundancy_set
        and singularize(kw.lower()) not in redundancy_set
    ]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ancestors", nargs="+", help="ancestor folder names, outermost first")
    ap.add_argument("--keywords", nargs="*", default=None, help="if given, also show them stripped")
    args = ap.parse_args(argv)
    rset = resolve_redundancy_set(args.ancestors)
    out: dict = {"redundancy_set": sorted(rset)}
    if args.keywords is not None:
        out["stripped_keywords"] = strip_redundant(args.keywords, rset)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
