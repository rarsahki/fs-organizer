"""name_assembler — Step 6 (Name) of the fs-organizer workflow.

Assembles final names to the fixed grammar and parses them back:

    keyword-keyword[-more-keywords][-YYYY-MM-DD][-vNN|-draft|-final].ext

Field rules (must stay in sync with references/naming-convention.md):
- keywords: kebab-case word segments; digits allowed inside a keyword
  ("1099", "2023"); a bare year is a keyword, NOT a date.
- date: only an exact YYYY-MM-DD segment run parses as the date field.
- version/status: only vNN (zero-padded) or draft/final, and only as the
  final segment before the extension.

parse() is the exact inverse of assemble() — the CLI's --date/--version/
--keyword filters depend on this round-tripping.

Usage as module:   from name_assembler import assemble, parse, disambiguate
Usage as CLI:      python name_assembler.py assemble tax return --date 2024-04-15 --ext .pdf
                   python name_assembler.py parse tax-return-2024-04-15-v02.pdf
"""
from __future__ import annotations

import argparse
import json
import re

from fsorg_common import (COMPOUND_EXTENSIONS, WINDOWS_RESERVED, is_token_char,
                          tokenize)

MAX_STEM_LEN = 60

# The convention's keyword count, enforced here because this module IS the
# grammar - both modes call it, so enforcing once covers both. parse() is
# deliberately NOT held to this: it reads names that already exist on
# disk, which may predate the convention, and rejecting them would make
# the skill unable to inspect what it is meant to fix.
KEYWORD_MIN = 2
KEYWORD_MAX = 4


def enforce_keyword_count(kept: list[str], original: list[str]) -> list[str]:
    """Hold *kept* to KEYWORD_MIN..KEYWORD_MAX, drawing on *original*.

    Two rules pull against each other and this is where that is settled.
    The redundancy rule says drop anything the folder path already
    conveys; the grammar says a name carries 2-4 keywords. Keywords
    [sec, 8k, investor, guide] landing in `sec-investor-guides/` strip
    down to just `8k` - obeying redundancy while producing a name nobody
    can search for. The count wins: restore stripped keywords in their
    original order until the floor is met. Above the ceiling, keep the
    first four, since keywords are given most-significant-first.
    """
    if len(kept) > KEYWORD_MAX:
        return kept[:KEYWORD_MAX]
    if len(kept) >= KEYWORD_MIN or not original:
        return kept
    for kw in original:  # original order, so the name still reads naturally
        if len(kept) >= KEYWORD_MIN:
            break
        if kw not in kept:
            kept.append(kw)
    return sorted(kept, key=original.index)

# Date and version digits stay explicitly ASCII: \d in Unicode mode also
# matches Arabic-Indic and Devanagari digits, which are not valid here.
_DATE_SEG = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_VERSION_SEG = re.compile(r"^v([0-9]{2,})$")
_STATUS_SEGS = {"draft", "final"}
# Full-name parser: keywords, optional date, optional version/status, optional
# ext. The regex matches STRUCTURE only — segments split on the grammar's two
# separators — and _valid_keyword then checks each segment's characters.
# Splitting it this way is what makes every script parseable: a character
# class cannot express "letter, digit, or combining mark" in stdlib `re`
# without hand-listing the mark ranges of every script, and any class built
# on \w silently treats Devanagari and Thai vowel signs as separators.
_SEG = r"[^-.]+"
# The compound extensions are listed explicitly rather than allowed as a
# general "one or more dotted parts": that looser form would swallow
# "file.name.with.dots.txt" whole and call it the extension.
_EXT = "|".join(re.escape(e) for e in COMPOUND_EXTENSIONS) + r"|\.[a-z0-9]+"
_NAME_RE = re.compile(
    rf"^(?P<keywords>{_SEG}(?:-{_SEG})*?)"
    r"(?:-(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2}))?"
    r"(?:-(?P<verstat>v[0-9]{2,}|draft|final))?"
    rf"(?P<ext>{_EXT})?$",
    re.UNICODE,
)


def _valid_keyword(segment: str) -> bool:
    """A keyword segment: non-empty, all keyword characters, none uppercase.

    The lowercase rule is what keeps parse() rejecting names that merely
    look close to the convention, like "Tax-Return.pdf".
    """
    return bool(segment) and all(
        is_token_char(ch) and not ch.isupper() for ch in segment
    )


def normalize_version(version: int | str | None) -> str | None:
    """1 -> 'v01', 'v3' -> 'v03', 'draft'/'final' pass through."""
    if version is None:
        return None
    if isinstance(version, str):
        v = version.lower()
        if v in _STATUS_SEGS:
            return v
        m = _VERSION_SEG.match(v) or re.match(r"^v?([0-9]+)$", v)
        if not m:
            raise ValueError(f"unrecognized version/status: {version!r}")
        return f"v{int(m.group(1)):02d}"
    return f"v{int(version):02d}"


def assemble(
    keywords: list[str],
    date: str | None = None,
    version: int | str | None = None,
    ext: str = "",
    redundancy_set: set[str] | None = None,
) -> str:
    """Build a convention-compliant name. Raises ValueError on bad input."""
    from folder_context_resolver import strip_redundant  # local import: sibling module

    if not keywords:
        raise ValueError("at least one keyword is required")
    if date is not None and not _DATE_SEG.match(date):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")

    # Kebab-normalize each keyword (may itself contain spaces/underscores).
    segs: list[str] = []
    for kw in keywords:
        segs.extend(tokenize(kw))
    if not segs:
        raise ValueError("keywords produced no usable tokens")

    original = list(segs)
    if redundancy_set:
        segs = strip_redundant(segs, redundancy_set)
    # The grammar's keyword count is settled here, after redundancy, so
    # every caller in both modes gets it without having to remember.
    segs = enforce_keyword_count(segs, original)

    stem = "-".join(segs)
    if date:
        stem += f"-{date}"
    verstat = normalize_version(version)
    if verstat:
        stem += f"-{verstat}"

    # Windows safety: reserved device names and stem length cap.
    if stem in WINDOWS_RESERVED:
        stem += "-file"
    if len(stem) > MAX_STEM_LEN:
        # Trim whole keyword segments from the end, never the date/version.
        tail = ""
        if verstat:
            tail = f"-{verstat}" + tail
        if date:
            tail = f"-{date}" + tail
        room = MAX_STEM_LEN - len(tail)
        kept = []
        for seg in segs:
            candidate = "-".join(kept + [seg])
            if len(candidate) > room:
                break
            kept.append(seg)
        if not kept:  # single huge keyword: hard-truncate it
            kept = [segs[0][:room]]
        stem = "-".join(kept) + tail

    return stem + ext.lower()


def parse(name: str) -> dict:
    """Split a convention-compliant name into fields.

    Returns {keywords: [...], date: str|None, version: str|None,
    status: str|None, ext: str}. Raises ValueError if the name does not
    match the grammar.
    """
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"name does not match convention grammar: {name!r}")
    keywords = m.group("keywords").split("-")
    if not all(_valid_keyword(k) for k in keywords):
        raise ValueError(f"name does not match convention grammar: {name!r}")
    verstat = m.group("verstat")
    version = status = None
    if verstat:
        if verstat in _STATUS_SEGS:
            status = verstat
        else:
            version = verstat
    return {
        "keywords": keywords,
        "date": m.group("date"),
        "version": version,
        "status": status,
        "ext": m.group("ext") or "",
    }


MAX_DISAMBIGUATION = 999


def disambiguate(proposed: str, taken: set[str]) -> str:
    """Resolve a collision with a numeric keyword suffix (deterministic fallback).

    Semantic disambiguation (an extra distinguishing keyword from content)
    is preferred and happens upstream; this is the last resort.

    The counter has to survive assemble()'s two size limits, and neither is
    obvious: appended as an extra keyword it is the segment KEYWORD_MAX
    discards, and appended to an already-long stem it is the segment
    MAX_STEM_LEN trims. Either way every candidate comes back equal to
    *proposed*, still collides, and the search never ends — a four-keyword
    name colliding once was enough to hang the whole run. So a keyword slot
    is reserved for the counter up front, the result is checked to confirm
    it actually landed, and the loop is bounded rather than open.
    """
    if proposed not in taken:
        return proposed
    fields = parse(proposed)
    version = fields["version"] or fields["status"]

    for n in range(2, MAX_DISAMBIGUATION + 1):
        counter = str(n)
        base = fields["keywords"][:KEYWORD_MAX - 1]
        while True:
            candidate = assemble(base + [counter], date=fields["date"],
                                 version=version, ext=fields["ext"])
            if counter in parse(candidate)["keywords"] or not base:
                break
            base = base[:-1]  # stem cap ate the counter: make room and retry
        if candidate not in taken:
            return candidate

    raise ValueError(
        f"could not disambiguate {proposed!r}: "
        f"{MAX_DISAMBIGUATION} variants are all taken"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("assemble")
    a.add_argument("keywords", nargs="+")
    a.add_argument("--date")
    a.add_argument("--version")
    a.add_argument("--ext", default="")
    a.add_argument("--redundant", nargs="*", default=[])

    p = sub.add_parser("parse")
    p.add_argument("name")

    args = ap.parse_args(argv)
    if args.cmd == "assemble":
        print(assemble(args.keywords, date=args.date, version=args.version,
                       ext=args.ext, redundancy_set=set(args.redundant)))
    else:
        print(json.dumps(parse(args.name), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
