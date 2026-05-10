"""Shared singletons + ephemeral caches used across route modules.

The Orchestrator is expensive to instantiate (loads prompts, opens skill
library, optionally warms an LLM client) so we keep one process-global
instance and import it from every route handler that needs it.

The skill-preview cache is a tiny LRU-ish dict keyed by hash(task_text). The
two-step `/task/questions` → `/task/run` flow uses it to avoid re-running
meta-think when the user submits the same task they just got questions for.

Why this lives outside main.py:
  - Multiple route modules need orchestrator_singleton; importing it from
    main creates a circular import (main imports routes → routes import
    main → ...).
  - Putting the singleton in its own module breaks the cycle and makes the
    dependency direction obvious: routes/ → dependencies → orchestrator.

Public exports:
  orchestrator_singleton — the one Orchestrator instance for the process
  stash_preview(task, preview)
  pop_preview(task) -> str
"""
import hashlib
import time

from agents import Orchestrator


orchestrator_singleton = Orchestrator()


_SKILL_PREVIEW_CACHE: dict[str, tuple[str, float]] = {}
_PREVIEW_TTL_SECS = 600


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.strip().encode()).hexdigest()[:16]


def stash_preview(task: str, preview: str) -> None:
    _SKILL_PREVIEW_CACHE[_task_hash(task)] = (preview, time.time())


def pop_preview(task: str) -> str:
    """Return the stashed preview if still fresh, else empty string. Removes
    the entry on access regardless of freshness so the cache self-trims."""
    entry = _SKILL_PREVIEW_CACHE.pop(_task_hash(task), None)
    if not entry:
        return ""
    preview, ts = entry
    if time.time() - ts > _PREVIEW_TTL_SECS:
        return ""
    return preview
