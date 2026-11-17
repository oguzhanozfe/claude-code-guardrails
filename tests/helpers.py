"""Imported first by every test module: point HOME and all guardrails paths at a temp dir.

Nothing in the test suite may read or write the real ~/.claude.
"""
import os
import sys
import tempfile
from pathlib import Path

REAL_HOME = os.path.expanduser("~")
FAKE_HOME = tempfile.mkdtemp(prefix="guardrails-home-")
os.environ["HOME"] = FAKE_HOME
os.environ["GUARDRAILS_CONFIG"] = os.path.join(FAKE_HOME, ".claude", "guardrails.json")
os.environ["GUARDRAILS_METER_LOG"] = os.path.join(FAKE_HOME, ".claude", "guardrails", "meter.jsonl")
assert os.path.expanduser("~") == FAKE_HOME != REAL_HOME

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def fake(*parts: str) -> str:
    """Join pieces at runtime so no credential-shaped literal sits in the repo
    (keeps GitHub push protection and secret scanners quiet)."""
    return "".join(parts)


# Credential-shaped test values, assembled at runtime. None of them is real.
GH_TOKEN = fake("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
AWS_KEY = fake("AK", "IA", "Z7Q4M2X9W8V6T5R3")
OPENAI_KEY = fake("sk-", "proj-", "Xy7Qa9Lm2Np4Rs6Tu8Vw0Yz3Bc5De7Fg9Hj")
ANTHROPIC_KEY = fake("sk-", "ant-", "api03-", "Q8w7E6r5T4y3U2i1O0pAsDfGhJkLzXcV")
STRIPE_KEY = fake("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc8xYz")
SLACK_TOKEN = fake("xo", "xb-", "1234567890-0987654321-AbCdEfGhIjKlMnOp")
GOOGLE_KEY = fake("AI", "za", "SyD3x4mPl3K3yF0rT3st1ngOnLy00000000")
JWT = fake("ey", "JhbGciOiJIUzI1NiJ9.", "eyJzdWIiOiIxMjM0NTY3ODkwIn0.", "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U")
PRIVATE_KEY = fake("-----BEGIN ", "RSA PRIVATE KEY-----\nMIIEow...\n")
