"""One config file for every guard: defaults here, overrides in guardrails.json."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "fail_closed": False,  # True: an internal hook error blocks the tool call (exit 2)
    "secret": {
        "enabled": True,
        "extra_patterns": [],  # extra regexes treated as credentials
        "allow_values": ["AKIAIOSFODNN7EXAMPLE"],  # AWS's documented example key
        "allow_paths": [],  # globs whose Write/Edit content is not scanned, e.g. "*/fixtures/*"
    },
    "destructive": {
        "enabled": True,
        "protected_branches": ["main", "master", "release/*", "production"],
        "extra_broad_paths": [],
        "rules": {
            "rm_broad": True,
            "force_push": True,
            "reset_hard": True,
            "drop_sql": True,
            "chmod_777": True,
            "pipe_to_shell": True,
        },
    },
    "protected_paths": {
        "enabled": True,
        # Any access (Read, Write, Edit, Bash mention) is blocked.
        "paths": [
            ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "id_ecdsa*",
            "~/.ssh", "~/.ssh/*", "~/.aws/credentials", "~/.config/gcloud/*",
            "~/.netrc", "~/.docker/config.json", "~/.kube/config",
        ],
        # Only writes are blocked; reading is fine.
        "write_paths": ["~/.claude/settings.json", "~/.claude/guardrails.json"],
        "allow": [".env.example", ".env.sample", ".env.template", "*.pub"],
    },
    "meter": {"enabled": True, "log_path": "~/.claude/guardrails/meter.jsonl"},
}


def config_path() -> Path:
    return Path(os.environ.get("GUARDRAILS_CONFIG", "~/.claude/guardrails.json")).expanduser()


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load(path: Path | None = None) -> dict:
    path = path or config_path()
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    return _merge(DEFAULTS, json.loads(path.read_text()))
