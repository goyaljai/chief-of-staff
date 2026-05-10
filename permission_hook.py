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

# V3.5 D1 Lite: cap individual snapshot entries — beyond this we record only
# a length + hash, not the content. Avoids ballooning snapshot files when
# Claude rewrites a 10MB build artifact.
_MAX_SNAPSHOT_BYTES = 256 * 1024  # 256KB


def _is_probably_binary(data: bytes) -> bool:
    """V3.5 R4-3 fix: detect binary content so we don't lossy-decode .aar /
    .apk / images / etc. into a text snapshot — undo would then write the
    corrupted text version back, destroying the file.

    Heuristic: any null byte in the first 8KB is a strong binary signal."""
    if b"\x00" in data[:8192]:
        return True
    # Also treat anything that fails strict UTF-8 decoding as binary.
    try:
        data.decode("utf-8")
        return False
    except UnicodeDecodeError:
        return True


def _maybe_snapshot_for_undo(tool_name: str, tool_input: dict, workspace: str) -> None:
    """Append a snapshot entry to <workspace>/.cos_snapshots.jsonl for any
    Write/Edit/MultiEdit (or their MCP equivalents) so /task/{id}/undo can
    restore the prior state. Best-effort — if anything fails, the hook still
    allows the tool call (we don't want to break the executor for an undo
    feature)."""
    try:
        if not workspace:
            return
        is_write = tool_name in WRITE_TOOLS or (
            tool_name.startswith("mcp__")
            and any(k in tool_name.lower() for k in ("write", "edit", "create"))
        )
        if not is_write:
            return
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        if not path:
            return
        from pathlib import Path as _P
        p = _P(path)
        if not p.is_absolute():
            p = _P(workspace) / path
        try:
            p.resolve().relative_to(_P(workspace).resolve())
        except (ValueError, OSError):
            return
        existed = p.exists()
        original = ""
        truncated = False
        size = 0
        is_binary = False
        if existed:
            try:
                size = p.stat().st_size
                if size > _MAX_SNAPSHOT_BYTES:
                    truncated = True
                else:
                    raw = p.read_bytes()
                    # R4-3 fix: detect binary BEFORE decoding. We never store
                    # binary content as a string — undo treats binary as
                    # "skip" rather than risk corruption.
                    if _is_probably_binary(raw):
                        is_binary = True
                        truncated = True
                    else:
                        original = raw.decode("utf-8")
            except Exception:
                truncated = True
        snap = {
            "ts": time.time(),
            "tool": tool_name,
            "path": str(p),
            "originally_existed": existed,
            "original_size": size,
            "original": "" if truncated else original,
            "truncated": truncated,
            "is_binary": is_binary,
        }
        # R4-4 fix: parallel DAG step subprocesses share the same workspace
        # (per the round-3 fix #8). Without a lock they can interleave JSON
        # lines into .cos_snapshots.jsonl and produce torn entries that
        # `/undo` then rejects. fcntl.flock serializes appends across
        # processes (POSIX-only, which matches our deployment).
        snap_path = _P(workspace) / ".cos_snapshots.jsonl"
        try:
            import fcntl as _fcntl
            with open(snap_path, "a") as f:
                _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
                try:
                    f.write(json.dumps(snap) + "\n")
                    f.flush()
                finally:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
        except Exception:
            # Non-POSIX or fcntl unavailable — fall back to plain append.
            # Concurrent writes still risk torn lines but it's all we can do.
            with open(snap_path, "a") as f:
                f.write(json.dumps(snap) + "\n")
    except Exception as e:
        sys.stderr.write(f"hook: snapshot capture failed (non-fatal): {e}\n")

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
    elif tool_name.startswith("mcp__"):
        if "bash" in tool_name.lower():
            cmd = (tool_input.get("command") or "")
            decision, reason = decide_bash(cmd, workspace)
            if decision == "allow":
                decision, reason = "allow_unknown", f"MCP bash variant ({tool_name}): supervisor will review"
        elif any(k in tool_name.lower() for k in ("write", "edit", "create")):
            path = tool_input.get("file_path") or tool_input.get("path") or ""
            decision, reason = decide_write(path, workspace)
            if decision == "allow":
                decision, reason = "allow_unknown", f"MCP write variant ({tool_name}): supervisor will review"
        else:
            decision, reason = "allow_unknown", f"MCP tool {tool_name}: supervisor will review"
    else:
        decision, reason = "allow", f"non-restricted tool: {tool_name}"

    # V3.5 D1 Lite: snapshot the original file content for any allowed write
    # so /task/{id}/undo can restore. Skip blocked ops (no mutation incoming)
    # and only handle Write/Edit/MultiEdit + their MCP equivalents.
    if decision != "block":
        _maybe_snapshot_for_undo(tool_name, tool_input, workspace)

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
