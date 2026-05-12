"""Mem0 cloud cross-task memory (T4).

Replaces / augments the skill_lessons + pgvector hybrid with a real
agent-memory layer. Mem0 stores structured facts about completed
tasks, user preferences, and recurring patterns; we query it at the
start of every new task so the orchestrator gets context without
re-reading task history every time.

Why Mem0 cloud and not self-host:
  - Cloud is one HTTP call away — no local Qdrant + embedding
    container to maintain.
  - The free tier is plenty for a single-user system; we'd burn more
    engineer time on self-host than the cloud will ever cost.
  - User chose cloud explicitly; key in .env.

This module is a thin wrapper:
  - ``add_task_memory(task, deliverables, summary, ...)`` — call
    this when a task completes so Mem0 has something to retrieve next
    time.
  - ``get_relevant_memories(query, ...)`` — call this at orchestrator
    think_and_ask to fetch prior context.
  - ``is_enabled()`` — gate every call. If the key is missing, every
    function silently returns "" / [] so the rest of the pipeline
    works exactly as it did pre-T4.

Failure mode: any HTTP error swallows quietly with a one-line log.
Mem0 is a quality-of-life feature; a Mem0 outage must NOT break a
task.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_USER_ID = "cos-default"
_client_lock = threading.Lock()
_client = None


def is_enabled() -> bool:
    """True iff MEM0_API_KEY is present in env. Used as the gate on
    every public function so the rest of the pipeline is unaffected
    when Mem0 isn't configured."""
    return bool(os.environ.get("MEM0_API_KEY"))


def _get_client():
    """Lazy singleton — Mem0 keeps an httpx pool internally so one
    client across all tasks is correct. Lock is for the
    construct-once race; subsequent calls go straight through."""
    global _client
    if _client is not None:
        return _client
    if not is_enabled():
        return None
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from mem0 import MemoryClient
        except Exception as e:
            log.warning("[mem0] import failed; cross-task memory disabled: %s", e)
            return None
        try:
            _client = MemoryClient(api_key=os.environ["MEM0_API_KEY"])
        except Exception as e:
            log.warning("[mem0] client init failed; cross-task memory disabled: %s", e)
            return None
    return _client


def add_task_memory(
    task: str,
    deliverables: list[str],
    summary: str,
    rationale: str = "",
    user_id: str = _DEFAULT_USER_ID,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Persist what we learned from a completed task.

    Stores a single conversation turn shaped as ``(user task → reviewer
    summary)``. Mem0 will extract durable facts ("the user prefers
    minimal blog post output", "the Android workspace lives at X")
    and discard ephemera. Returns True on success.

    No-op when MEM0_API_KEY is unset.
    """
    client = _get_client()
    if client is None:
        return False
    if not task or not summary:
        return False
    body_user = task[:1000]
    body_assistant_lines = [summary[:500]]
    if rationale:
        body_assistant_lines.append(f"Reasoning: {rationale[:300]}")
    if deliverables:
        body_assistant_lines.append("Deliverables: " + ", ".join(deliverables[:5]))
    body_assistant = "\n".join(body_assistant_lines)
    messages = [
        {"role": "user", "content": body_user},
        {"role": "assistant", "content": body_assistant},
    ]

    # Bug fix (Phase 3 audit r2): the Mem0 cloud .add() is synchronous
    # HTTP and takes 200-500ms. Calling it inline from the supervisor's
    # async run() loop blocks the asyncio event loop for that window,
    # stalling every other concurrently running task. Mem0 writes are
    # fire-and-forget — if they drop on a transient error we lose one
    # memory, not correctness. Run in a daemon thread so the supervisor
    # returns immediately.
    def _do_add() -> None:
        try:
            client.add(messages, user_id=user_id, metadata=metadata or {})
            log.info("[mem0] stored memory for user=%s task=%r", user_id, task[:60])
        except Exception as e:
            log.warning("[mem0] add failed; continuing: %s", e)

    threading.Thread(target=_do_add, daemon=True, name="mem0-add").start()
    return True


def get_relevant_memories(
    query: str,
    user_id: str = _DEFAULT_USER_ID,
    limit: int = 5,
    timeout_secs: float = 3.0,
) -> list[dict]:
    """Retrieve memories relevant to ``query``. Returns a list of
    ``{"memory": str, "score": float, ...}`` dicts (Mem0's native
    shape). Empty list on miss / disabled / error / timeout.

    Bug fix (Phase 3 audit r2): the Mem0 SDK's .search() is sync HTTP
    with no built-in deadline. Wrapping it in a thread + future with a
    hard timeout prevents a slow / unreachable Mem0 endpoint from
    stalling the orchestrator forever. 3s is a generous ceiling — a
    healthy Mem0 round-trip is ~200ms.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    client = _get_client()
    if client is None:
        return []
    if not query:
        return []

    def _do_search():
        # Mem0 v2 requires filters={'user_id': ...}; v1's top-level
        # user_id kwarg is rejected. Pass both via filters to stay
        # forward-compatible.
        return client.search(
            query=query[:1000],
            filters={"user_id": user_id},
            limit=limit,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_do_search).result(timeout=timeout_secs)
    except FuturesTimeout:
        log.warning("[mem0] search exceeded %.1fs timeout; returning empty", timeout_secs)
        return []
    except Exception as e:
        log.warning("[mem0] search failed; returning empty: %s", e)
        return []
    if isinstance(result, dict):
        items = result.get("results") or result.get("memories") or []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    return items[:limit]


def render_memory_block(memories: list[dict]) -> str:
    """Format Mem0 results as a markdown block suitable for inclusion
    in the orchestrator prompt. Returns '' on empty input."""
    if not memories:
        return ""
    lines = ["## Relevant prior context (Mem0 cross-task memory)"]
    for m in memories:
        text = (m.get("memory") or m.get("content") or "").strip()
        if not text:
            continue
        score = m.get("score")
        score_tag = f" _(score={score:.2f})_" if isinstance(score, (int, float)) else ""
        lines.append(f"- {text}{score_tag}")
    if len(lines) == 1:
        return ""
    lines.append(
        "\nUse this context to skip questions whose answers are already known, "
        "or to reuse prior decisions for the same kind of task. Do NOT cite memory "
        "as authoritative if it contradicts the current task — recency wins."
    )
    return "\n".join(lines)
