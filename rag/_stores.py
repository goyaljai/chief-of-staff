"""Stage 2: PGVector store wrappers (LangChain pgvector integration).

Two collections live in one Postgres database (langchain_pg_embedding
table) sharing the same row schema but separated by `collection_name`:

  task_summaries     — completed task summaries, used by /ask to answer
                       "have we done something like this before?"
  skill_descriptions — battle-tested skill briefs, used by orchestrator's
                       find_matching_skill to decide "reuse a known recipe
                       vs. regenerate from scratch"

Module state (singletons via _STORES_LOCK):
  _TASK_STORE  — PGVector for task_summaries
  _SKILL_STORE — PGVector for skill_descriptions

`_PRE_RERANK_FETCH = 20` is the cosine-stage candidate pool size. Larger
pools give the reranker more material at higher API cost. 20 is a
reasonable default for personal-scale retrieval.
"""
import os
import threading

from langchain_postgres import PGVector

from .embeddings import _get_embed


_STORES_LOCK = threading.RLock()
_TASK_STORE: PGVector | None = None
_SKILL_STORE: PGVector | None = None

_PRE_RERANK_FETCH = 20


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return url


def _get_task_store() -> PGVector:
    """PGVector store for task summaries. Used by search_tasks for
    'have we done something like this before?' queries on /ask."""
    global _TASK_STORE
    if _TASK_STORE is not None:
        return _TASK_STORE
    with _STORES_LOCK:
        if _TASK_STORE is None:
            _TASK_STORE = PGVector(
                collection_name="task_summaries",
                connection=_dsn(),
                embeddings=_get_embed(),
                use_jsonb=True,
            )
        return _TASK_STORE


def _get_skill_store() -> PGVector:
    """PGVector store for skill descriptions. Used by find_matching_skill
    to detect 'we have a battle-tested brief — reuse it instead of
    regenerating'."""
    global _SKILL_STORE
    if _SKILL_STORE is not None:
        return _SKILL_STORE
    with _STORES_LOCK:
        if _SKILL_STORE is None:
            _SKILL_STORE = PGVector(
                collection_name="skill_descriptions",
                connection=_dsn(),
                embeddings=_get_embed(),
                use_jsonb=True,
            )
        return _SKILL_STORE
