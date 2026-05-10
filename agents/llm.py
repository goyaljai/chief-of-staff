"""Databricks chat LLM gateway used by every agent role.

We talk to Databricks AI Gateway through OpenAI's SDK because the gateway
exposes an OpenAI-compatible chat endpoint and we already have the SDK
installed for embeddings (see rag/embeddings.py). One library, two uses.

Why a hand-rolled retry loop wrapping the SDK:
  • the SDK's own max_retries setting only handles a narrow set of errors
    cleanly (5xx via a generic backoff). We want jittered exponential backoff
    on a wider transient set: 429 / connection / timeout / 5xx, plus a
    substring fallback for SDKs that pass through provider errors as plain
    Exception. F5 (orchestrator) — see commit history for the original fix.
  • we want a per-context usage callback (cost accounting) that fires after
    each successful call. contextvars keeps that callback scoped to the
    current asyncio.Task so parallel DAG steps don't leak callbacks into
    each other.
  • LangSmith tracing wraps the OpenAI client object globally, but only when
    LANGSMITH_TRACING is true AND the API key is set. Wrapping early when
    the env var is absent silently breaks tracing for everyone.

Public API:
  _chat(system, user, max_tokens=2048, skills_context="") -> str
  _extract_json(text) -> dict
  set_usage_callback(cb)
  _client() -> OpenAI
  _maybe_init_langsmith() -> bool
"""
import contextvars
import json
import random
import re
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from config import DATABRICKS_BASE_URL, DATABRICKS_MODEL, DATABRICKS_TOKEN


_LANGSMITH_TRACED = False


def _maybe_init_langsmith() -> bool:
    """Enable LangSmith tracing if env vars say so. Wraps the OpenAI client
    globally. Returns False (and prints once) if the flag is on but the API
    key is missing — so the user knows tracing isn't actually active."""
    global _LANGSMITH_TRACED
    if _LANGSMITH_TRACED:
        return True
    import os
    if os.environ.get("LANGSMITH_TRACING", "").lower() not in ("true", "1", "yes"):
        return False
    if not os.environ.get("LANGSMITH_API_KEY"):
        print("[langsmith] LANGSMITH_TRACING=true but LANGSMITH_API_KEY missing; skipping")
        return False
    try:
        from langsmith.wrappers import wrap_openai  # noqa: F401
        _LANGSMITH_TRACED = True
        print(
            f"[langsmith] tracing enabled "
            f"(project={os.environ.get('LANGSMITH_PROJECT', 'chief-of-staff')})"
        )
        return True
    except Exception as e:
        print(f"[langsmith] failed to init: {e}")
        return False


def _client() -> OpenAI:
    """One OpenAI client per call — the SDK is cheap to construct and the
    Databricks gateway expects fresh connection-keepalive tracking. If the
    LangSmith wrapper is available we apply it on the way out so every call
    is automatically traced."""
    base = OpenAI(
        api_key=DATABRICKS_TOKEN,
        base_url=DATABRICKS_BASE_URL,
        max_retries=5,
        timeout=120.0,
    )
    if _maybe_init_langsmith():
        try:
            from langsmith.wrappers import wrap_openai
            return wrap_openai(base)
        except Exception:
            return base
    return base


# ContextVar so each asyncio.Task's supervisor loop has its own callback —
# parallel DAG steps don't leak callbacks into each other.
_USAGE_CALLBACK_CTX: contextvars.ContextVar = contextvars.ContextVar(
    "usage_callback", default=None,
)


def set_usage_callback(cb):
    """Register a per-context callback. Each task's supervisor loop runs
    in its own asyncio.Task with its own context."""
    _USAGE_CALLBACK_CTX.set(cb)


def _chat(system: str, user: str, max_tokens: int = 2048, skills_context: str = "") -> str:
    """Single chat completion against Databricks. Adds the skills_context
    (if any) into the system message so the agent always has the global
    skills + per-task SKILL brief in front of it.

    F5: production-grade retry — explicit OpenAI exception types (more
    robust than substring matching on the error message) PLUS a substring
    fallback for plain Exceptions that some provider integrations raise.
    Jittered backoff so synchronized retries don't pile onto the gateway:
    1s/2s/4s base + 0–1s jitter, max 4 attempts (~10s total wait).
    """
    full_system = system
    if skills_context:
        full_system = (
            f"{system}\n\n"
            "## Available skills / knowledge for this task\n"
            "Treat these as authoritative knowledge supplementing your judgment.\n\n"
            f"{skills_context}"
        )

    transient_excs = (
        RateLimitError,           # 429 from gateway
        APIConnectionError,       # network blip / DNS / reset
        APITimeoutError,          # client-side timeout
        InternalServerError,      # 5xx
    )
    transient_substrings = (
        # Belt-and-braces: providers occasionally raise generic Exception
        # before the SDK has classified it. Keep these as a fallback.
        "rate limit", "rate_limit", "429",
        "503", "502", "504", "timeout", "timed out",
        "connection", "temporary", "overloaded",
    )
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            response = _client().chat.completions.create(
                model=DATABRICKS_MODEL,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": full_system},
                    {"role": "user", "content": user},
                ],
            )
            try:
                cb = _USAGE_CALLBACK_CTX.get()
                if cb and getattr(response, "usage", None):
                    cb(
                        getattr(response.usage, "prompt_tokens", 0) or 0,
                        getattr(response.usage, "completion_tokens", 0) or 0,
                    )
            except Exception:
                pass
            return response.choices[0].message.content or ""
        except transient_excs as e:
            last_err = e
            transient = True
        except Exception as e:
            last_err = e
            transient = any(s in str(e).lower() for s in transient_substrings)
        if not transient or attempt == 3:
            raise last_err
        base = 2 ** attempt  # 1, 2, 4, 8
        wait = base + random.uniform(0, 1.0)
        kind = type(last_err).__name__
        print(
            f"[orchestrator] transient {kind} (attempt {attempt+1}/4): "
            f"{last_err}. retrying in {wait:.1f}s"
        )
        time.sleep(wait)
    if last_err:
        raise last_err
    return ""


def _extract_json(text: str) -> dict:
    """Best-effort extraction of a JSON object from an LLM response. Strips
    code fences, finds the outermost `{...}` block, json.loads. Returns an
    empty dict on any failure — callers handle missing keys with defaults."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
