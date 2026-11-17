"""Hook stdin/stdout contract, meter, and installer, all against a temp HOME."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers as h
from guardrails import config, install, meter

HOOK = str(h.ROOT / "hook.py")


def run_hook(mode, payload, env=None):
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, HOOK, "hook", mode, "--tag", install.TAG], input=stdin,
                          capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=30)


def pre(command, **extra):
    return {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}, "cwd": "/tmp", **extra}


class HookContract(unittest.TestCase):
    def setUp(self):
        self.log = Path(h.FAKE_HOME, f"meter-{self._testMethodName}.jsonl")
        self.env = {"GUARDRAILS_METER_LOG": str(self.log)}

    def test_deny_is_json_on_stdout_with_exit_0(self):
        r = run_hook("pre", pre("rm -rf ~"), self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("[rm_broad]", out["permissionDecisionReason"])
        rec = json.loads(self.log.read_text())
        self.assertEqual((rec["kind"], rec["guard"], rec["tool"]), ("blocked", "destructive/rm_broad", "Bash"))

    def test_pass_through_prints_nothing(self):
        r = run_hook("pre", pre("ls -la"), self.env)
        self.assertEqual((r.returncode, r.stdout), (0, ""))  # silence, never "allow"

    def test_malformed_input_fails_open_by_default(self):
        r = run_hook("pre", "{not json", self.env)
        self.assertEqual((r.returncode, r.stdout), (1, ""))  # non-blocking error
        self.assertIn("internal error", r.stderr)

    def test_fail_closed_blocks_on_internal_error(self):
        cfg = Path(h.FAKE_HOME, "closed.json")
        cfg.write_text('{"fail_closed": true}')
        r = run_hook("pre", "[]", {**self.env, "GUARDRAILS_CONFIG": str(cfg)})
        self.assertEqual(r.returncode, 2)

    def test_post_and_stop_are_metered(self):
        run_hook("post", {"session_id": "s9", "hook_event_name": "PostToolUse", "tool_name": "Bash", "duration_ms": 120,
                          "tool_input": {"command": "secret stuff"}, "tool_response": {"stdout": "x"}}, self.env)
        run_hook("post", {"session_id": "s9", "hook_event_name": "PostToolUseFailure", "tool_name": "Read",
                          "duration_ms": 5, "error": "nope"}, self.env)
        r = run_hook("stop", {"session_id": "s9", "hook_event_name": "Stop"}, self.env)
        self.assertEqual(r.returncode, 0)
        text = self.log.read_text()
        self.assertNotIn("secret stuff", text)  # tool input is never logged
        [s] = meter.summarize(self.log)
        self.assertEqual((s["calls"], s["failures"], s["stops"], s["tool_ms"]), (2, 1, 1, 125))
        self.assertEqual(s["tools"]["Bash"], {"calls": 1, "ms": 120})
        summary = subprocess.run([sys.executable, HOOK, "summary"], capture_output=True, text=True,
                                 env={**os.environ, **self.env})
        self.assertIn("calls 2", summary.stdout)


class Demo(unittest.TestCase):
    def test_demo_runs_keyless_in_temp_home(self):
        from guardrails import demo
        rows, summary = demo.run()
        self.assertEqual(sum(r["blocked"] for r in rows), 10)
        self.assertEqual(len(rows), 18)
        self.assertIn("blocked 10", summary)
        self.assertNotIn(demo.FAKE_TOKEN, demo.as_text(rows, summary) + demo.as_html(rows, summary) + demo.as_svg(rows))
        self.assertFalse(Path(h.REAL_HOME, ".claude", "guardrails", "meter.jsonl").exists()
                         and "demo-0001" in Path(h.REAL_HOME, ".claude", "guardrails", "meter.jsonl").read_text())


class Installer(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=h.FAKE_HOME))
        self.settings = self.dir / "settings.json"
        self.original = {
            "model": "opus", "unknownKey": {"keep": [1, 2]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}],
                      "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        }
        self.settings.write_text(json.dumps(self.original, indent=4))

    def apply(self, **kw):
        return install.apply(self.settings, out=io.StringIO(), **kw)

    def backups(self):
        return sorted(self.dir.glob("settings.json.bak-*"))

    def test_never_touches_real_home(self):
        self.assertTrue(str(config.config_path()).startswith(h.FAKE_HOME))
        self.assertTrue(str(meter.log_path(config.load())).startswith(h.FAKE_HOME))

    def test_install_preserves_foreign_hooks_and_keys(self):
        self.assertEqual(self.apply(), 0)
        s = json.loads(self.settings.read_text())
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["unknownKey"], {"keep": [1, 2]})
        self.assertEqual(s["hooks"]["PreToolUse"][0], self.original["hooks"]["PreToolUse"][0])
        self.assertEqual(s["hooks"]["SessionStart"], self.original["hooks"]["SessionStart"])
        ours = s["hooks"]["PreToolUse"][1]["hooks"][0]
        self.assertEqual(ours["command"], sys.executable)
        self.assertEqual(ours["args"][-2:], ["--tag", install.TAG])
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(config.config_path().exists())

    def test_idempotent(self):
        self.apply()
        first = self.settings.read_bytes()
        out = io.StringIO()
        install.apply(self.settings, out=out)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertIn("already installed", out.getvalue())
        self.assertEqual(len(self.backups()), 1)

    def test_dry_run_prints_diff_and_writes_nothing(self):
        before = self.settings.read_bytes()
        out = io.StringIO()
        install.apply(self.settings, dry_run=True, out=out)
        self.assertIn("+", out.getvalue())
        self.assertIn(install.TAG, out.getvalue())
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_uninstall_restores_structure(self):
        self.apply()
        self.apply(uninstall=True)
        self.assertEqual(json.loads(self.settings.read_text()), self.original)
        self.assertEqual(len(self.backups()), 2)  # one per write, none overwritten

    def test_uninstall_from_empty_settings_removes_hooks_key(self):
        self.settings.write_text("{}")
        self.apply()
        self.apply(uninstall=True)
        self.assertEqual(json.loads(self.settings.read_text()), {})

    def test_missing_settings_file(self):
        self.settings.unlink()
        self.assertEqual(self.apply(), 0)
        self.assertIn("PreToolUse", json.loads(self.settings.read_text())["hooks"])
        self.assertEqual(self.backups(), [])

    def test_invalid_json_aborts_without_writing(self):
        self.settings.write_text("{oops")
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.settings.read_text(), "{oops")
        self.assertEqual(self.backups(), [])

    def test_symlink_is_followed_not_replaced(self):
        real = self.dir / "real-settings.json"
        real.write_text("{}")
        self.settings.unlink()
        self.settings.symlink_to(real)
        self.apply()
        self.assertTrue(self.settings.is_symlink())
        self.assertIn("PreToolUse", json.loads(real.read_text())["hooks"])

    def test_installed_command_runs_the_hook(self):
        self.apply()
        handler = json.loads(self.settings.read_text())["hooks"]["PreToolUse"][1]["hooks"][0]
        r = subprocess.run([handler["command"], *handler["args"]], input=json.dumps(pre("git reset --hard")),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
