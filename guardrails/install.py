"""Merge the guardrails hooks into a Claude Code settings.json, or take them out again.

Our handlers are recognised by the TAG argument, so foreign hooks are never touched.
"""
from __future__ import annotations

import difflib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from . import config

TAG = "claude-code-guardrails"
HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hook.py"
PRE_MATCHER = "Bash|Read|Write|Edit|MultiEdit|NotebookEdit"


def _handler(mode: str, timeout: int) -> dict:
    # Exec form (args present): no shell, so paths with spaces need no quoting.
    return {"type": "command", "command": sys.executable,
            "args": [str(HOOK_SCRIPT), "hook", mode, "--tag", TAG], "timeout": timeout}


def our_hooks() -> dict:
    return {
        "PreToolUse": [{"matcher": PRE_MATCHER, "hooks": [_handler("pre", 10)]}],
        "PostToolUse": [{"matcher": "*", "hooks": [_handler("post", 5)]}],
        "PostToolUseFailure": [{"matcher": "*", "hooks": [_handler("post", 5)]}],
        "Stop": [{"hooks": [_handler("stop", 5)]}],
    }


def _ours(handler: dict) -> bool:
    return TAG in (handler.get("args") or []) or TAG in str(handler.get("command", ""))


def strip(settings: dict) -> dict:
    """Copy of settings with every guardrails handler removed and emptied containers dropped."""
    out = json.loads(json.dumps(settings))
    hooks = out.get("hooks")
    if not isinstance(hooks, dict):
        return out
    removed_any = False
    for event in list(hooks):
        if not isinstance(hooks[event], list):
            continue
        groups, removed = [], False
        for group in hooks[event]:
            handlers = group.get("hooks", []) if isinstance(group, dict) else []
            kept = [h for h in handlers if not _ours(h)]
            removed |= len(kept) != len(handlers)
            if kept or not handlers:
                groups.append({**group, "hooks": kept} if handlers else group)
        if removed:
            removed_any = True
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
    if removed_any and not hooks:
        del out["hooks"]
    return out


def merged(settings: dict) -> dict:
    out = strip(settings)
    hooks = out.setdefault("hooks", {})
    for event, groups in our_hooks().items():
        hooks.setdefault(event, []).extend(groups)
    return out


def _dump(d: dict) -> str:
    return json.dumps(d, indent=2) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    target = path.resolve()  # follow a symlinked settings.json instead of replacing the link
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".settings-", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    if target.exists():
        os.chmod(tmp, target.stat().st_mode & 0o777)
    os.replace(tmp, target)


def apply(path: Path, uninstall: bool = False, dry_run: bool = False, out=sys.stdout) -> int:
    old_text = path.read_text() if path.exists() else ""
    try:
        old = json.loads(old_text) if old_text.strip() else {}
    except ValueError as e:
        print(f"{path} is not valid JSON ({e}); nothing changed.", file=sys.stderr)
        return 1
    if not isinstance(old, dict):
        print(f"{path} does not hold a JSON object; nothing changed.", file=sys.stderr)
        return 1
    new = strip(old) if uninstall else merged(old)
    if new == old:
        print(f"{path}: already {'clean' if uninstall else 'installed'}, nothing to do.", file=out)
        return 0
    new_text = _dump(new)
    if dry_run:
        out.writelines(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                            str(path), f"{path} (after)"))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        while backup.exists():  # never overwrite an earlier backup
            n += 1
            backup = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        backup.write_text(old_text)
        print(f"backup: {backup}", file=out)
    _write_atomic(path, new_text)
    print(f"{'removed guardrails hooks from' if uninstall else 'installed guardrails hooks into'} {path}", file=out)
    cfg_path = config.config_path()
    if not uninstall and not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(_dump(config.DEFAULTS))
        print(f"wrote default config: {cfg_path}", file=out)
    return 0
