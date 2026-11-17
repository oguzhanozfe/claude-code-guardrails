#!/usr/bin/env python3
"""claude-code-guardrails command line.

  hook.py hook pre|post|stop     run as a Claude Code hook (reads the event JSON on stdin)
  hook.py check '<command>'      test a Bash command against the guards
  hook.py summary [--session ID] [--last N] [--json]
  hook.py install|uninstall [--settings PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from guardrails import config, guards, install, meter  # noqa: E402


def run_hook(mode: str) -> int:
    cfg = None
    try:
        cfg = config.load()
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        if mode == "pre":
            reason = guards.check(event, cfg)
            if reason:
                guard = "/".join(re.findall(r"\[(\w+)\]", reason))
                meter.record(cfg, event, "blocked", guard=guard)
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }}))
        else:
            meter.record(cfg, event, meter.kind_for(event))
        return 0
    except Exception as e:  # noqa: BLE001 - a hook must never crash with a traceback
        print(f"guardrails: internal error, {type(e).__name__}: {e}", file=sys.stderr)
        # Exit 2 blocks the tool call (fail closed); any other non-zero code lets it proceed.
        return 2 if mode == "pre" and (cfg or config.DEFAULTS).get("fail_closed") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hook.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("mode", choices=["pre", "post", "stop"])
    h.add_argument("--tag", help="marker the installer uses to find its own entries")
    c = sub.add_parser("check")
    c.add_argument("command")
    s = sub.add_parser("summary")
    s.add_argument("--session")
    s.add_argument("--last", type=int, default=5)
    s.add_argument("--json", action="store_true")
    for name in ("install", "uninstall"):
        i = sub.add_parser(name)
        i.add_argument("--settings", type=Path, default=Path("~/.claude/settings.json").expanduser())
        i.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    if a.cmd == "hook":
        return run_hook(a.mode)
    if a.cmd == "check":
        reason = guards.check({"tool_name": "Bash", "tool_input": {"command": a.command}}, config.load())
        print(reason or "allowed")
        return 1 if reason else 0
    if a.cmd == "summary":
        rows = meter.summarize(meter.log_path(config.load()), a.session, a.last)
        print(json.dumps(rows, indent=2) if a.json else meter.format_summary(rows))
        return 0
    return install.apply(a.settings, uninstall=a.cmd == "uninstall", dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
