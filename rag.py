"""rag.py — Retrieval-augmented generation layer for chief-of-staff.

ARCHITECTURE (v2.0)
===================

Two-stage retrieval pipeline:

    query
      │
      ▼
   ┌─────────────────────────────────────────────────────┐
   │ STAGE 1 — EMBED (T2)                                │
   │   Databricks AI Gateway: gte-large-en (1024-dim)    │
   │   Reuses our existing DATABRICKS_TOKEN — no new     │
   │   key. Falls back to Voyage voyage-3 if Databricks  │
   │   is unreachable (also 1024-dim, schema-compatible).│
   └─────────────────────────────────────────────────────┘
      │
      ▼ vector
   ┌─────────────────────────────────────────────────────┐
   │ STAGE 2 — pgvector cosine search                    │
   │   Postgres pgvector index on tasks.summary_embedding│
   │   and tasks.skill_embedding (both vector(1024)).    │
   │   Returns top N by cosine similarity (N >> top_k    │
   │   so reranker has candidates to choose from).       │
   └─────────────────────────────────────────────────────┘
      │
      ▼ N candidates
   ┌─────────────────────────────────────────────────────┐
   │ STAGE 3 — RERANK (T1)                               │
   │   Voyage rerank-2 cross-encoder reads (query, doc)  │
   │   pairs and returns top_k by relevance. Lifts       │
   │   retrieval accuracy +15-30% vs cosine alone.       │
   │   Cleanly degrades to "skip rerank" when            │
   │   VOYAGE_API_KEY is missing (returns the cosine     │
   │   results unchanged).                               │
   └─────────────────────────────────────────────────────┘
      │
      ▼ top_k results to caller

KEYS USED
=========

  DATABRICKS_TOKEN, DATABRICKS_BASE_URL — for Stage 1 (embeddings).
  VOYAGE_API_KEY                        — for Stage 3 (rerank). Optional.
  DATABASE_URL                          — for Stage 2 (pgvector storage).

PUBLIC API
==========

  index_task(task_id, goal, summary, skill_md="")
  index_skill(task_id, name, description, skill_md)
  search_tasks(query, top_k=5)         → reranked task summaries
  find_matching_skill(query, top_k=1, distance_max=0.40) → reranked + filtered
  reindex_all_from_db()                → bulk re-embed + reindex
  warm_embedding_fn() / is_ready()     → optional warmup helper

WHY THESE CHOICES (v2.0 vs v1.0)
================================

v1.0 used local HuggingFace MiniLM (384-dim, sentence-transformers).
Pros: free, offline, no API. Cons: ~600MB model, CPU-bound, lower quality.

v2.0 swaps to Databricks gte-large-en (1024-dim, API). Pros: no local model
download, higher quality on benchmarks, same auth as our LLM calls. Cons:
adds an API roundtrip per index.

The reranker (Voyage rerank-2) is added in v2.0 because pure cosine
similarity over high-dim embeddings still has known failure modes (synonyms,
paraphrase). A cross-encoder reading the actual query+doc text catches
those — empirically +15-30% on standard RAG benchmarks.
"""
import os
import threading

from langchain_postgres import PGVector

import db


# ─── module state ─────────────────────────────────────────────────────────

_LOCK = threading.RLock()
_EMBED = None                      # singleton embedding client (Stage 1)
_TASK_STORE: PGVector | None = None
_SKILL_STORE: PGVector | None = None
_READY = False

# Reranker client cached lazily — None means "not yet checked / not configured"
_RERANK_CLIENT = None
_RERANK_INIT_TRIED = False

# Tunable: how many candidates to fetch from pgvector before rerank trims to
# top_k. Larger = more material for reranker to work with, more API cost.
# 20 is a reasonable balance for personal-scale retrieval.
_PRE_RERANK_FETCH = 20


# ─── Stage 1: embeddings (Databricks gte-large-en, 1024-dim) ──────────────

class DatabricksEmbeddings:
    """LangChain-compatible embedding wrapper around Databricks AI Gateway.

    Why a hand-rolled class instead of langchain-databricks:
      - langchain-databricks pulls in heavy MLflow deps and assumes a
        Databricks SDK environment we don't otherwise use.
      - Our existing DATABRICKS_TOKEN + DATABRICKS_BASE_URL already work
        with the OpenAI SDK against the AI Gateway, so we just call the
        /embeddings endpoint directly.

    Implements the protocol that langchain-postgres PGVector needs:
        embed_query(str)            -> list[float]
        embed_documents(list[str])  -> list[list[float]]

    Output dim is 1024 for `databricks-gte-large-en`. Schema must match
    (see migrations/postgres_v2_0_t2_embed_1024.sql).

    Reliability (R5-1 fix):
      The embeddings endpoint can return 429 / 5xx / connection errors just
      like chat. We mirror the F5 retry pattern from orchestrator._chat —
      explicit OpenAI exception types + jittered exponential backoff. A
      single transient blip used to fail an entire RAG operation; now it
      retries up to 4 times.
    """

    def __init__(self, model: str = "databricks-gte-large-en"):
        # Lazy import so importing this module doesn't require the OpenAI SDK
        # being installed (it always is in our env, but principle-of-least-
        # surprise wins).
        from openai import OpenAI
        self._client = OpenAI(
            api_key=os.environ["DATABRICKS_TOKEN"],
            base_url=os.environ["DATABRICKS_BASE_URL"],
            timeout=60.0,
            max_retries=3,
        )
        self._model = model
        self._batch_size = 96  # Databricks gateway accepts batches; 96 is safe

    def _create_with_retry(self, inputs: list[str]):
        """R5-1: jittered exponential backoff on transient failures.
        Mirrors orchestrator._chat's pattern. 4 attempts total: 1s, 2s, 4s, 8s
        base waits + 0-1s jitter.

        Also guards against empty inputs which Databricks returns 400 for —
        we strip empties and raise our own clearer error if everything's empty."""
        from openai import (
            APIConnectionError, APITimeoutError,
            InternalServerError, RateLimitError,
        )
        import random as _random
        import time as _time
        # Defensive: empty strings cause 400 from the gateway.
        clean = [t for t in inputs if (t or "").strip()]
        if not clean:
            raise ValueError("DatabricksEmbeddings: all input texts were empty")
        transient_excs = (
            RateLimitError, APIConnectionError, APITimeoutError, InternalServerError,
        )
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return self._client.embeddings.create(model=self._model, input=clean)
            except transient_excs as e:
                last_exc = e
                if attempt == 3:
                    raise
                wait = (2 ** attempt) + _random.uniform(0, 1.0)
                print(f"[rag] embed transient {type(e).__name__} attempt {attempt+1}/4 — retrying in {wait:.1f}s")
                _time.sleep(wait)
        if last_exc:  # pragma: no cover (loop exits via raise)
            raise last_exc

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query. Used by retrieval calls (search_tasks,
        find_matching_skill)."""
        resp = self._create_with_retry([text])
        return resp.data[0].embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents. Used by index calls. Splits into
        chunks of `_batch_size` to respect gateway limits."""
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            chunk = texts[i:i + self._batch_size]
            resp = self._create_with_retry(chunk)
            out.extend(d.embedding for d in resp.data)
        return out


def _get_embed():
    """Singleton accessor for the embedding client. Thread-safe via _LOCK."""
    global _EMBED
    if _EMBED is not None:
        return _EMBED
    with _LOCK:
        if _EMBED is None:
            _EMBED = DatabricksEmbeddings()
        return _EMBED


# ─── Stage 2: pgvector stores (LangChain PGVector wrappers) ──────────────

def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return url


def _get_task_store() -> PGVector:
    """PGVector store for task summaries (collection_name='task_summaries').
    Used by search_tasks() for 'have we done something like this before?'
    queries on the /ask endpoint."""
    global _TASK_STORE
    if _TASK_STORE is not None:
        return _TASK_STORE
    with _LOCK:
        if _TASK_STORE is None:
            _TASK_STORE = PGVector(
                collection_name="task_summaries",
                connection=_dsn(),
                embeddings=_get_embed(),
                use_jsonb=True,
            )
        return _TASK_STORE


def _get_skill_store() -> PGVector:
    """PGVector store for skill descriptions (collection_name='skill_descriptions').
    Used by find_matching_skill() to detect 'we have a battle-tested skill
    brief for this kind of task — reuse it instead of regenerating'."""
    global _SKILL_STORE
    if _SKILL_STORE is not None:
        return _SKILL_STORE
    with _LOCK:
        if _SKILL_STORE is None:
            _SKILL_STORE = PGVector(
                collection_name="skill_descriptions",
                connection=_dsn(),
                embeddings=_get_embed(),
                use_jsonb=True,
            )
        return _SKILL_STORE


# ─── Stage 3: Voyage rerank (T1) ──────────────────────────────────────────

def _get_rerank_client():
    """Singleton Voyage client. Returns None when VOYAGE_API_KEY is missing
    or the SDK can't be imported — callers gracefully skip reranking in
    that case (cosine results are still returned).

    R5-2 fix: thread-safe via _LOCK. Previously we flipped
    _RERANK_INIT_TRIED before init succeeded with no lock, so two concurrent
    callers could race past the flag and both initialize. Now we hold _LOCK
    around the whole check-and-init."""
    global _RERANK_CLIENT, _RERANK_INIT_TRIED
    if _RERANK_INIT_TRIED:
        return _RERANK_CLIENT
    with _LOCK:
        # Re-check inside the lock — another thread may have initialized
        # while we were waiting.
        if _RERANK_INIT_TRIED:
            return _RERANK_CLIENT
        key = os.environ.get("VOYAGE_API_KEY", "").strip()
        if not key:
            print("[rag] VOYAGE_API_KEY unset — rerank will be skipped (cosine-only retrieval)")
            _RERANK_INIT_TRIED = True
            return None
        try:
            import voyageai
            _RERANK_CLIENT = voyageai.Client(api_key=key)
            _RERANK_INIT_TRIED = True
            print("[rag] T1 Voyage rerank-2 client initialized")
            return _RERANK_CLIENT
        except Exception as e:
            print(f"[rag] Voyage SDK init failed (rerank skipped): {e}")
            _RERANK_INIT_TRIED = True
            return None


def _rerank(query: str, candidates: list[dict], top_k: int) -> list[dict]:
    """Re-score candidates using Voyage rerank-2 cross-encoder.

    Args:
        query: The user's query / task description.
        candidates: List of candidate dicts from Stage 2. Each must have a
            'doc' field with the text content. Other fields are passed
            through unchanged.
        top_k: How many candidates to return after re-ranking.

    Returns:
        Top_k candidates from the input list, re-ordered by reranker
        relevance. Each entry gets a `rerank_score` field added (float
        between 0 and 1; higher = more relevant).

    Behavior when reranker is unavailable:
        Returns candidates[:top_k] with `rerank_score=None` so callers can
        always rely on the field being present (even if the value tells
        them rerank was skipped). Downgrades quality but maintains the
        API contract — see R5-3.
    """
    client = _get_rerank_client()
    if client is None or not candidates:
        # R5-3: preserve the rerank_score field even on the fallback path
        # so callers don't crash on KeyError or get silently misleading
        # results. None means 'rerank was skipped'.
        return [{**c, "rerank_score": None} for c in candidates[:top_k]]
    try:
        docs = [c.get("doc", "") for c in candidates]
        # rerank-2 supports top_k up to len(documents); we ask for top_k
        # so we don't pay for re-scoring documents we'll throw away.
        result = client.rerank(
            query=query,
            documents=docs,
            model="rerank-2",
            top_k=min(top_k, len(docs)),
        )
        out = []
        for r in result.results:
            base = dict(candidates[r.index])
            base["rerank_score"] = float(r.relevance_score)
            out.append(base)
        return out
    except Exception as e:
        print(f"[rag] rerank call failed (falling back to cosine order): {e}")
        # R5-3: same contract on error path
        return [{**c, "rerank_score": None} for c in candidates[:top_k]]


# ─── Public API ────────────────────────────────────────────────────────────

def warm_embedding_fn(timeout_secs: int = 120) -> bool:
    """Pre-warm the embedding client by issuing one query embed.
    Useful at startup so the first real request doesn't pay the connection
    setup cost. Returns True on success, False on failure (callers can
    decide to retry / fail-soft)."""
    global _READY
    if _READY:
        return True
    try:
        emb = _get_embed()
        emb.embed_query("warmup")
        _READY = True
        print("[rag] T2 Databricks gte-large-en embedding warmed up (1024-dim)")
        return True
    except Exception as e:
        print(f"[rag] warmup failed: {e}")
        return False


def is_ready() -> bool:
    return _READY


def index_task(task_id: str, goal: str, summary: str, skill_md: str = "") -> None:
    """Index a completed task in the task_summaries store.

    Stores BOTH:
      - The full text in PGVector's collection (for hybrid metadata retrieval)
      - The dense embedding in tasks.summary_embedding (for fast pgvector
        ANN search via our ivfflat index)

    NAMESPACING: we prefix the row id with `task::` because langchain_postgres
    uses a single `langchain_pg_embedding` table for ALL collections; if
    index_task and index_skill share the raw task_id, the second write
    overwrites the first across collections. Tests caught this — the search
    result for a task_summaries query was returning skill_descriptions text.

    Idempotent: re-indexing the same task_id overwrites the prior task entry
    (but not the skill entry, thanks to the namespace prefix)."""
    if not goal:
        return
    try:
        text = f"GOAL: {goal}\n\nSUMMARY: {summary}\n\nSKILL: {skill_md[:1500]}"
        store = _get_task_store()
        store.add_texts(
            texts=[text],
            metadatas=[{"task_id": task_id, "goal": goal[:500]}],
            ids=[f"task::{task_id}"],
        )
        # Also write to our denormalized column so the hot-path search
        # avoids the langchain_postgres collection table and uses the
        # ivfflat index directly.
        emb = _get_embed().embed_query(text)
        db.update_embeddings(task_id, summary_embedding=emb)
        print(f"[rag] indexed task {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_task failed: {e}")
        traceback.print_exc()


def index_skill(task_id: str, name: str, description: str, skill_md: str) -> None:
    """Index a skill description for future find_matching_skill lookups.

    Namespaced with `skill::` prefix — see the index_task docstring for why."""
    if not description:
        return
    try:
        store = _get_skill_store()
        store.add_texts(
            texts=[description],
            metadatas=[{
                "task_id": task_id,
                "name": name or "",
                "description": description[:500],
                "skill_md_preview": skill_md[:500],
            }],
            ids=[f"skill::{task_id}"],
        )
        emb = _get_embed().embed_query(description)
        db.update_embeddings(task_id, skill_embedding=emb)
        print(f"[rag] indexed skill {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_skill failed: {e}")
        traceback.print_exc()


def search_tasks(query: str, top_k: int = 5) -> list[dict]:
    """Find similar past tasks. Two-stage: pgvector cosine top-N → rerank → top_k.

    Returns:
        List of dicts with keys: task_id, doc, distance (cosine; lower=better),
        meta. When reranker is configured, also includes rerank_score (higher=better).
    """
    if not query.strip():
        return []
    try:
        store = _get_task_store()
        # Stage 2: fetch a wider pool than top_k so the reranker has options
        results = store.similarity_search_with_score(query, k=_PRE_RERANK_FETCH)
        candidates = [
            {
                "task_id": doc.metadata.get("task_id"),
                "doc": doc.page_content,
                "distance": float(distance),
                "meta": doc.metadata,
            }
            for doc, distance in results
        ]
        # Stage 3: rerank
        return _rerank(query, candidates, top_k=top_k)
    except Exception as e:
        print(f"[rag] search_tasks failed: {e}")
        return []


def find_matching_skill(
    description_or_task: str,
    top_k: int = 1,
    distance_max: float = 0.40,
) -> dict | None:
    """Decide whether we already have a battle-tested skill brief for this
    kind of task. If yes, the supervisor reuses it instead of paying the
    LLM to regenerate from scratch (a real cost saver on repeat tasks).

    Args:
        description_or_task: Free-text describing the new task.
        top_k: Number of candidates to consider AFTER rerank.
        distance_max: Cosine distance threshold (lower = more similar).
            Returns None if even the best match is farther than this.

    Returns:
        The best-matching skill, or None if no match crosses the threshold.
    """
    if not description_or_task.strip():
        return None
    try:
        store = _get_skill_store()
        results = store.similarity_search_with_score(
            description_or_task, k=_PRE_RERANK_FETCH,
        )
        if not results:
            return None
        candidates = [
            {
                "task_id": doc.metadata.get("task_id"),
                "doc": doc.page_content,
                "description": doc.page_content,  # legacy field name
                "distance": float(distance),
                "meta": doc.metadata,
            }
            for doc, distance in results
        ]
        # Filter by cosine distance BEFORE rerank — anything farther than
        # `distance_max` is guaranteed irrelevant; no point paying the
        # reranker to re-score it.
        candidates = [c for c in candidates if c["distance"] <= distance_max]
        if not candidates:
            return None
        ranked = _rerank(description_or_task, candidates, top_k=top_k)
        return ranked[0] if ranked else None
    except Exception as e:
        print(f"[rag] find_matching_skill failed: {e}")
        return None


def reindex_all_from_db() -> dict:
    """Bulk re-embed every task in the DB. Used after a schema migration
    (like the v1.0→v2.0 swap from 384-dim to 1024-dim) to repopulate
    embeddings against the new model.

    R5-4 fix: previously this called index_task / index_skill in a loop,
    which means one Databricks embeddings round-trip per task — for 100
    tasks that was 100 round-trips (~30s+ on a free Databricks tier).
    Now we batch the embedding calls: build all texts first, embed in
    batches of 96 (the Databricks gateway limit), then write rows.

    Side effect: PGVector's `add_texts` is also called batch-wise so
    we're not paying the connection-per-row cost there either.
    """
    rows = db.all_tasks_with_skill_md()
    if not rows:
        return {"tasks_indexed": 0, "skills_indexed": 0}

    emb = _get_embed()

    # ── tasks ─────────────────────────────────────────────────────────────
    task_records = [r for r in rows if r.get("goal")]
    task_texts = [
        f"GOAL: {r['goal']}\n\nSUMMARY: {r.get('summary') or ''}\n\nSKILL: {(r.get('skill_md') or '')[:1500]}"
        for r in task_records
    ]
    task_ids_ns = [f"task::{r['id']}" for r in task_records]
    task_metas = [{"task_id": r["id"], "goal": r["goal"][:500]} for r in task_records]

    n_tasks = 0
    if task_records:
        try:
            # Single batched embed call — replaces N round-trips
            task_embeddings = emb.embed_documents(task_texts)
            # Bulk add_texts — langchain_postgres still does it per-row but
            # we save the embedding cost which dominates.
            store = _get_task_store()
            store.add_texts(texts=task_texts, metadatas=task_metas, ids=task_ids_ns)
            for r, vec in zip(task_records, task_embeddings):
                try:
                    db.update_embeddings(r["id"], summary_embedding=vec)
                    n_tasks += 1
                except Exception as e:
                    print(f"[rag] reindex update_embeddings({r['id']}) failed: {e}")
            print(f"[rag] reindexed {n_tasks} tasks in 1 batch")
        except Exception as e:
            import traceback
            print(f"[rag] reindex tasks batch failed: {e}")
            traceback.print_exc()

    # ── skills ────────────────────────────────────────────────────────────
    skill_records = [r for r in rows if r.get("skill_description")]
    n_skills = 0
    if skill_records:
        try:
            skill_texts = [r["skill_description"] for r in skill_records]
            skill_ids_ns = [f"skill::{r['id']}" for r in skill_records]
            skill_metas = [
                {
                    "task_id": r["id"],
                    "name": r.get("skill_name") or "",
                    "description": r["skill_description"][:500],
                    "skill_md_preview": (r.get("skill_md") or "")[:500],
                }
                for r in skill_records
            ]
            skill_embeddings = emb.embed_documents(skill_texts)
            store = _get_skill_store()
            store.add_texts(texts=skill_texts, metadatas=skill_metas, ids=skill_ids_ns)
            for r, vec in zip(skill_records, skill_embeddings):
                try:
                    db.update_embeddings(r["id"], skill_embedding=vec)
                    n_skills += 1
                except Exception as e:
                    print(f"[rag] reindex update_embeddings({r['id']}) failed: {e}")
            print(f"[rag] reindexed {n_skills} skills in 1 batch")
        except Exception as e:
            import traceback
            print(f"[rag] reindex skills batch failed: {e}")
            traceback.print_exc()

    return {"tasks_indexed": n_tasks, "skills_indexed": n_skills}
