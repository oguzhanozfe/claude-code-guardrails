"""Per-session tool-call meter: one JSONL record per tool call, block, and stop.

Records hold the tool name, outcome and timing only, never tool input or output.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path


def log_path(cfg: dict) -> Path:
    return Path(os.environ.get("GUARDRAILS_METER_LOG") or cfg["meter"]["log_path"]).expanduser()


def record(cfg: dict, event: dict, kind: str, **extra) -> None:
    if not cfg["meter"]["enabled"]:
        return
    rec = {"ts": round(time.time(), 3), "session": event.get("session_id", ""), "kind": kind}
    if event.get("tool_name"):
        rec["tool"] = event["tool_name"]
    if isinstance(event.get("duration_ms"), (int, float)):
        rec["ms"] = event["duration_ms"]
    rec.update(extra)
    path = log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One write() on an O_APPEND fd: parallel PostToolUse hooks don't interleave lines.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec, separators=(",", ":")) + "\n").encode())
    finally:
        os.close(fd)


def kind_for(event: dict) -> str:
    return {"PostToolUse": "call", "PostToolUseFailure": "fail", "Stop": "stop"}.get(
        event.get("hook_event_name", ""), "call")


def summarize(path: Path, session: str | None = None, last: int = 5) -> list[dict]:
    sessions: dict[str, dict] = {}
    if not path.exists():
        return []
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        sid = r.get("session", "")
        if session and not sid.startswith(session):
            continue
        s = sessions.setdefault(sid, {"session": sid, "first": r["ts"], "last": r["ts"], "calls": 0,
                                      "failures": 0, "blocked": 0, "stops": 0, "tool_ms": 0,
                                      "tools": defaultdict(lambda: [0, 0])})
        s["last"] = max(s["last"], r["ts"])
        kind = r.get("kind")
        if kind in ("call", "fail"):
            s["calls"] += 1
            s["failures"] += kind == "fail"
            s["tool_ms"] += r.get("ms", 0)
            s["tools"][r.get("tool", "?")][0] += 1
            s["tools"][r.get("tool", "?")][1] += r.get("ms", 0)
        elif kind == "blocked":
            s["blocked"] += 1
        elif kind == "stop":
            s["stops"] += 1
    rows = sorted(sessions.values(), key=lambda s: s["last"])[-last:]
    for s in rows:
        s["wall_s"] = round(s["last"] - s["first"], 1)
        s["tools"] = {k: {"calls": v[0], "ms": v[1]} for k, v in sorted(s["tools"].items(), key=lambda kv: -kv[1][0])}
    return rows


def format_summary(rows: list[dict]) -> str:
    if not rows:
        return "No meter records yet."
    out = []
    for s in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["first"]))
        out.append(f"session {s['session'][:8] or '?'}  started {when}  wall {s['wall_s']}s  "
                   f"calls {s['calls']}  failed {s['failures']}  blocked {s['blocked']}  "
                   f"tool time {s['tool_ms'] / 1000:.1f}s  turns {s['stops']}")
        for tool, t in s["tools"].items():
            out.append(f"    {tool:<24} {t['calls']:>5} calls  {t['ms'] / 1000:>8.1f}s")
    return "\n".join(out)
