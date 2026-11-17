#!/usr/bin/env python3
"""Measure false positives on two benign corpora and write results/fp_report.json.

1. tests/corpus/benign_commands.txt: everyday dev shell commands, run through every guard as Bash calls.
2. The running Python's standard library .py files, run through the secret guard as Write content
   (real code nobody wrote for this project; numbers depend on the Python version, which is recorded).
"""
import json
import os
import platform
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HOME"] = tempfile.mkdtemp()  # defaults only; never read the real user config
os.environ["GUARDRAILS_CONFIG"] = os.path.join(os.environ["HOME"], "none.json")

from guardrails import config, guards  # noqa: E402

cfg = config.load()
commands = [line for line in (ROOT / "tests/corpus/benign_commands.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")]
cmd_hits = []
for c in commands:
    reason = guards.check({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": "/work/app"}, cfg,
                          branch_of=lambda _: "feature/x")
    if reason:
        cmd_hits.append({"command": c, "reason": reason})

stdlib = Path(sysconfig.get_paths()["stdlib"])
files = sorted(p for p in stdlib.rglob("*.py") if "site-packages" not in p.parts)
file_hits = []
for p in files:
    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    name = guards.find_secret(text, cfg["secret"])
    if name:
        file_hits.append({"file": str(p.relative_to(stdlib)), "matched": name})

report = {
    "python": platform.python_version(),
    "benign_commands": {"total": len(commands), "flagged": len(cmd_hits),
                        "fp_rate": round(len(cmd_hits) / len(commands), 4), "flagged_items": cmd_hits},
    "stdlib_files_secret_guard": {"total": len(files), "flagged": len(file_hits),
                                  "fp_rate": round(len(file_hits) / len(files), 4), "flagged_items": file_hits},
}
out = ROOT / "results" / "fp_report.json"
out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps(report, indent=2) + "\n")
for key in ("benign_commands", "stdlib_files_secret_guard"):
    r = report[key]
    print(f"{key}: {r['flagged']}/{r['total']} flagged ({r['fp_rate']:.2%})")
    for item in r["flagged_items"]:
        print("   ", item)
