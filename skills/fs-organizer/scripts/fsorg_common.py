"""Shared helpers for fs-organizer scripts.

Internal module — not one of the six tools. Holds tokenization,
exclusion, and hashing rules that several tools need so they stay
consistent.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path

# Files/dirs the organizer must never rename, move, or propose changes for.
EXCLUDED_FILES = {"desktop.ini", "thumbs.db", ".ds_store"}
# Office writes a lock file alongside every open document ("~$report.docx").
# It is live state belonging to a running app, not a document.
EXCLUDED_PREFIXES = ("~$",)
EXCLUDED_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".claude",
}

# Windows reserved device names — illegal as a bare file stem.
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

def is_token_char(ch: str) -> bool:
    """True for a character that can appear inside a keyword.

    Letters and digits of any script, plus combining marks. The marks are
    the part that is easy to miss and expensive to get wrong: regex `\\w`
    excludes them, so a class built on it splits हिंदी into ['ह', 'द'] —
    every vowel sign becomes a separator and the word is destroyed. The
    same applies to Thai, Tamil, Bengali, and to Arabic or Hebrew written
    with vowel points.
    """
    if ch == "_":
        return False
    category = unicodedata.category(ch)
    return category[0] in ("L", "N") or category in ("Mn", "Mc", "Me")


# Two-part extensions that name ONE format. `.tar.gz` is not a gzip file
# that happens to be called "backup.tar" — the pair is the format, and
# Path.suffix returning only ".gz" both loses that and leaves a stray "tar"
# to be picked up as a keyword. Deliberately limited to the tar family:
# ".min.js" and ".d.ts" look similar but those leading parts are really
# name components, not extensions.
COMPOUND_EXTENSIONS = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar.lz")


def split_name(path: str | Path) -> tuple[str, str]:
    """Split a filename into (stem, extension), honouring compound extensions.

    `split_name("backup.tar.gz")` -> `("backup", ".tar.gz")`, where
    `Path.stem`/`Path.suffix` would give `("backup.tar", ".gz")`.
    """
    name = Path(path).name
    lowered = name.lower()
    for compound in COMPOUND_EXTENSIONS:
        if lowered.endswith(compound) and len(lowered) > len(compound):
            return name[: -len(compound)], compound
    p = Path(name)
    return p.stem, p.suffix.lower()


_MARK_CATEGORIES = ("Mn", "Mc", "Me")


def _strip_leading_marks(token: str) -> str:
    """Drop marks with no base character to modify, at a token's start."""
    i = 0
    while i < len(token) and unicodedata.category(token[i]) in _MARK_CATEGORIES:
        i += 1
    return token[i:] or token


def _split_tokens(text: str) -> list[str]:
    """Runs of keyword characters, in order."""
    tokens, current = [], []
    for ch in text:
        if is_token_char(ch):
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return [_strip_leading_marks(t) for t in tokens]


def _fold_latin_accents(name: str) -> str:
    """Strip diacritics from Latin letters only: café -> cafe, naïve -> naive.

    Restricted to Latin bases on purpose. Devanagari vowel signs, Arabic
    and Hebrew points, and Thai tone marks are combining characters too,
    and dropping those does not "fold an accent" — it destroys the word.
    Non-Latin scripts therefore pass through untouched and keep their own
    characters as keyword tokens.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFD", name):
        if unicodedata.combining(ch) and out:
            base = unicodedata.name(out[-1], "")
            if base.startswith("LATIN"):
                continue  # a Latin accent: fold it away
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def tokenize(name: str) -> list[str]:
    """Split a file/folder name into lowercase word tokens.

    Handles kebab-case, snake_case, spaces, camelCase, parenthesised
    counters like "invoice (1)", and non-ASCII names in any script.
    """
    folded = _fold_latin_accents(name)
    # Break camelCase before lowering so "ClientReport" -> client report.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", folded)
    return _split_tokens(spaced.lower())


# Windows file attributes (os.stat_result.st_file_attributes).
FILE_ATTRIBUTE_HIDDEN = 0x02
FILE_ATTRIBUTE_SYSTEM = 0x04
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _attributes(path: Path) -> int:
    """Windows attribute bits for *path*, or 0 where unavailable.

    Never follows a link: a junction's own attributes are the question,
    not its target's. Returns 0 on any OSError so an unreadable entry is
    judged by name alone rather than blowing up the caller.
    """
    try:
        st = path.lstat()
    except OSError:
        return 0
    return getattr(st, "st_file_attributes", 0)


def is_reparse_point(path: Path) -> bool:
    """True for a junction, symlink, or other reparse point.

    These must never be traversed. A junction's contents live somewhere
    else entirely, so walking one silently pulls files from outside the
    scope into the run — they get read, fingerprinted, and planned against
    — and a junction aimed at its own ancestor recurses without bound.
    Windows ships several in every user profile (Documents alone holds
    "My Music", "My Pictures", "My Videos"), so this is the common case,
    not an exotic one.
    """
    return bool(_attributes(path) & FILE_ATTRIBUTE_REPARSE_POINT)


def is_hidden(path: Path) -> bool:
    """Hidden by dot-prefix convention OR by Windows hidden/system attribute.

    The dot-prefix check alone misses how Windows actually marks things
    hidden: `attrib +H` sets a bit, it does not rename the file. Files the
    OS and applications hide on purpose were being treated as ordinary
    documents to rename.
    """
    if path.name.startswith("."):
        return True
    return bool(_attributes(path) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def saved_page_html_for(dir_path: Path) -> Path | None:
    """If *dir_path* is a saved web page's asset folder, return its html.

    Browsers saving a full page write `X.html` plus `X_files/` side by
    side, and the html references the folder by that literal name in its
    hrefs. The pair is one logical item: the folder's contents are asset
    junk (js/css/images), never documents to fingerprint, rename, or
    index individually. Detection is by name: a directory ending in
    `_files` whose sibling `<stem>.html`/`.htm` exists.
    """
    if not dir_path.name.lower().endswith("_files"):
        return None
    stem = dir_path.name[: -len("_files")]
    for ext in (".html", ".htm"):
        sibling = dir_path.parent / (stem + ext)
        if sibling.is_file():
            return sibling
    return None


def is_excluded(path: Path) -> bool:
    """True if the organizer must not touch this file/dir."""
    name = path.name.lower()
    # Checked before is_dir(): is_dir() follows the link, so a junction
    # whose target is missing would otherwise fall through to the file
    # branch and be judged as an ordinary file.
    if is_reparse_point(path):
        return True
    if name.startswith(EXCLUDED_PREFIXES):
        return True
    if path.is_dir():
        if name in EXCLUDED_DIRS or is_hidden(path):
            return True
        # A saved page's asset folder is an attachment of its html, not
        # content: excluded from scanning/indexing, and moved only as part
        # of moving the html (see the workflow docs' saved-page rule).
        return saved_page_html_for(path) is not None
    return name in EXCLUDED_FILES or is_hidden(path)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Content hash of *path*. The one implementation — Step 2 (fingerprints)
    and Step 3 (duplicate scanning) must both call this rather than hashing
    independently, so a file is only ever read once for its hash."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def singularize(token: str) -> str:
    """Naive singular form used only for redundancy comparison (tests -> test)."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token
