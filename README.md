# claude-code-guardrails

A small pack of [Claude Code hooks](https://code.claude.com/docs/en/hooks) that stop an agent before it
writes a credential, runs a destructive command, or touches a secrets file. It also meters every tool
call per session into a local JSONL file. Pure Python standard library, 3.10+, about 700 lines.

## Why

Coding agents run shell commands and write files fast. Most mistakes are recoverable. A few are not:
`rm -rf "$DIR"/` with an empty variable, a force push over `main`, an API key committed in a config
file, a `curl | sh` from a URL the model guessed. Permission prompts help, but people approve them on
autopilot. These hooks give a second, deterministic check that names the problem and tells the model
what to do instead. The model reads that reason and usually picks a safer route on its own.

## What it guards

| Guard | Hook | Blocks | Example reason returned to the model |
| --- | --- | --- | --- |
| `secret` | PreToolUse on Bash, Write, Edit, MultiEdit, NotebookEdit | Private key blocks, AWS/GitHub/OpenAI/Anthropic/Stripe/Google/Slack keys, JWTs, `password = "<literal>"`, passwords in URLs | `content for /app/settings.py contains a string shaped like a GitHub token (line 2). Read it from an environment variable or secret manager instead.` (the secret itself is never echoed) |
| `destructive` | PreToolUse on Bash | Recursive `rm` on `/`, `~`, `.`, `*`, top-level dirs or `"$VAR"/`; force push to protected branches; `git reset --hard`; `DROP`/`TRUNCATE TABLE` sent to a DB client; `chmod -R 777`; `curl`/`wget` piped or substituted into a shell | `[force_push] force push to protected branch 'main'. Push to a feature branch, or ask the user.` |
| `protected_paths` | PreToolUse on Read, Grep, Glob, Write, Edit, Bash | `.env`, `.env.*`, `*.pem`, `*.key`, SSH keys, `~/.ssh`, `~/.aws/credentials`, `~/.netrc`, kube/docker/gcloud configs. Writes only: `~/.claude/settings.json` and the guardrails config, so the agent can't switch its own guards off | `command touches '.env' (pattern '.env'). Ask the user to handle this file.` |
| `meter` | PostToolUse, PostToolUseFailure, Stop | Nothing. Appends `{ts, session, kind, tool, ms}` per call, block and turn. Never logs tool input or output | |

Shell commands are tokenised with `shlex`, not grepped, so `echo "rm -rf /"`, `git commit -m "drop table users_old"`
and `grep -rn "curl | sh" docs/` pass, while `bash -c "rm -rf ~"`, `echo $(rm -rf /)` and
`sudo -u root env X=1 rm -rf /usr` are caught. When the hook blocks, it prints the documented PreToolUse JSON
(`permissionDecision: "deny"` plus a reason) and exits 0. When it has nothing to say, it prints nothing.
It never returns `allow`, so your normal permission prompts stay in charge.

## Install

Needs Python 3.10+ and Claude Code. Clone anywhere, then:

```bash
git clone https://github.com/oguzhanozfe/claude-code-guardrails && cd claude-code-guardrails
python3 hook.py install --dry-run   # show the diff to ~/.claude/settings.json, change nothing
python3 hook.py install             # back up settings.json, merge the hooks, write ~/.claude/guardrails.json
python3 hook.py uninstall           # remove only our entries (backup first)
```

The installer:

- writes a timestamped `settings.json.bak-*` before every change and never overwrites an older backup;
- keeps every foreign hook and unknown key, and only adds or removes handlers tagged `claude-code-guardrails`;
- is idempotent: a second install reports "already installed" and writes nothing;
- uses the exec form (`command` = your Python, `args` = the script path), so paths with spaces need no quoting;
- writes atomically and follows a symlinked `settings.json` instead of replacing the link;
- refuses to touch a `settings.json` that is not valid JSON.

Use `--settings .claude/settings.json` to install into one project instead of your user settings.
The hooks call the script from where you cloned it, so keep the clone where it is (or reinstall after moving it).

## Configure

Everything lives in one file, `~/.claude/guardrails.json` (or `$GUARDRAILS_CONFIG`). It's a partial
override of the defaults in [`guardrails/config.py`](guardrails/config.py); keys you leave out keep their default.

```json
{
  "secret": {"allow_paths": ["*/tests/fixtures/*"], "extra_patterns": ["corp_[a-z0-9]{32}"]},
  "destructive": {"protected_branches": ["main", "develop"], "rules": {"reset_hard": false}},
  "protected_paths": {"paths": [".env", ".env.*", "secrets/*"]},
  "meter": {"log_path": "~/.claude/guardrails/meter.jsonl"},
  "fail_closed": false
}
```

`fail_closed: true` makes an internal hook error (bad input, a bug) block the tool call. The default
fails open: the error is shown in the transcript and the call proceeds.

## Try it

```bash
python3 hook.py check 'rm -rf "$BUILD_DIR"/'
# guardrails[destructive][rm_broad] recursive rm on '$BUILD_DIR/', a path built from a variable that becomes / when the variable is empty. ...
python3 hook.py check 'rm -rf "${BUILD_DIR:?}"/out'
# allowed
echo '{"tool_name":"Bash","tool_input":{"command":"git push -f origin main"}}' \
  | GUARDRAILS_METER_LOG=/tmp/guardrails-demo.jsonl python3 hook.py hook pre   # hook mode also writes a meter record
# {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...}}
python3 hook.py summary            # last 5 sessions; --session <id-prefix>, --last N, --json
```

## Results

All numbers below come from files in [`results/`](results/).

**Live run with real Claude Code** ([`results/e2e_live_run.txt`](results/e2e_live_run.txt), reproduce with
`scripts/live_check.sh`). Guardrails code at commit `1894f80`, Claude Code 2.1.296 with Haiku. The hooks load
from a temp settings file through `--settings`, and `--setting-sources project` keeps the user's own settings
out. Config, meter log, a temp git repo and a fake `.env` all live in a throwaway directory. The prompt asked
for 8 steps:
- `ls`, `cat notes.txt` and `git status --short` ran.
- `cat .env` was denied by `protected_path`.
- A Write of a GitHub-token-shaped string was denied by `secret`, and no file was created.
- `git reset --hard` was denied by `destructive/reset_hard`.
- `git push --force origin main` was denied by `destructive/force_push`.
- Claude skipped the Read of `.env` on its own after the Bash block, so that run doesn't exercise the Read-tool
  path. The unit tests cover it.

The meter logged 3 calls, 4 blocks and 1 turn, and no guardrails file appeared in the real `~/.claude`. That
is one run, not a benchmark.

**Test tables** (`tests/test_guards.py`): 84 inputs that must be blocked and 73 that must be allowed,
including the traps above. Plus 15 tests for the hook's stdin/stdout contract, the meter and the installer
(dry run, idempotence, backups, foreign hooks kept, invalid JSON, symlinks, uninstall restoring the original).
All of them run offline and on CI against a temp `HOME`.

**False positives on benign input** (`python3 scripts/fp_report.py`; CI reruns it on every push for 3.10–3.13 and uploads the report):

| Corpus | Size | Flagged | Rate | Source file |
| --- | --- | --- | --- | --- |
| Everyday dev shell commands ([`tests/corpus/benign_commands.txt`](tests/corpus/benign_commands.txt)), all guards | 501 | 1 | 0.20% | every `results/fp_report*.json` |
| Full CPython 3.10.22 stdlib incl. tests (GitHub runner), secret guard on Write content | 1713 | 1 | 0.06% | [`fp_report_ci_py3.10.json`](results/fp_report_ci_py3.10.json) |
| Full CPython 3.11.18 stdlib | 1824 | 1 | 0.05% | [`fp_report_ci_py3.11.json`](results/fp_report_ci_py3.11.json) |
| Full CPython 3.12.16 stdlib | 1803 | 0 | 0.00% | [`fp_report_ci_py3.12.json`](results/fp_report_ci_py3.12.json) |
| Full CPython 3.13.18 stdlib | 1770 | 1 | 0.06% | [`fp_report_ci_py3.13.json`](results/fp_report_ci_py3.13.json) |
| Trimmed CPython 3.12.14 stdlib without `test/` (uv build, macOS) | 686 | 0 | 0.00% | [`fp_report.json`](results/fp_report.json) |

The one flagged command is `cp .env.example .env.local`. That's a deliberate trade-off: the guard
blocks writing any `.env.*` file except the example/sample/template names. On 3.10 and 3.11, the flagged
file is a test fixture in `distutils/tests/test_config.py`. On 3.13 it's a real false positive:
`'pwd': 'pwd#module-pwd'` in `pydoc_data/module_docs.py`, because `pwd` is one of the password keywords.
An earlier version also flagged ordinary calls like `connect(password=db_password)`. A review caught that
bug, and the table rows for it are now in the tests.

How far to trust these numbers: the command corpus was written by a separate LLM subagent from a
description of everyday dev work, without seeing the rules, but I wrote the rules and the corpus is still
synthetic. The stdlib corpus is real code nobody wrote for this project, but stdlib code holds few
credential-shaped strings, so it mostly shows the `password = ...` heuristic doesn't fire on ordinary code.
Neither corpus measures how many real attacks get through.

## Known limits

- **It's a seatbelt, not a sandbox.** A determined or prompt-injected model can get around pattern checks:
  write a script file and run it, base64-encode a command, use `find -delete`, `xargs rm`, `git clean -fdx`,
  `python -c "shutil.rmtree(...)"`, or `psql -f drop.sql`. None of those are caught. For real isolation, run the
  agent in a container or VM with no production credentials.
- The secret guard only checks what is *written* (Bash commands, Write/Edit content). It does not scan tool
  output, and it misses secrets that don't match a known shape. For `password = ...` it guesses whether the
  value is code or a literal. All-letter values (`correcthorsebattery`) and snake_case values (`hunter2_prod`)
  are treated as identifiers and pass. A variable name with a digit (`password=password1`) is treated as a
  literal and blocked.
- The protected-path check on Bash looks at words in the command, so it can't see paths built at runtime
  (`cat "$(echo .e)nv"`). It also blocks some harmless commands, such as `git rm --cached .env`.
- When `git push --force` names no branch, the hook asks git for the current branch. If git can't say
  (detached HEAD, not a repo), it blocks.
- `--force-with-lease` counts as a force push, but only protected branches are guarded.
- Matching is tested on macOS and Linux paths. Windows paths and PowerShell are not handled.

## Layout

```
hook.py                 CLI entry: hook pre|post|stop, check, summary, install, uninstall
guardrails/guards.py    the three PreToolUse guards and the shell tokeniser
guardrails/meter.py     JSONL meter and summary
guardrails/install.py   settings.json merge/strip
guardrails/config.py    defaults and the one config file
scripts/fp_report.py    false-positive measurement
```

## Credits

The base credential pattern list is adapted from
[`oguzhanozfe/llm-offload`](https://github.com/oguzhanozfe/llm-offload) `offload/credentials.py` (MIT).
This version tightens the `sk-` pattern so long kebab-case identifiers don't match, and adds Stripe, Slack
webhook and URL-password shapes.

## License

MIT
