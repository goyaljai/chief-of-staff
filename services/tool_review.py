"""Agnostic classifier — does this tool name look side-effecting?

Why this exists
---------------
Per-action review (Reviewer agent gating Bash/Write/Edit before they execute)
used to be hardcoded:

    REVIEW_TOOLS = {"Bash", "Write", "Edit", "MultiEdit"}
    _is_reviewable_mcp_tool: substring check for "bash"/"write"/"edit" in mcp__*

This biased the supervisor toward the developer's own setup. Users who route
Write/Edit through their own MCP (e.g. token-savers, sandbox proxies) — or who
add MCPs we've never heard of (Slack-poster, K8s-applier, GitHub-merger) —
either got *extra* reviews on irrelevant read-only tools, or *no* review on
genuine side-effecting MCPs.

The fix: classify by *tokenized* tool name, not by a fixed allowlist.

How
---
Tokenize the name on three boundaries: snake_case, CamelCase, and the
MCP-style `mcp__server__name` triple-underscore. Match any token against a
verb set. False positives (an irrelevant tool gets reviewed) are cheap — one
extra Reviewer call. False negatives (a genuine side-effecting tool slips
review) are the expensive case, so the verb set leans inclusive.

Examples
~~~~~~~~
  Bash                                       → ["bash"]                          → review
  MultiEdit                                  → ["multi", "edit"]                 → review
  mcp__glance-token-saver__write_file        → [..., "write", "file"]            → review
  mcp__glance-token-saver__bash_compressed   → [..., "bash", "compressed"]       → review
  mcp__glance-token-saver__read_compressed   → [..., "read", "compressed"]       → skip
  Read / Grep / WebFetch / WebSearch         → no verb match                     → skip
  mcp__plugin_exa_exa__authenticate          → no verb match                     → skip

Override
--------
Set ``REVIEW_EXTRA_VERBS`` in env (comma-separated) to add to the verb set.
Useful for org-specific MCPs whose names don't include a standard verb
(e.g. ``REVIEW_EXTRA_VERBS=apply,merge,promote``).
"""
from __future__ import annotations

import os
import re

# Verbs that signal a side-effecting action — reviewing one false positive
# (a read-only tool whose name happens to contain "save" or "send") costs one
# Reviewer call. Missing a real write costs much more.
SIDE_EFFECT_VERBS: frozenset[str] = frozenset({
    # filesystem
    "write", "edit", "modify", "create", "delete", "remove", "rm",
    "append", "save", "patch", "update", "overwrite",
    "mkdir", "rmdir", "mv", "cp", "move", "copy", "rename",
    "chmod", "chown", "touch",
    # shell / process
    "bash", "shell", "exec", "execute", "run", "command", "cmd",
    "spawn", "kill", "launch", "start", "stop", "restart",
    # network mutation
    "post", "put", "push", "send", "publish", "deploy", "upload",
    "submit", "trigger", "invoke",
    # data store
    "insert", "drop", "alter", "truncate", "migrate", "commit",
})

# Tools that look reviewable by name but aren't worth the Reviewer round-trip:
#   TodoWrite — Claude's plan list. Updating it has no external effect; we
#               already special-case it in supervisor_loop._on_event to
#               capture the plan itself.
NON_REVIEWABLE: frozenset[str] = frozenset({"TodoWrite"})


def _extra_verbs_from_env() -> set[str]:
    raw = os.getenv("REVIEW_EXTRA_VERBS", "").strip()
    if not raw:
        return set()
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


_VERBS = SIDE_EFFECT_VERBS | _extra_verbs_from_env()


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")
_TOKEN_SPLIT = re.compile(r"[_\W]+")


def tokenize(tool_name: str) -> list[str]:
    """Split a tool name into lowercase tokens.

    Splits on snake_case (``_``), CamelCase, and any non-word char (covers
    ``-``, ``.``, and the MCP triple-underscore). Empty tokens are dropped.
    """
    if not tool_name:
        return []
    snake = _CAMEL_BOUNDARY.sub("_", tool_name)
    return [t for t in _TOKEN_SPLIT.split(snake.lower()) if t]


def is_side_effecting(tool_name: str | None) -> bool:
    """True if this tool warrants per-action Reviewer review.

    Works for native Claude tools (``Bash``, ``Write``, ``MultiEdit``) and
    arbitrary MCP tools (``mcp__org__write_thing``). The same code path
    classifies both — there is no native-vs-MCP branch.
    """
    if not tool_name or tool_name in NON_REVIEWABLE:
        return False
    tokens = tokenize(tool_name)
    return any(t in _VERBS for t in tokens)


def classify_announced_tools(tool_names: list[str]) -> tuple[list[str], list[str]]:
    """Partition a tool list into (review-worthy, skipped).

    Called once on the ``system.init`` event so the supervisor can log which
    tools will be reviewed for this session — useful when debugging "why
    didn't the reviewer fire on X?" against a new MCP.
    """
    review: list[str] = []
    skip: list[str] = []
    for name in tool_names:
        (review if is_side_effecting(name) else skip).append(name)
    return review, skip
