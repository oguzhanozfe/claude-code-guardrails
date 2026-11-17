"""PreToolUse guards. Each guard returns a reason string to block, or None to stay silent.

Guards never return "allow": staying silent leaves Claude Code's normal permission flow in charge.
"""
from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import shlex
import subprocess

# --------------------------------------------------------------------------- secret guard
# Base list adapted from oguzhanozfe/llm-offload offload/credentials.py (MIT),
# with the sk- pattern tightened and a few provider formats added.
_SECRET_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), False),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), False),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), False),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), False),
    # Real OpenAI/Anthropic keys are long random base62; requiring mixed case + digit
    # keeps long kebab-case identifiers like sk-some-feature-flag-name from matching.
    ("OpenAI/Anthropic-style key", re.compile(r"\bsk-(?:proj-|ant-|svcacct-)?[A-Za-z0-9_-]{32,}"), True),
    ("Stripe live key", re.compile(r"\b[rs]k_live_[0-9A-Za-z]{20,}"), False),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), False),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), False),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"), False),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), False),
]
_KV = re.compile(
    r"(?i)(?:\b|_)(?:password|passwd|pwd|secret|secret[_-]?key|api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|private[_-]?key)\b['\"]?\s*[:=]\s*['\"]?([^\s'\",;]{8,})"
)
_URL_CRED = re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s:@]{6,})@")
_PLACEHOLDER = re.compile(r"(?i)example|changeme|change_me|placeholder|your|xxx|redacted|dummy|secret_here|\*{3}|<|\$")
_EXPRESSION = re.compile(r"[(\[{]|^[A-Za-z_]+$|^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")


def _mixed(s: str) -> bool:
    return any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s)


def _literal_value(value: str) -> bool:
    """True when a key=value match looks like a real literal, not code or a placeholder."""
    return not (_PLACEHOLDER.search(value) or _EXPRESSION.search(value))


def find_secret(text: str, cfg: dict) -> str | None:
    """Name of the first credential shape found in text, or None. Never returns the secret."""
    if not text:
        return None
    allow = set(cfg.get("allow_values", []))
    for name, pattern, needs_mixed in _SECRET_PATTERNS:
        for m in pattern.finditer(text):
            if m.group(0) in allow or (needs_mixed and not _mixed(m.group(0))):
                continue
            return name
    for name, pattern in (("password/secret assignment", _KV), ("password in URL", _URL_CRED)):
        for m in pattern.finditer(text):
            if m.group(1) not in allow and _literal_value(m.group(1)):
                return name
    for extra in cfg.get("extra_patterns", []):
        if re.search(extra, text):
            return f"custom pattern {extra!r}"
    return None


def _line_of(text: str, cfg: dict) -> str:
    for i, line in enumerate(text.splitlines(), 1):
        if find_secret(line, cfg):
            return f" (line {i})"
    return ""


# --------------------------------------------------------------------------- shell parsing
_SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;"}
_WRAPPERS = {"sudo", "doas", "env", "nohup", "time", "command", "exec", "nice", "timeout", "builtin", "xargs"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes: fall back to a plain split
        return cmd.split()


def segments(cmd: str) -> list[tuple[list[str], str]]:
    """Split a shell command into (argv, following separator) pairs, with wrappers stripped.

    Contents of $( ... ) and backticks, and `sh -c "..."` / `eval "..."` strings, are
    analysed as commands of their own.
    """
    out: list[tuple[list[str], str]] = []
    for inner in re.findall(r"\$\(([^()]*)\)", cmd) + re.findall(r"`([^`]*)`", cmd):
        out += segments(inner)
    cur: list[str] = []
    for tok in _tokens(cmd) + [";"]:
        if tok in _SEPARATORS:
            argv = _strip_wrappers(cur)
            if argv:
                name = posixpath.basename(argv[0])
                if name in _SHELLS and "-c" in argv[1:-1]:
                    out += segments(argv[argv.index("-c") + 1])
                elif name == "eval":
                    out += segments(" ".join(argv[1:]))
                out.append((argv, tok))
            cur = []
        else:
            cur.append(tok)
    return out


def _strip_wrappers(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if re.match(r"^[A-Za-z_]\w*=", tok):  # VAR=value prefix
            i += 1
        elif posixpath.basename(tok) in _WRAPPERS:
            i += 1
            while i < len(argv) and (argv[i].startswith("-") or argv[i].isdigit()):
                i += 1
        else:
            break
    return argv[i:]


# --------------------------------------------------------------------------- destructive guard
_HOME_DIRS = {"Documents", "Desktop", "Downloads", "Library", "Pictures", "Music", "Movies",
              "Projects", "projects", "code", "src", "dev", "work", ".config", ".local"}
_HOME_ALIASES = re.compile(r"^(?:~|\$HOME|\$\{HOME\})(?=/|$)")


def _broad_path(target: str, extra: list[str]) -> str | None:
    if target in extra or os.path.expanduser(target) in [os.path.expanduser(e) for e in extra]:
        return "a configured broad path"
    if re.match(r"^\$\{?\w+\}?/\**$", target) and not _HOME_ALIASES.match(target):
        return "a path built from a variable that becomes / when the variable is empty"
    path = _HOME_ALIASES.sub("~", target)
    norm = posixpath.normpath(path) if path else path
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    while parts and parts[-1] in ("*", ".*"):
        parts.pop()
    if norm.startswith("/"):
        if len(parts) <= 1 or (parts[0] in ("Users", "home") and len(parts) <= 2):
            return "a top-level system or home directory"
    elif norm.startswith("~"):
        rest = parts[1:]
        if not rest or (len(rest) == 1 and rest[0] in _HOME_DIRS):
            return "your home directory or a top-level folder in it"
    elif all(p == ".." for p in parts):
        return "the current or a parent directory"
    return None


def _rm(args: list[str], extra: list[str]) -> str | None:
    recursive, opts_done, targets = False, False, []
    for a in args:
        if not opts_done and a == "--":
            opts_done = True
        elif not opts_done and a.startswith("--"):
            recursive |= a == "--recursive"
            if a == "--no-preserve-root":
                return "rm --no-preserve-root removes / itself"
        elif not opts_done and a.startswith("-") and len(a) > 1:
            recursive |= bool(set(a[1:]) & {"r", "R"})
        else:
            targets.append(a)
    if recursive:
        for t in targets:
            why = _broad_path(t, extra)
            if why:
                return f"recursive rm on {t!r}, {why}"
    return None


def current_branch(cwd: str | None) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or None,
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    branch = out.stdout.strip()
    return branch if out.returncode == 0 and branch and branch != "HEAD" else None


def _force_push(argv: list[str], protected: list[str], cwd: str | None, branch_of) -> str | None:
    if "push" not in argv:
        return None
    args = argv[argv.index("push") + 1:]
    force, positional, skip = False, [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            skip = True
        elif a.startswith("--force") or a in ("--mirror",) or (re.match(r"^-[a-z]+$", a) and "f" in a):
            force = True
        elif not a.startswith("-"):
            positional.append(a)
    refspecs = positional[1:]
    if any(r.startswith("+") for r in refspecs):
        force = True
    if not force:
        return None
    if "--all" in args or "--mirror" in args:
        return "force push of all branches"
    dests = [r.lstrip("+").split(":")[-1].removeprefix("refs/heads/") for r in refspecs]
    dests = [branch_of(cwd) if d in ("HEAD", "") else d for d in dests] or [branch_of(cwd)]
    for d in dests:
        if d is None:
            return "force push where the target branch could not be determined"
        if any(fnmatch.fnmatchcase(d, p) for p in protected):
            return f"force push to protected branch {d!r}"
    return None


_SQL = re.compile(r"(?i)\b(?:drop\s+(?:table|database|schema)|truncate\s+table)\b")
_DB_CLIENT = re.compile(r"\b(?:psql|mysql|mariadb|sqlite3|sqlcmd|duckdb|clickhouse-client|cockroach|mongosh|snowsql)\b")
_PROC_SUB = re.compile(r"\b(?:ba|z|da|k)?sh\s+[^|;&]*<\(\s*(?:curl|wget)\b")
_SUBST_EXEC = re.compile(r"(?:\b(?:ba|z|da|k)?sh\s+-c|\beval)\s+[\"']?\$\(\s*(?:curl|wget)\b")


def check_command(cmd: str, cfg: dict, cwd: str | None = None, branch_of=current_branch) -> str | None:
    rules = cfg["rules"]
    segs = segments(cmd)
    for i, (argv, sep) in enumerate(segs):
        name = posixpath.basename(argv[0])
        if rules["rm_broad"] and name == "rm":
            why = _rm(argv[1:], cfg.get("extra_broad_paths", []))
            if why:
                return f"[rm_broad] {why}. Delete a narrower path, or ask the user to run it."
        if name == "git":
            if rules["force_push"]:
                why = _force_push(argv, cfg["protected_branches"], cwd, branch_of)
                if why:
                    return f"[force_push] {why}. Push to a feature branch, or ask the user."
            if rules["reset_hard"] and "reset" in argv and "--hard" in argv:
                return "[reset_hard] git reset --hard discards uncommitted work. Use git stash, or ask the user."
        if rules["chmod_777"] and name == "chmod":
            recursive = any(a == "--recursive" or re.match(r"^-[a-zA-Z]*R", a) for a in argv[1:])
            if recursive and any(a in ("777", "0777", "a+rwx", "a=rwx", "ugo+rwx", "ugo=rwx") for a in argv[1:]):
                return "[chmod_777] recursive world-writable chmod. Grant the narrowest mode that works."
        if rules["pipe_to_shell"] and name in ("curl", "wget"):
            j = i
            while segs[j][1] in ("|", "|&") and j + 1 < len(segs):
                j += 1
                if posixpath.basename(segs[j][0][0]) in _SHELLS:
                    return "[pipe_to_shell] piping a download into a shell runs unreviewed code. Download, read, then run."
    if rules["pipe_to_shell"] and (_PROC_SUB.search(cmd) or _SUBST_EXEC.search(cmd)):
        return "[pipe_to_shell] executing a downloaded script without review. Download, read, then run."
    if rules["drop_sql"] and _SQL.search(cmd) and _DB_CLIENT.search(cmd):
        return "[drop_sql] DROP/TRUNCATE sent to a database client. Ask the user to run it."
    return None


# --------------------------------------------------------------------------- protected paths
def _matches(path: str, patterns: list[str], cwd: str) -> str | None:
    full = posixpath.normpath(os.path.join(cwd, os.path.expanduser(path)))
    base = posixpath.basename(full)
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatchcase(full, posixpath.normpath(os.path.expanduser(pat))):
                return pat
        elif fnmatch.fnmatchcase(base, pat):
            return pat
    return None


def protected_hit(path: str, cfg: dict, cwd: str, write: bool) -> str | None:
    if not path or _matches(path, cfg["allow"], cwd):
        return None
    return _matches(path, cfg["paths"] + (cfg["write_paths"] if write else []), cwd)


def _bash_paths(cmd: str) -> list[str]:
    """Words in a command that could name a file. echo/printf arguments are text, not paths."""
    words = []
    for argv, _ in segments(cmd):
        prev = ""
        for tok in argv:
            if posixpath.basename(argv[0]) not in ("echo", "printf") or prev in (">", ">>"):
                words.append(tok.split("=", 1)[1] if tok.startswith("--") and "=" in tok else tok)
            prev = tok
    return words


# --------------------------------------------------------------------------- entry point
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _written_text(tool: str, inp: dict) -> str:
    if tool == "Write":
        return inp.get("content") or ""
    if tool == "Edit":
        return inp.get("new_string") or ""
    if tool == "MultiEdit":
        return "\n".join(e.get("new_string") or "" for e in inp.get("edits") or [])
    if tool == "NotebookEdit":
        return inp.get("new_source") or ""
    return ""


def check(event: dict, cfg: dict, branch_of=current_branch) -> str | None:
    """Return a block reason for a PreToolUse event, or None."""
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    cmd = (inp.get("command") or "") if tool == "Bash" else ""

    pp = cfg["protected_paths"]
    if pp["enabled"]:
        if tool in _WRITE_TOOLS or tool == "Read":
            hit = protected_hit(path, pp, cwd, write=tool in _WRITE_TOOLS)
            if hit:
                return f"guardrails[protected_path]: {path} matches protected pattern {hit!r}. Ask the user to handle this file."
        for word in _bash_paths(cmd):
            hit = protected_hit(word, pp, cwd, write=False)
            if hit:
                return f"guardrails[protected_path]: command touches {word!r} (pattern {hit!r}). Ask the user to handle this file."

    sec = cfg["secret"]
    if sec["enabled"]:
        text = cmd or _written_text(tool, inp)
        skip = tool in _WRITE_TOOLS and _matches(path, sec["allow_paths"], cwd) if path else False
        name = None if skip else find_secret(text, sec)
        if name:
            where = "command" if cmd else f"content for {path}"
            return (f"guardrails[secret]: {where} contains a string shaped like a {name}{_line_of(text, sec)}. "
                    "Read it from an environment variable or secret manager instead of writing it out.")

    if cfg["destructive"]["enabled"] and cmd:
        why = check_command(cmd, cfg["destructive"], cwd, branch_of)
        if why:
            return f"guardrails[destructive]{why}"
    return None
