"""Runner package — subprocess wrappers around external CLI tools.

Today there's exactly one runner: ClaudeRunner, which drives the
`claude --output-format stream-json` CLI for headless task execution.
The package layout leaves room for future runners (Codex, Aider,
Devin, etc.) without renaming anything.

LAYOUT
======
  runners/events.py — ClaudeEvent + TaskResult dataclasses
  runners/hooks.py  — install_hooks (writes per-task .claude/settings.json)
  runners/claude.py — class ClaudeRunner

PUBLIC API
==========
  from runners import ClaudeRunner, ClaudeEvent, TaskResult, install_hooks
"""
from .claude import ClaudeRunner
from .events import ClaudeEvent, TaskResult
from .hooks import install_hooks


__all__ = ["ClaudeRunner", "ClaudeEvent", "TaskResult", "install_hooks"]
