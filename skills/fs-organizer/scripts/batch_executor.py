"""batch_executor — Step 7 (Execute) of the fs-organizer workflow.

Applies a plan of filesystem operations with:
- scope enforcement: with --scope, any op touching a path outside the
  given directory is rejected (the run is scoped to that directory and
  nothing else)
- recoverable deletion only: `delete` sends the file to the Windows
  Recycle Bin, never an unlink. Deletions auto-execute without
  confirmation, so the Recycle Bin is the safety net that makes a wrong
  call recoverable. This needs no third-party package — the shell's own
  SHFileOperationW is called through ctypes, with send2trash used first
  when it is installed. If neither route works the op fails and is
  reported rather than falling back to a permanent delete.
- a journal written to ~/.fs-organizer/<scope-name>/journals/<timestamp>.json
  recording every operation and its outcome (enables undo/resume/audit),
  stored under the scope's own state folder like every other per-scope
  artifact — never merged into one pile at the .fs-organizer top level
- Windows safety: two-step rename for case-only changes, per-op error
  isolation (a locked file is skipped + reported, never aborts the batch)
- refusal to overwrite: an op whose destination exists fails that op

Callers must always pass --scope with the directory the run is scoped to.

Plan file format (JSON):
  [{"op": "mkdir",  "path": "..."},
   {"op": "rename", "src": "...", "dst": "..."},      # same parent
   {"op": "move",   "src": "...", "dst": "..."},      # may change parent
   {"op": "delete", "path": "..."}]                   # -> Recycle Bin
Note: a single move op both relocates AND renames (dst = new folder + new
name) in one atomic os.rename — never split it into two ops.

Usage as module:   from batch_executor import execute_plan
Usage as CLI:      python batch_executor.py plan.json --scope <dir> [--dry-run]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

STATE_ROOT = Path.home() / ".fs-organizer"


class ScopeViolation(Exception):
    """An operation whose paths fall outside the run's --scope.

    A distinct type, not PermissionError, because Windows raises
    PermissionError for the entirely routine case of a file being held
    open by another process (Word, Acrobat, OneDrive, an antivirus scan).
    Sharing one exception made every locked file report as
    "rejected-out-of-scope", which accuses the skill of trying to escape
    its scope when it did nothing of the kind — discrediting the one
    guarantee the caller most needs to trust.
    """


def _journal_dir(scope: Path | None) -> Path:
    """Per-scope journal location, mirroring the index and session log."""
    return STATE_ROOT / scope.name / "journals" if scope else STATE_ROOT / "journals"


def _long(p: str | Path) -> str:
    """Absolute path in Windows extended-length form when it needs one.

    The 260-character MAX_PATH limit is lifted only if the machine has
    LongPathsEnabled set in the registry, and that is off by default. The
    \\\\?\\ prefix bypasses the limit unconditionally, so a deep destination
    works on every Windows PC rather than only on the ones whose owner has
    already changed that setting.
    """
    s = os.path.abspath(str(p))
    if os.name != "nt" or len(s) < 240 or s.startswith("\\\\?\\"):
        return s
    return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s


def _exists(p: str | Path) -> bool:
    """exists() that also works past MAX_PATH."""
    return os.path.exists(_long(p))


def _ondisk_name(p: Path) -> str | None:
    """The real on-disk spelling of *p*'s final component, or None if absent."""
    try:
        target = os.path.normcase(p.name)
        with os.scandir(_long(p.parent)) as entries:
            for entry in entries:
                if os.path.normcase(entry.name) == target:
                    return entry.name
    except OSError:
        pass
    return None


def _already_applied(src: Path, dst: Path) -> bool:
    """Heuristic for idempotent re-runs after a mid-batch cutoff: the op's
    source is gone and its destination exists -> this op was applied in a
    previous (interrupted) run of the same plan."""
    if os.path.normcase(str(src)) == os.path.normcase(str(dst)):
        # Case-only rename. exists() is case-insensitive on Windows, so the
        # source always looks present — it is the same directory entry as
        # the destination. Only the entry's real spelling settles whether
        # the rename has happened, and without checking it a re-run redid
        # every case-only rename and reported it as fresh work.
        return _ondisk_name(dst) == dst.name
    return (not _exists(src)) and _exists(dst)


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_FO_DELETE = 3
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040          # this flag IS the Recycle Bin
_FOF_NOERRORUI = 0x0400


def _recycle_native(path: Path) -> None:
    """Send *path* to the Recycle Bin via the Windows shell, using only the
    standard library.

    FOF_ALLOWUNDO is what makes the shell recycle rather than delete, so
    this is the same destination send2trash uses, reached through ctypes
    instead of a package that has to be installed first. Verified against
    files, directories, non-ASCII names, and paths past MAX_PATH.

    pFrom is a double-NUL-terminated list, not a plain string — one NUL
    ends the single entry and the second ends the list.
    """
    if os.name != "nt":
        raise RuntimeError("native Recycle Bin is Windows-only")
    target = os.path.abspath(str(path))
    op = _SHFILEOPSTRUCTW(
        None, _FO_DELETE, target + "\0\0", None,
        _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_NOERRORUI | _FOF_SILENT,
        False, None, None,
    )
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        raise OSError(f"SHFileOperationW failed with code {rc} for {path}")
    if op.fAnyOperationsAborted:
        raise OSError(f"Recycle Bin operation aborted for {path}")
    if _exists(path):
        raise OSError(f"delete reported success but {path} is still present")


def _apply_delete(path: Path) -> None:
    """Send *path* to the Windows Recycle Bin. Never unlinks.

    Deletions in this workflow (byte-identical copies, versions superseded
    by a newer same-named file) auto-execute with no human review, so the
    Recycle Bin is what makes a wrong duplicate call recoverable. There is
    deliberately no permanent-delete fallback: destroying a file the user
    never approved losing is strictly worse than failing this op and
    reporting it.

    send2trash is tried first when it happens to be installed, because a
    dedicated library is likelier to keep pace with shell changes than a
    hand-rolled call. It is not required, though — the native path below
    needs nothing beyond the standard library, so a fresh install can
    de-duplicate on day one rather than reporting every delete as failed.
    """
    if not _exists(path):
        raise FileNotFoundError(f"delete target missing: {path}")

    errors = []
    try:
        from send2trash import send2trash  # optional accelerator
    except ImportError:
        pass
    else:
        try:
            send2trash(str(path))
            if not _exists(path):
                return
            errors.append("send2trash reported success but the file remains")
        except Exception as e:
            errors.append(f"send2trash: {type(e).__name__}: {e}")

    try:
        _recycle_native(path)
        return
    except Exception as e:
        errors.append(f"native: {type(e).__name__}: {e}")

    raise RuntimeError(
        "could not move to the Recycle Bin, so nothing was deleted "
        f"({'; '.join(errors)})"
    )


def _apply_rename_or_move(src: Path, dst: Path) -> None:
    if not _exists(src):
        raise FileNotFoundError(f"source missing: {src}")
    same_target = os.path.normcase(str(src)) == os.path.normcase(str(dst))
    if _exists(dst) and not same_target:
        raise FileExistsError(f"destination exists: {dst}")
    os.makedirs(_long(dst.parent), exist_ok=True)
    if same_target and str(src) != str(dst):
        # Case-only rename on a case-insensitive filesystem: two-step via temp.
        tmp = src.with_name(f".fsorg-tmp-{uuid.uuid4().hex[:8]}-{src.name}")
        os.rename(_long(src), _long(tmp))
        os.rename(_long(tmp), _long(dst))
    else:
        # Same-volume rename/move; raises OSError on cross-volume.
        os.rename(_long(src), _long(dst))


def _in_scope(p: Path, scope: Path) -> bool:
    try:
        p.resolve().relative_to(scope)
        return True
    except ValueError:
        return False


def _check_scope(op: dict, scope: Path | None) -> None:
    """Raise ScopeViolation if any path in *op* falls outside *scope*."""
    if scope is None:
        return
    paths = [op[k] for k in ("path", "src", "dst") if k in op]
    for raw in paths:
        if not _in_scope(Path(raw), scope):
            raise ScopeViolation(f"out of scope ({scope}): {raw}")


def execute_plan(ops: list[dict], dry_run: bool = False,
                 journal_dir: Path | None = None,
                 scope: str | Path | None = None) -> dict:
    """Apply *ops* in order; journal everything. Returns the journal dict.

    With *scope*, every path in every op must resolve inside that directory;
    violating ops are rejected (status "rejected-out-of-scope") and never
    applied — the rest of the batch continues.
    """
    scope_p = Path(scope).resolve() if scope else None
    jdir = Path(journal_dir) if journal_dir else _journal_dir(scope_p)
    started = datetime.now(timezone.utc)
    journal: dict = {
        "started": started.isoformat(),
        "dry_run": dry_run,
        "scope": str(scope_p) if scope_p else None,
        "operations": [],
    }

    # Journal is created up front and flushed after EVERY op, so a mid-batch
    # kill (session cutoff, power loss) always leaves an accurate record of
    # exactly what was applied.
    jpath: Path | None = None
    if not dry_run:
        jdir.mkdir(parents=True, exist_ok=True)
        jpath = jdir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.json"
        journal["journal_path"] = str(jpath)

    def _flush() -> None:
        if jpath is not None:
            jpath.write_text(json.dumps(journal, indent=2), encoding="utf-8")

    _flush()
    for op in ops:
        entry = dict(op)
        try:
            _check_scope(op, scope_p)
            kind = op["op"]
            if kind == "mkdir":
                if not dry_run:
                    os.makedirs(_long(op["path"]), exist_ok=True)
                entry["status"] = "would-apply" if dry_run else "applied"
            elif kind == "delete":
                target = Path(op["path"])
                if not dry_run and not _exists(target):
                    # Idempotent re-run: already sent to the Recycle Bin.
                    entry["status"] = "already-applied"
                else:
                    if not dry_run:
                        _apply_delete(target)
                    entry["status"] = "would-apply" if dry_run else "applied"
                    entry["recoverable"] = "recycle-bin"
            elif kind in ("rename", "move"):
                src, dst = Path(op["src"]), Path(op["dst"])
                if not dry_run and _already_applied(src, dst):
                    # Idempotent re-run of an interrupted plan: skip, don't fail.
                    entry["status"] = "already-applied"
                else:
                    if not dry_run:
                        _apply_rename_or_move(src, dst)
                    entry["status"] = "would-apply" if dry_run else "applied"
            else:
                raise ValueError(f"unknown op: {kind}")
        except ScopeViolation as e:  # rejected, never applied
            entry["status"] = "rejected-out-of-scope"
            entry["error"] = str(e)
        except Exception as e:  # per-op isolation: record and continue
            entry["status"] = "failed"
            entry["error"] = f"{type(e).__name__}: {e}"
            # WinError 32/5 on an in-scope path means something else holds
            # the file open — the single most common real-world failure,
            # and worth naming so it isn't read as a scope or logic problem.
            if getattr(e, "winerror", None) in (5, 32):
                entry["hint"] = ("file is locked or access-denied — it is open in "
                                 "another application, or syncing")
        journal["operations"].append(entry)
        _flush()

    journal["finished"] = datetime.now(timezone.utc).isoformat()
    journal["applied"] = sum(1 for e in journal["operations"] if e["status"] == "applied")
    journal["already_applied"] = sum(1 for e in journal["operations"]
                                     if e["status"] == "already-applied")
    journal["failed"] = sum(1 for e in journal["operations"]
                            if e["status"] in ("failed", "rejected-out-of-scope"))
    _flush()
    return journal


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan_json")
    ap.add_argument("--scope", default=None,
                    help="directory the approval covered; ops outside it are rejected")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    ops = json.loads(Path(args.plan_json).read_text(encoding="utf-8"))
    result = execute_plan(ops, dry_run=args.dry_run, scope=args.scope)
    print(json.dumps(result, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
