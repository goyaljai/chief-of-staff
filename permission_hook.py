#!/usr/bin/env python3
"""
PreToolUse hook for Claude Code.

Claude Code spawns this script BEFORE every tool call.
We read the tool call from stdin (JSON), decide allow/block, exit.

Exit codes per Claude Code hooks spec:
  0  = allow
  2  = block (stderr message goes back to Claude as denial reason)

We also emit one JSONL line per decision to a per-task log so the
Supervisor can audit what was allowed/denied after the fact.

Hook is registered per-task in <task_workspace>/.claude/settings.json,
so it ONLY fires for Claude Code instances spawned by the Supervisor —
your normal Claude Code usage is untouched.
"""
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

ALWAYS_ALLOW_TOOLS = {"Read", "Glob", "Grep", "LS"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

BASH_ALWAYS_ALLOW_PREFIXES = (
    "ls", "pwd", "cat", "echo", "head", "tail", "wc",
    "find", "grep", "rg", "tree", "file", "stat",
    "mkdir", "touch", "cp", "mv",
    "test", "[", "true", "false",
    "gradle", "gradlew", "./gradlew", "java", "javac", "kotlin", "kotlinc",
    "python", "python3", "pip", "node", "npm", "npx", "yarn", "pnpm",
    "go", "cargo", "rustc",
    "git", "diff", "patch",
    "make", "cmake",
    "which", "type", "command",
    "date", "sleep", "env", "export",
)

BASH_HARD_DENY_PATTERNS = (
    r"\brm\s+-rf?\s+/(\s|;|\||&|$)",
    r"\brm\s+-rf?\s+/\*",
    r"\brm\s+-rf?\s+~(\s|;|\||&|/|$)",
    r"\brm\s+-rf?\s+\$HOME(\s|;|\||&|/|$)",
    r"\bdd\s+if=.*of=/dev/(sd|nvme|disk|hd)",
    r"\bmkfs\.\w+\s+/dev/",
    r"\b:\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:",
    r">\s*/dev/sd[a-z]",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bdiskutil\s+(erase|secureErase)",
)

BASH_REVIEW_PATTERNS = (
    r"\bsudo\b",
    r"\bsu\s+",
    r"\bcurl\s+.*\|\s*(sh|bash|zsh)",
    r"\bwget\s+.*\|\s*(sh|bash|zsh)",
    r"\bchmod\s+777\b",
    r"\beval\s+",
    r"\bpip\s+install\s+-",
    r"\bnpm\s+install\s+-g",
)

BLOCKED_PATH_FRAGMENTS = (
    "/etc/", "/System/", "/Library/Keychains",
    "/.ssh/", "/.aws/", "/.gnupg/",
)

DESTRUCTIVE_BASH_COMMANDS = ("rm", "rmdir", "shred", "trash")
WORKSPACE_SAFE_OUTSIDE = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/folders/")


def _path_inside_workspace(path: str, workspace: str) -> bool:
    if not workspace:
        return True
    try:
        from pathlib import Path
        p = Path(path).resolve()
        ws = Path(workspace).resolve()
        return ws == p or ws in p.parents
    except Exception:
        return False


def _path_in_safe_outside(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in WORKSPACE_SAFE_OUTSIDE)


def _extract_destructive_targets(cmd: str) -> list[str]:
    targets: list[str] = []
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return targets
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        base = tok.split("/")[-1]
        if base in DESTRUCTIVE_BASH_COMMANDS or tok in DESTRUCTIVE_BASH_COMMANDS:
            j = i + 1
            while j < len(tokens):
                arg = tokens[j]
                if arg in (";", "&&", "||", "|", "&"):
                    break
                if arg.startswith("-"):
                    j += 1
                    continue
                targets.append(arg)
                j += 1
            i = j
        else:
            i += 1
    return targets


def log_decision(record: dict):
    log_path = os.environ.get("SUPERVISOR_HOOK_LOG")
    if not log_path:
        return
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def decide_bash(command: str, workspace: str = "") -> tuple[str, str]:
    cmd = command.strip()
    if not cmd:
        return "allow", "empty command"

    for pattern in BASH_HARD_DENY_PATTERNS:
        if re.search(pattern, cmd, flags=re.IGNORECASE):
            return "block", f"catastrophic pattern blocked: {pattern}"

    if workspace:
        for target in _extract_destructive_targets(cmd):
            if target.startswith("/") or target.startswith("~") or target.startswith("$"):
                if _path_in_safe_outside(target):
                    continue
                if not _path_inside_workspace(target, workspace):
                    return "block", f"destructive op targets path outside workspace: {target}"

    for pattern in BASH_REVIEW_PATTERNS:
        if re.search(pattern, cmd, flags=re.IGNORECASE):
            return "review", f"sensitive command, supervisor will review: {pattern}"

    try:
        first_token = shlex.split(cmd)[0]
    except ValueError:
        first_token = cmd.split()[0] if cmd.split() else ""

    base = first_token.split("/")[-1]
    for prefix in BASH_ALWAYS_ALLOW_PREFIXES:
        if base == prefix or first_token == prefix or first_token.endswith("/" + prefix):
            return "allow", f"safelisted: {base}"

    return "allow_unknown", f"unknown command (allowed, supervisor will review): {base}"


def decide_write(path: str, workspace: str = "") -> tuple[str, str]:
    if not path:
        return "allow", "no path"
    for fragment in BLOCKED_PATH_FRAGMENTS:
        if fragment in path:
            return "block", f"writes to sensitive path: {fragment}"
    if workspace and (path.startswith("/") or path.startswith("~")):
        if _path_in_safe_outside(path):
            return "allow", "path inside safe-outside dir"
        if not _path_inside_workspace(path, workspace):
            return "block", f"write target outside workspace: {path}"
    return "allow", "path ok"


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sys.stderr.write("hook: could not parse stdin as JSON\n")
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    workspace = os.environ.get("SUPERVISOR_WORKSPACE", "")

    decision = "allow"
    reason = "default allow"

    if tool_name in ALWAYS_ALLOW_TOOLS:
        decision, reason = "allow", "always-allow tool"
    elif tool_name in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        decision, reason = decide_write(path, workspace)
    elif tool_name == "Bash":
        decision, reason = decide_bash(tool_input.get("command", ""), workspace)
    else:
        decision, reason = "allow", f"non-restricted tool: {tool_name}"

    log_decision({
        "ts": time.time(),
        "tool": tool_name,
        "input": tool_input,
        "decision": decision,
        "reason": reason,
    })

    if decision == "block":
        sys.stderr.write(f"Supervisor blocked: {reason}\n")
        sys.exit(2)

    sys.exit(0)


# decision values:
#   allow          — safelisted, fine
#   allow_unknown  — unknown but not catastrophic; supervisor reviews via log
#   review         — sensitive (sudo, curl|sh); allowed but flagged for supervisor
#   block          — catastrophic; never allowed (rm -rf /, dd to disk, shutdown, etc.)


if __name__ == "__main__":
    main()
