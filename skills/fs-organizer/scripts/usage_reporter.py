"""usage_reporter — Step 9 (Report) of the fs-organizer workflow.

Reports exact token usage and API-equivalent cost for a run by parsing the
Claude Code session transcripts (JSONL), recursively — a subagent's
transcript doesn't always live as inline sidechain turns in the parent
session's own .jsonl; some environments write it to a nested
<session-id>/subagents/agent-<id>.jsonl instead. Every assistant message
carries a `usage` block and a model ID in the transcript, so no estimation
is involved, but a subagent's cost is only actually counted if its file is
found — hence the recursive scan.

The per-model breakdown and the full price table below are kept so the
report stays accurate for whatever transcript it is pointed at.

Pricing note: figures use public API list prices (cached 2026-07-19).
Subscription plans (Pro/Max) don't bill per token — the cost shown is the
API-equivalent value of the tokens consumed. Update PRICES when list prices
change, or pass --prices with a JSON override.

Usage as module:   from usage_reporter import summarize_usage
Usage as CLI:      python usage_reporter.py --since 2026-07-19T10:00:00+00:00
                   python usage_reporter.py --project-dir <dir> --since <iso>
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# USD per 1M tokens: (input, output). Cache write = 1.25x input (5m TTL),
# 2x (1h TTL); cache read = 0.1x input. Matched by substring, first hit wins —
# order matters (e.g. "sonnet-5" must precede plain "sonnet").
PRICES: list[tuple[str, float, float]] = [
    ("fable", 10.00, 50.00),
    ("mythos", 10.00, 50.00),
    ("opus", 5.00, 25.00),
    ("sonnet-5", 3.00, 15.00),
    ("sonnet", 3.00, 15.00),
    ("haiku", 1.00, 5.00),
]


def _price_for(model: str) -> tuple[float, float]:
    m = model.lower()
    for key, pin, pout in PRICES:
        if key in m:
            return pin, pout
    return 3.00, 15.00  # unknown model: assume Sonnet-tier, flagged in output


def _project_dir_for_cwd(cwd: str | Path) -> Path:
    """Claude Code stores transcripts under ~/.claude/projects/<sanitized-cwd>/.

    Dots are replaced too, not just separators: a cwd inside `.claude`
    becomes `--claude`, and missing that produced a directory name that
    simply does not exist - so the report came back empty with no error,
    which reads exactly like "this run cost nothing".
    """
    sanitized = re.sub(r"[:\\/.]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / sanitized


def _iter_assistant_entries(project_dir: Path, since: datetime,
                            session_id: str | None = None):
    # Recursive: subagent transcripts don't always live as inline sidechain
    # entries in the parent session's own .jsonl — this environment writes
    # them to a nested <session-id>/subagents/agent-<id>.jsonl instead. A
    # non-recursive glob silently misses them (empty per_model, no error),
    # which understates cost precisely where it matters most: a subagent
    # run is usually the priciest part of a run that spawns one.
    # A transcript can contain the SAME assistant message several times -
    # 53 records for 28 real messages was measured on one run - each copy
    # carrying identical usage. Counting every record double-counts the
    # whole run (that run's output tokens summed to 22,173 against the
    # 10,982 the API actually reported). Dedupe on message id, which is
    # stable across the copies; fall back to the record uuid when a
    # transcript has no message id.
    seen_messages: set[str] = set()

    for jsonl in project_dir.rglob("*.jsonl"):
        if session_id and session_id not in jsonl.parts[-1] and session_id not in str(jsonl.parent):
            # Scoped to one run: Claude Code names a transcript after its
            # session id, and a subagent's file sits under that session's
            # own folder. Without this filter every OTHER Claude session
            # active in the same working directory during the run gets
            # counted too, which silently inflates the figure - the cost
            # of a one-file watcher run once came back ~25% high because a
            # separate interactive session was running alongside it.
            continue
        if datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc) < since:
            continue  # file untouched since run start — nothing relevant inside
        with jsonl.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                ts = obj.get("timestamp")
                if ts:
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if when < since:
                            continue
                    except ValueError:
                        pass
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                key = msg.get("id") or obj.get("uuid")
                if key is not None:
                    if key in seen_messages:
                        continue  # duplicate record of a message already counted
                    seen_messages.add(key)
                yield msg.get("model", "unknown"), bool(obj.get("isSidechain")), usage


def summarize_usage(since: datetime, project_dir: str | Path | None = None,
                    cwd: str | Path | None = None,
                    session_id: str | None = None) -> dict:
    """Sum tokens per model since *since*; price at API list rates.

    Sidechain (subagent) turns are included and also broken out separately so
    a subagent's share of the run is visible.

    Pass *session_id* to scope the figure to one run. Without it every
    Claude session active in the same working directory during the window
    is counted, so the result is an upper bound rather than this run's
    cost.
    """
    pdir = Path(project_dir) if project_dir else _project_dir_for_cwd(cwd or Path.cwd())
    per_model: dict[str, dict] = {}

    for model, sidechain, usage in _iter_assistant_entries(pdir, since, session_id):
        bucket = per_model.setdefault(model, {
            "input_tokens": 0, "output_tokens": 0,
            "cache_write_5m_tokens": 0, "cache_write_1h_tokens": 0,
            "cache_read_tokens": 0, "messages": 0, "subagent_messages": 0,
        })
        bucket["messages"] += 1
        if sidechain:
            bucket["subagent_messages"] += 1
        bucket["input_tokens"] += usage.get("input_tokens", 0) or 0
        bucket["output_tokens"] += usage.get("output_tokens", 0) or 0
        bucket["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0) or 0
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):  # newer transcripts break out TTLs
            bucket["cache_write_5m_tokens"] += cc.get("ephemeral_5m_input_tokens", 0) or 0
            bucket["cache_write_1h_tokens"] += cc.get("ephemeral_1h_input_tokens", 0) or 0
        else:
            bucket["cache_write_5m_tokens"] += usage.get("cache_creation_input_tokens", 0) or 0

    total_cost = 0.0
    for model, b in per_model.items():
        pin, pout = _price_for(model)
        cost = (
            b["input_tokens"] / 1e6 * pin
            + b["output_tokens"] / 1e6 * pout
            + b["cache_write_5m_tokens"] / 1e6 * pin * 1.25
            + b["cache_write_1h_tokens"] / 1e6 * pin * 2.0
            + b["cache_read_tokens"] / 1e6 * pin * 0.1
        )
        b["cost_usd"] = round(cost, 4)
        b["total_tokens"] = (b["input_tokens"] + b["output_tokens"]
                             + b["cache_write_5m_tokens"] + b["cache_write_1h_tokens"]
                             + b["cache_read_tokens"])
        total_cost += cost

    return {
        "since": since.isoformat(),
        "project_dir": str(pdir),
        "session_id": session_id,
        "scope_note": ("scoped to this session" if session_id else
                       "NOT scoped to a session - includes any other Claude "
                       "session active in this directory during the window"),
        "per_model": per_model,
        "total_cost_usd": round(total_cost, 4),
        "note": "API-list-equivalent cost; subscription plans do not bill per token.",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True, help="ISO timestamp of run start")
    ap.add_argument("--project-dir", default=None,
                    help="transcript dir; default derived from --cwd")
    ap.add_argument("--cwd", default=None, help="workspace dir to derive transcript dir from")
    ap.add_argument("--session-id", default=None,
                    help="only count this session's transcript; without it, concurrent "
                         "sessions in the same directory inflate the total")
    args = ap.parse_args(argv)
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    print(json.dumps(summarize_usage(since, project_dir=args.project_dir, cwd=args.cwd,
                                     session_id=args.session_id), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
