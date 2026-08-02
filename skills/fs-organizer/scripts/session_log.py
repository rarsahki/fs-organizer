"""session_log — appends one entry to a scope's session log.

The session log is a run's only record (see SKILL.md), so an entry is
written the moment each step finishes. Doing that with a file-write per
step costs one tool call per step; chaining this script onto the step's
own command instead costs none:

    python scripts/<step>.py ... && python scripts/session_log.py ...

That is the whole reason this exists. It also stamps its own UTC time, so
no separate "what time is it" call is needed either — a naive local
timestamp silently shifts the cutoff the closing usage report measures
from, and asking the shell for the time was costing a round trip per
entry.

Log path: <state-root>/<scope-name>/session-file-<YYYY-MM-DD>, one file
per scope per date, appended to by every run that day, in both modes.

Usage as module:   from session_log import append_entry
Usage as CLI:
  python session_log.py --scope <watched-dir> --mode watcher \
      --step "2-fingerprint" --command "<verbatim command>" \
      --input "<what it consumed>" --output "<what it produced>"
  python session_log.py --scope <dir> --mode organize --step run-start \
      --note "scope: ..., new_files: ..."
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from fsorg_common import STATE_ROOT, scope_state_dir  # noqa: F401


def log_path(scope: str | Path, when: datetime | None = None) -> Path:
    """Where this scope's log for *when*'s date lives (created if needed).

    Uses scope_state_dir rather than a folder named after the scope's leaf:
    two unrelated directories sharing a leaf name would otherwise append to
    one another's log, interleaving runs that have nothing to do with each
    other.
    """
    when = when or datetime.now(timezone.utc)
    return scope_state_dir(scope) / f"session-file-{when:%Y-%m-%d}"


def append_entry(scope: str | Path, mode: str, step: str,
                 command: str | None = None, input_: str | None = None,
                 output: str | None = None, note: str | None = None) -> Path:
    """Append one entry. Returns the log file written to.

    The timestamp is generated here, in UTC with an explicit offset —
    callers never pass one in, so a naive local time cannot leak into the
    log.
    """
    now = datetime.now(timezone.utc)
    path = log_path(scope, now)

    lines = ["---",
             f"timestamp: {now.isoformat(timespec='seconds')}",
             f"mode: {mode}",
             f"step: {step}"]
    for label, value in (("command", command), ("input", input_),
                         ("output", output), ("note", note)):
        if value:
            lines.append(f"{label}: {value}")

    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", required=True, help="the run's scope directory")
    ap.add_argument("--mode", required=True, choices=["organize", "watcher"])
    ap.add_argument("--step", required=True,
                    help="step number and name, e.g. '3-exact-duplicates'")
    ap.add_argument("--command", default=None, help="the verbatim command this step ran")
    ap.add_argument("--input", dest="input_", default=None)
    ap.add_argument("--output", default=None)
    ap.add_argument("--note", default=None)
    args = ap.parse_args(argv)

    path = append_entry(args.scope, args.mode, args.step, args.command,
                        args.input_, args.output, args.note)
    print(f"logged {args.step} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
