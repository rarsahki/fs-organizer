"""Regression suite for the Windows edge cases fs-organizer has to survive.

Every test here corresponds to a defect found by running the skill against
a deliberately hostile corpus. They exist so a future change cannot quietly
reintroduce one. No pytest dependency — run it directly:

    python tests/test_windows_edges.py

Exits non-zero if anything fails.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

def _find_scripts() -> Path:
    """Locate scripts/, whether tests/ sits beside it or at a repo root.

    Installed, the skill is flat: <skill>/tests and <skill>/scripts. Packaged
    as a plugin, tests live at the repo root while the skill sits under
    skills/fs-organizer/. The same file has to run in both.
    """
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "scripts",
                      here.parent / "skills" / "fs-organizer" / "scripts"):
        if (candidate / "fsorg_common.py").is_file():
            return candidate
    raise SystemExit("cannot locate the skill's scripts/ directory")


SCRIPTS = _find_scripts()
sys.path.insert(0, str(SCRIPTS))

from batch_executor import execute_plan                                    # noqa: E402
from content_fingerprint_extractor import _head_text, extract_fingerprint  # noqa: E402
from fsorg_common import (is_excluded, is_hidden, is_reparse_point,          # noqa: E402
                          split_name, tokenize)
from index_manager import build_index, load_index, update_index           # noqa: E402
from name_assembler import assemble, disambiguate, parse                  # noqa: E402
from path_context_reader import read_context_tree                         # noqa: E402

_failures: list[str] = []


def check(name: str, got, expected) -> None:
    if got == expected:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}\n          got:      {got!r}\n          expected: {expected!r}")
        _failures.append(name)


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
# Naming grammar
# --------------------------------------------------------------------------
def test_names() -> None:
    section("naming grammar")

    check("basic assemble", assemble(["tax", "return"], ext=".pdf"), "tax-return.pdf")
    check("date + version",
          assemble(["tax", "return"], date="2024-04-15", version=2, ext=".pdf"),
          "tax-return-2024-04-15-v02.pdf")

    # Latin diacritics fold; other scripts keep their own characters. Before
    # this, [a-z0-9] tokenization deleted them: "café"->"caf", "報告書"->"",
    # and a Cyrillic name raised "keywords produced no usable tokens".
    check("folds latin accents", assemble(["café", "notes"], ext=".txt"), "cafe-notes.txt")
    check("splits nothing on umlaut", assemble(["naïve", "über"], ext=".txt"), "naive-uber.txt")
    check("keeps cjk", assemble(["報告書", "2024"], ext=".txt"), "報告書-2024.txt")
    check("keeps cyrillic", assemble(["предложение"], ext=".txt"), "предложение.txt")
    check("keeps greek tonos", assemble(["Ελλάδα", "report"], ext=".txt"), "ελλάδα-report.txt")

    for name in ("報告書-2024-2024-04-15-v02.pdf", "предложение-документ.txt",
                 "cafe-notes-final.pdf", "tax-return-2024-04-15-v02.pdf"):
        fields = parse(name)
        roundtrip = assemble(fields["keywords"], date=fields["date"],
                             version=fields["version"] or fields["status"],
                             ext=fields["ext"])
        check(f"round-trips {name}", roundtrip, name)

    # Dates and versions stay ASCII even though keywords no longer are.
    check("rejects non-ascii digits in date",
          _raises(lambda: assemble(["a", "b"], date="٢٠٢٤-٠٤-١٥")), "ValueError")
    check("still rejects uppercase on parse", _raises(lambda: parse("Tax-Return.pdf")),
          "ValueError")

    # A 4-keyword collision used to spin forever: the counter became a 5th
    # keyword and KEYWORD_MAX discarded it, so every candidate was identical.
    check("disambiguates a full-width name", disambiguate("a-b-c-d.pdf", {"a-b-c-d.pdf"}),
          "a-b-c-2.pdf")
    check("disambiguates through many collisions",
          disambiguate("a-b-c-d.pdf", {"a-b-c-d.pdf"} | {f"a-b-c-{n}.pdf" for n in range(2, 40)}),
          "a-b-c-40.pdf")
    # The stem cap can also eat the counter; the counter must still survive.
    long_taken = "supercalifragilistic-supercalifragilistic-2024-01-01-v03.pdf"
    check("disambiguates past the stem cap",
          "-2-" in disambiguate(long_taken, {long_taken}), True)

    check("windows reserved stem", assemble(["con"], ext=".txt"), "con-file.txt")

    # Path.suffix returns only ".gz", which loses the archive format and
    # leaves a stray "tar" behind to be picked up as a keyword.
    check("splits a tar pair", split_name("backup.tar.gz"), ("backup", ".tar.gz"))
    check("splits .tar.zst", split_name("src.TAR.ZST"), ("src", ".tar.zst"))
    check("leaves ordinary dotted names alone",
          split_name("file.name.with.dots.txt"), ("file.name.with.dots", ".txt"))
    check("a bare .tar.gz is not a compound", split_name(".tar.gz")[1], ".gz")
    check("assembles a tar pair",
          assemble(["database", "backup"], ext=".tar.gz"), "database-backup.tar.gz")
    check("parses a tar pair back",
          parse("database-backup.tar.gz")["ext"], ".tar.gz")
    check("does not swallow ordinary dots as an extension",
          _raises(lambda: parse("file.name.with.dots.txt")), "ValueError")


def _raises(fn) -> str:
    try:
        fn()
    except Exception as exc:
        return type(exc).__name__
    return "no exception"


# --------------------------------------------------------------------------
# Exclusion rules
# --------------------------------------------------------------------------
def test_exclusions(root: Path) -> None:
    section("exclusion rules")

    ordinary = root / "ordinary.txt"
    ordinary.write_text("plain", encoding="utf-8")
    check("ordinary file included", is_excluded(ordinary), False)

    # attrib +H sets a bit; it does not rename the file. The dot-prefix
    # check alone missed every file Windows actually marks hidden.
    hidden = root / "HiddenByAttribute.txt"
    hidden.write_text("h", encoding="utf-8")
    subprocess.run(["attrib", "+H", str(hidden)], capture_output=True)
    check("hidden attribute detected", is_hidden(hidden), True)
    check("hidden attribute excluded", is_excluded(hidden), True)

    system = root / "SystemFile.dat"
    system.write_bytes(b"s")
    subprocess.run(["attrib", "+S", str(system)], capture_output=True)
    check("system attribute excluded", is_excluded(system), True)

    lock = root / "~$report.docx"
    lock.write_bytes(b"lock")
    check("office lock file excluded", is_excluded(lock), True)

    check("dot file still excluded", is_excluded(root / ".hidden"), True)


def test_junctions(root: Path) -> None:
    section("junctions and symlinks")

    outside = root.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("must never be read", encoding="utf-8")

    scope = root / "scope"
    scope.mkdir(exist_ok=True)
    (scope / "own.txt").write_text("mine", encoding="utf-8")
    junction = scope / "junction-out"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                   capture_output=True)
    if not junction.exists():
        print("  SKIP  junction tests (mklink unavailable)")
        return

    check("junction detected", is_reparse_point(junction), True)
    check("junction excluded", is_excluded(junction), True)

    # Walking a junction pulls files from outside the scope into the run,
    # where they get read and fingerprinted before anything rejects them.
    tree = read_context_tree(scope)
    seen = {f["name"] for batch in tree["batches"] for f in batch["files"]}
    check("scope walk does not cross a junction", "secret.txt" in seen, False)
    check("scope walk still sees its own files", "own.txt" in seen, True)

    # build_index died here with an uncaught ValueError from relative_to.
    index_path = root / "junction-index.json"
    index = build_index(scope, index_path)
    indexed = {p for paths in index["by_sha256"].values() for p in paths}
    check("index build survives a junction", any("own.txt" in p for p in indexed), True)
    check("index excludes outside-scope files",
          any("secret" in p for p in indexed), False)


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------
def test_executor(root: Path) -> None:
    section("executor")

    scope = root / "exec-scope"
    scope.mkdir(exist_ok=True)
    outside = root / "exec-outside"
    outside.mkdir(exist_ok=True)
    victim = outside / "victim.txt"
    victim.write_text("safe", encoding="utf-8")
    journals = root / "journals"

    def run(ops):
        journal = execute_plan(ops, journal_dir=journals, scope=scope)
        return [op["status"] for op in journal["operations"]]

    check("out-of-scope delete rejected",
          run([{"op": "delete", "path": str(victim)}])[0], "rejected-out-of-scope")
    check("out-of-scope file survives", victim.exists(), True)
    check("dotdot escape rejected",
          run([{"op": "delete", "path": str(scope / ".." / "exec-outside" / "victim.txt")}])[0],
          "rejected-out-of-scope")

    # A locked file raises PermissionError, which used to share an except
    # clause with the scope check and so reported as a scope violation.
    locked = scope / "locked.txt"
    locked.write_text("busy", encoding="utf-8")
    handle = open(locked, "r+b")
    try:
        status = run([{"op": "rename", "src": str(locked), "dst": str(scope / "freed.txt")}])[0]
    finally:
        handle.close()
    check("locked file reports failure, not a scope violation", status, "failed")

    # Case-only renames: exists() is case-insensitive, so the source always
    # looks present and a re-run redid the work every time.
    mixed = scope / "MiXeD.TXT"
    mixed.write_text("c", encoding="utf-8")
    plan = [{"op": "rename", "src": str(mixed), "dst": str(scope / "mixed.txt")}]
    check("case-only rename applies", run(plan)[0], "applied")
    check("case-only rename is idempotent", run(plan)[0], "already-applied")
    check("case-only rename landed lowercase",
          any(p.name == "mixed.txt" for p in scope.iterdir()), True)

    # MAX_PATH is only lifted when LongPathsEnabled is set in the registry,
    # and it is off by default, so the executor must not depend on it.
    deep = scope
    for i in range(6):
        deep = deep / ("longdir-" + "y" * 30 + str(i))
    source = scope / "L.txt"
    source.write_text("long", encoding="utf-8")
    statuses = run([{"op": "mkdir", "path": str(deep)},
                    {"op": "move", "src": str(source), "dst": str(deep / "moved.txt")}])
    check(f"survives a {len(str(deep / 'moved.txt'))}-char destination",
          statuses, ["applied", "applied"])

    keep = scope / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    other = scope / "other.txt"
    other.write_text("other", encoding="utf-8")
    check("refuses to overwrite",
          run([{"op": "rename", "src": str(keep), "dst": str(other)}])[0], "failed")
    check("overwrite target intact", other.read_text(encoding="utf-8"), "other")

    check("unknown op fails cleanly",
          run([{"op": "frobnicate", "path": str(scope / "keep.txt")}])[0], "failed")

    # Journals belong to the scope, like every other piece of its state.
    journal = execute_plan([{"op": "mkdir", "path": str(scope / "probe")}], scope=scope)
    check("journal is scoped to the run",
          Path(journal["journal_path"]).parent.parent.name, scope.name)


# --------------------------------------------------------------------------
# Fingerprints and the index
# --------------------------------------------------------------------------
def test_delete_without_send2trash(root: Path) -> None:
    """Deletion must work on a fresh install, with nothing pip-installed.

    Note: this really does recycle its temp files, because the Recycle Bin
    is the whole point — a mock would prove nothing about recoverability.
    They land in the Bin under a fsorg-tests- temp path and can be purged.
    """
    section("deletion without send2trash")

    scope = root / "delete-scope"
    scope.mkdir(exist_ok=True)
    journals = root / "del-journals"

    import batch_executor

    # A None entry makes `import send2trash` raise, whether or not it is
    # installed on the machine running these tests.
    saved = sys.modules.get("send2trash", "absent")
    sys.modules["send2trash"] = None
    try:
        targets = ["duplicate.txt", "дубликат.txt", "重複ファイル.txt", "with spaces (2).txt"]
        ops = []
        for name in targets:
            (scope / name).write_text("delete me", encoding="utf-8")
            ops.append({"op": "delete", "path": str(scope / name)})
        folder = scope / "a folder"
        folder.mkdir(exist_ok=True)
        (folder / "inner.txt").write_text("x", encoding="utf-8")
        ops.append({"op": "delete", "path": str(folder)})

        journal = batch_executor.execute_plan(ops, journal_dir=journals, scope=scope)
        check("every delete applied", journal["applied"], len(ops))
        check("no delete failed", journal["failed"], 0)
        check("files really gone", any((scope / n).exists() for n in targets), False)
        check("directory really gone", folder.exists(), False)
        check("recorded as recoverable",
              {op.get("recoverable") for op in journal["operations"]}, {"recycle-bin"})
    finally:
        if saved == "absent":
            del sys.modules["send2trash"]
        else:
            sys.modules["send2trash"] = saved

    # And a missing target is never a permanent-delete fallback.
    journal = batch_executor.execute_plan(
        [{"op": "delete", "path": str(scope / "never-existed.txt")}],
        journal_dir=journals, scope=scope)
    check("missing target is a no-op", journal["operations"][0]["status"], "already-applied")


def test_fingerprints(root: Path) -> None:
    section("fingerprints")

    # read_text()[:cap] materialised the whole file before slicing it.
    big = root / "big.log"
    with big.open("wb") as handle:
        for _ in range(400):
            handle.write(b"2024-01-01 INFO a line of log output\n" * 2000)
    size_mb = big.stat().st_size // 1024 // 1024
    text = _head_text(big)
    check(f"reads only the head of a {size_mb}MB file", len(text) <= 8192, True)

    # Notepad still writes BOMs; decoding those as plain UTF-8 put a stray
    # ﻿ at the front of the title, or produced mojibake outright.
    bom8 = root / "bom8.txt"
    bom8.write_bytes(b"\xef\xbb\xbfTitle Here\r\n\r\nBody text.\r\n")
    check("strips a utf-8 BOM", extract_fingerprint(bom8)["title"], "Title Here")

    bom16 = root / "bom16.txt"
    bom16.write_bytes("Wide Title\r\n\r\nBody.\r\n".encode("utf-16-le"))
    bom16.write_bytes(b"\xff\xfe" + "Wide Title\r\n\r\nBody.\r\n".encode("utf-16-le"))
    check("decodes utf-16", extract_fingerprint(bom16)["title"], "Wide Title")

    # 0-byte files get no hash: sha256("") is a constant, so hashing them
    # would cluster every unrelated empty file as an exact duplicate.
    empty = root / "empty.txt"
    empty.write_bytes(b"")
    fingerprint = extract_fingerprint(empty)
    check("empty file flagged", fingerprint["modality"], "empty")
    check("empty file unhashed", "sha256" in fingerprint, False)


def test_index(root: Path) -> None:
    section("index")

    scope = root / "index-scope"
    scope.mkdir(exist_ok=True)
    loose = scope / "notes.txt"
    loose.write_text("Meeting notes\n\nSome content.\n", encoding="utf-8")
    index_path = root / "index.json"
    build_index(scope, index_path)
    check("loose file indexed", "notes.txt" in load_index(index_path)["loose_files"], True)

    # update_index is called with the post-move path, but loose_files holds
    # bare names, so without the previous path the old entry was unreachable
    # and stayed forever. Organize mode's closing rebuild hid this; watcher
    # mode has no rebuild.
    folder = scope / "refs"
    folder.mkdir(exist_ok=True)
    moved = folder / "meeting-notes-summary.txt"
    loose.rename(moved)
    update_index(index_path, "refs/meeting-notes-summary.txt",
                 extract_fingerprint(moved), "refs", previous_rel="notes.txt")
    remaining = load_index(index_path)["loose_files"]
    check("moved file leaves no ghost", "notes.txt" in remaining, False)
    check("no stale loose entries at all",
          [n for n in remaining if not (scope / n).exists()], [])


def test_tokenizer() -> None:
    section("tokenizer")
    check("kebab", tokenize("tax-return"), ["tax", "return"])
    check("snake", tokenize("tax_return"), ["tax", "return"])
    check("camel", tokenize("ClientReport"), ["client", "report"])
    check("counter", tokenize("invoice (1)"), ["invoice", "1"])
    check("accents folded", tokenize("résumé café"), ["resume", "cafe"])
    check("cjk kept", tokenize("報告書 2024"), ["報告書", "2024"])
    check("cyrillic kept", tokenize("предложение"), ["предложение"])
    # Devanagari vowel signs are combining marks too — folding them would
    # delete the word rather than an accent.
    check("devanagari intact", tokenize("हिंदी"), ["हिंदी"])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="fsorg-tests-") as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        test_names()
        test_tokenizer()
        test_exclusions(root)
        test_junctions(root)
        test_executor(root)
        test_delete_without_send2trash(root)
        test_fingerprints(root)
        test_index(root)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
