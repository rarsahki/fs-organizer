"""content_fingerprint_extractor — Step 2 (Fingerprint) of the fs-organizer workflow.

Extracts a compact, token-cheap fingerprint per file — never full content:
- text-bearing files: title/first heading, first paragraph, word count
- PDF/DOCX: same, via optional libs (pypdf / python-docx); metadata-only fallback
- images/audio: pluggable model hooks (vision caption / transcript) —
  registered by task 3 once a privacy-compliant provider is chosen; until
  then these return modality + metadata only

Every non-empty file's fingerprint also carries a `sha256` content hash
(via fsorg_common.sha256_file — the one hashing implementation shared with
Step 3's duplicate scanner, so a file is read once, not twice). Empty (0-byte)
files deliberately get no hash: sha256("") is the same constant for every
0-byte file, so hashing them would falsely group unrelated empty files as
"exact duplicates" — they're flagged and excluded from dedup instead.

Usage as module:   from content_fingerprint_extractor import extract_fingerprint
Usage as CLI (single item):  python content_fingerprint_extractor.py <file> [...]
Usage as CLI (bulk):         python content_fingerprint_extractor.py --from context.json --output fingerprints.json
"""
from __future__ import annotations

import argparse
import json
import logging
import warnings
from pathlib import Path
from typing import Callable

from fsorg_common import sha256_file, split_name

# The optional parsers narrate over stderr — pypdf logs "EOF marker not
# found" for any PDF with a slightly off trailer. Harmless, but it lands in
# the middle of a step's output where it reads as a failure of the run.
logging.getLogger("pypdf").setLevel(logging.ERROR)

TEXT_EXTS = {
    ".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".sh",
    ".ps1", ".bat", ".ini", ".cfg", ".toml", ".log",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".heic"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".wma"}

_READ_CAP = 8192  # bytes of text we ever read — fingerprints stay cheap

# task 3 plugs privacy-compliant local models in here.
# Signature: hook(path) -> str | None  (short caption / transcript excerpt)
MODEL_HOOKS: dict[str, Callable[[Path], str | None] | None] = {
    "image_caption": None,
    "audio_transcript": None,
}


# BOM-aware codecs, so the mark itself is consumed rather than decoded into
# a leading ﻿ that would end up at the front of the file's title.
# Longest prefix first: a UTF-32-LE BOM starts with the UTF-16-LE one.
_BOM_ENCODINGS = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def _head_text(path: Path, cap: int = _READ_CAP) -> str | None:
    """Decode at most *cap* characters from the start of *path*.

    Reads a bounded number of BYTES. The previous read_text()[:cap] sliced
    only after materialising the whole file, so summarising a multi-
    gigabyte log or CSV meant loading all of it into memory to produce a
    500-character fingerprint.

    Also honours a byte-order mark. Notepad still writes UTF-8-with-BOM and
    UTF-16 on Windows, and decoding those as plain UTF-8 put a stray
    \\ufeff at the front of every title or turned the text into mojibake —
    which then became the file's proposed name.
    """
    try:
        with path.open("rb") as f:
            raw = f.read(cap * 4)  # worst-case bytes per char, bounded
    except OSError:
        return None
    encoding = "utf-8"
    for bom, enc in _BOM_ENCODINGS:
        if raw.startswith(bom):
            encoding = enc
            break
    # lstrip as well: some producers write a BOM the codec then keeps, and a
    # bounded read can slice a UTF-16 pair in half, which errors="replace"
    # absorbs rather than raising.
    return raw.decode(encoding, errors="replace").lstrip("﻿")[:cap]


def _text_fingerprint(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines()]
    nonempty = [ln for ln in lines if ln]
    title = nonempty[0].lstrip("#").strip() if nonempty else ""
    # First paragraph: consecutive non-empty lines after the title line.
    para_lines: list[str] = []
    seen_title = False
    for ln in lines:
        if not seen_title:
            if ln:
                seen_title = True
            continue
        if ln:
            para_lines.append(ln)
        elif para_lines:
            break
    return {
        "title": title[:200],
        "first_paragraph": " ".join(para_lines)[:500],
        "word_count": len(text.split()),
    }


def _pdf_text(path: Path) -> str | None:
    try:
        # pypdf pulls in `cryptography`, which warns on import about a
        # moved ARC4 class. It is a UserWarning subclass, so no category
        # filter suppresses it — only silencing the import itself does.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from pypdf import PdfReader  # optional dependency
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages[:2]:  # first pages are enough for a fingerprint
            chunks.append(page.extract_text() or "")
            if sum(len(c) for c in chunks) > _READ_CAP:
                break
        return "\n".join(chunks)[:_READ_CAP]
    except Exception:
        return None


def _docx_text(path: Path) -> str | None:
    try:
        import docx  # optional dependency: python-docx
    except ImportError:
        return None
    try:
        d = docx.Document(str(path))
        out, total = [], 0
        for p in d.paragraphs:
            out.append(p.text)
            total += len(p.text)
            if total > _READ_CAP:
                break
        return "\n".join(out)[:_READ_CAP]
    except Exception:
        return None


_EXIF_DATE_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime


def _exif_date(path: Path) -> str | None:
    """EXIF capture date as YYYY-MM-DD, via optional Pillow. None if absent."""
    try:
        from PIL import Image  # optional dependency: Pillow
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            for tag in _EXIF_DATE_TAGS:
                raw = exif.get(tag)
                if raw:  # EXIF format: "YYYY:MM:DD HH:MM:SS"
                    date_part = str(raw).split()[0].replace(":", "-")
                    if len(date_part) == 10:
                        return date_part
    except Exception:
        pass
    return None


_DIR_SAMPLE = 20  # entry names included in a directory fingerprint


def _dir_fingerprint(p: Path) -> dict:
    """Fingerprint a directory as a unit: a shallow signal (entry names,
    counts, size), never a per-file crawl. Watcher mode places an arriving
    folder whole — an extracted archive, a copied project — judged from
    what it's called and what it visibly holds; its contents are only
    individually organized if the user later runs Organize mode on it.
    No sha256: byte-identity has no meaning for a directory, so folders
    never participate in duplicate detection.
    """
    files = dirs = 0
    total = 0
    names: list[str] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.is_dir():
                dirs += 1
            else:
                files += 1
                try:
                    total += child.stat().st_size
                except OSError:
                    pass
            if len(names) < _DIR_SAMPLE:
                names.append(child.name + ("/" if child.is_dir() else ""))
    except PermissionError:
        return {"path": str(p), "ext": "", "modality": "unreadable-directory"}
    return {
        "path": str(p), "ext": "", "modality": "directory",
        "file_count": files, "dir_count": dirs, "size": total,
        "entries": names,
    }


def extract_fingerprint(path: str | Path) -> dict:
    """Return {path, modality, ...signal fields}. Never raises on content errors."""
    p = Path(path)
    if p.is_dir():
        return _dir_fingerprint(p)
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {p}")
    stem, ext = split_name(p)
    base = {"path": str(p), "ext": ext, "stem": stem, "size": p.stat().st_size}

    if base["size"] == 0:
        # No content signal at all — callers must flag, never rename.
        # No hash either (see module docstring: empty files would falsely
        # collide with every other empty file under sha256).
        return {**base, "modality": "empty",
                "note": "0-byte file (failed download / accidental save?)"}

    base["sha256"] = sha256_file(p)

    if ext in TEXT_EXTS:
        text = _head_text(p)
        if text is None:
            return {**base, "modality": "unreadable"}
        return {**base, "modality": "text", **_text_fingerprint(text)}

    if ext == ".pdf":
        text = _pdf_text(p)
        if text and text.strip():
            return {**base, "modality": "text", **_text_fingerprint(text)}
        return {**base, "modality": "binary", "note": "pdf text extraction unavailable"}

    if ext == ".docx":
        text = _docx_text(p)
        if text and text.strip():
            return {**base, "modality": "text", **_text_fingerprint(text)}
        return {**base, "modality": "binary", "note": "docx text extraction unavailable"}

    if ext in IMAGE_EXTS:
        hook = MODEL_HOOKS.get("image_caption")
        caption = hook(p) if hook else None
        out = {**base, "modality": "image"}
        exif_date = _exif_date(p)
        if exif_date:
            # Capture time embedded by the device — outranks filesystem
            # dates in the naming convention's date hierarchy.
            out["exif_date"] = exif_date
        if caption:
            out["caption"] = caption[:300]
        else:
            out["note"] = "vision model pending (task 3)"
        return out

    if ext in AUDIO_EXTS:
        hook = MODEL_HOOKS.get("audio_transcript")
        transcript = hook(p) if hook else None
        out = {**base, "modality": "audio"}
        if transcript:
            out["transcript_excerpt"] = transcript[:500]
        else:
            out["note"] = "transcription model pending (task 3)"
        return out

    return {**base, "modality": "binary"}


# Which optional package would have read each fallback note's file type.
_PARSER_FOR_NOTE = {
    "pdf text extraction unavailable": "pypdf",
    "docx text extraction unavailable": "python-docx",
}


def _missing_parser_hint(results: list[dict]) -> str | None:
    """One line naming what could not be read, and what would fix it.

    Every optional parser has a metadata-only fallback, so a missing one
    never fails a run — it just quietly produces a worse name. Saying so
    is the difference between "this skill names PDFs badly" and "installing
    one package makes it name them well".
    """
    counts: dict[str, int] = {}
    for fp in results:
        package = _PARSER_FOR_NOTE.get(fp.get("note", ""))
        if package:
            counts[package] = counts.get(package, 0) + 1
    if not counts:
        return None
    parts = [f"{n} file(s) need {pkg}" for pkg, n in sorted(counts.items())]
    return ("  note: named without reading content — " + "; ".join(parts)
            + f". Install with: pip install {' '.join(sorted(counts))}")


def _files_from_context(context_path: str | Path) -> list[str]:
    """Expand a context.json's folder batches into a flat file list."""
    ctx = json.loads(Path(context_path).read_text(encoding="utf-8-sig"))
    files = []
    for batch in ctx["batches"]:
        folder = Path(batch["folder"])
        for meta in batch["files"]:
            files.append(str(folder / meta["name"]))
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", help="explicit files (single-item form)")
    ap.add_argument("--from", dest="from_", default=None,
                    help="context.json to expand into the full file list (bulk form)")
    ap.add_argument("--output", default=None,
                    help="write results here instead of stdout (required with --from)")
    args = ap.parse_args(argv)

    if args.from_:
        file_list = _files_from_context(args.from_)
    else:
        file_list = args.files
    if not file_list:
        ap.error("no files given: pass files directly or --from context.json")

    if args.output:
        results, errors = [], []
        for f in file_list:
            try:
                results.append(extract_fingerprint(f))
            except Exception as e:
                errors.append({"path": f, "error": f"{type(e).__name__}: {e}"})
        out = {"fingerprints": results, "errors": errors}
        Path(args.output).write_text(json.dumps(out, indent=2), encoding="utf-8")
        empty = sum(1 for r in results if r.get("modality") == "empty")
        print(f"Fingerprinted {len(results)} files ({empty} empty), "
              f"{len(errors)} errors -> {args.output}")
        # Name the missing parsers rather than quietly producing worse names:
        # a PDF read as an opaque blob still gets named, just from its old
        # filename and folder instead of its contents, and nothing about the
        # result says so.
        hint = _missing_parser_hint(results)
        if hint:
            print(hint)
    else:
        results = [extract_fingerprint(f) for f in file_list]
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
