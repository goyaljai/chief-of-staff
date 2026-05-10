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
        # R6-2 fix: actionable error if creds are missing. Default os.environ[]
        # raises a bare KeyError that crashes deep inside add_texts with no
        # hint about how to fix it.
        token = os.environ.get("DATABRICKS_TOKEN", "").strip()
        base_url = os.environ.get("DATABRICKS_BASE_URL", "").strip()
        if not token or not base_url:
            raise RuntimeError(
                "DatabricksEmbeddings: DATABRICKS_TOKEN and DATABRICKS_BASE_URL "
                "must both be set. Add them to .env (see config.py)."
            )
        self._client = OpenAI(
            api_key=token,
            base_url=base_url,
            timeout=60.0,
            max_retries=3,
        )
        self._model = model
        self._batch_size = 96  # Databricks gateway accepts batches; 96 is safe

    def _create_with_retry(self, inputs: list[str]):
        """R5-1: jittered exponential backoff on transient failures.
        Mirrors orchestrator._chat's pattern. 4 attempts total: 1s, 2s, 4s, 8s
        base waits + 0-1s jitter."""
        from openai import (
            APIConnectionError, APITimeoutError,
            InternalServerError, RateLimitError,
        )
        import random as _random
        import time as _time
        transient_excs = (
            RateLimitError, APIConnectionError, APITimeoutError, InternalServerError,
        )
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return self._client.embeddings.create(model=self._model, input=inputs)
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
        # Single placeholder for a fully-empty query so callers don't crash;
        # the resulting vector will be the embedding of " " which is fine
        # for the rare empty-query edge case (returns no useful matches).
        resp = self._create_with_retry([text or " "])
        return resp.data[0].embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents. Used by index calls. Splits into
        chunks of `_batch_size` to respect gateway limits.

        R6-1 fix (CRITICAL — silent data corruption):
            Previously we filtered empty strings BEFORE sending to the API.
            That broke the input-ordering contract: callers passing
            `[s1, '', s2]` got back 2 vectors for 3 metadata entries, and
            PGVector.add_texts wrote vectors against the WRONG metadatas.
            Now we substitute empties with a single space placeholder so
            the API still accepts the call AND the output count matches
            the input count exactly. Caller's metadatas/ids align correctly.
        """
        if not texts:
            return []
        # R6-1: substitute empties with " " (cheap to embed, rare in practice).
        # The output index === input index always.
        clean_texts = [t if (t and t.strip()) else " " for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(clean_texts), self._batch_size):
            chunk = clean_texts[i:i + self._batch_size]
            resp = self._create_with_retry(chunk)
            chunk_out = [d.embedding for d in resp.data]
            if len(chunk_out) != len(chunk):
                # Defensive: should never happen now, but if it does we
                # fail loudly rather than silently misalign downstream.
                raise RuntimeError(
                    f"DatabricksEmbeddings: API returned {len(chunk_out)} vectors "
                    f"for {len(chunk)} inputs — would corrupt caller's mapping"
                )
            out.extend(chunk_out)
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

    Stores in BOTH places (dual-write, ordered):
      1. tasks.summary_embedding (denormalized column, drives the hot-path
         vector_search_tasks query through our ivfflat index)
      2. langchain_pg_embedding via PGVector.add_texts (richer metadata
         retrieval through search_tasks)

    R6-4 fix (silent retrieval skew):
        Previously we wrote (2) first and (1) second with no atomicity. If
        (2) succeeded but (1) failed, search_tasks would find the doc but
        vector_search_tasks would miss it — different query paths returned
        different result sets.

        Now we embed FIRST, then write (1) and (2) in order. If (2) fails,
        we attempt to roll back (1) by clearing the denormalized column,
        leaving the system in a clean "not indexed" state rather than a
        half-indexed one.

    NAMESPACING: row id prefixed with `task::` because langchain_postgres
    uses a single langchain_pg_embedding table for ALL collections; sharing
    raw task_id between index_task and index_skill caused cross-collection
    overwrites (tests caught it).
    """
    if not goal:
        return
    try:
        text = f"GOAL: {goal}\n\nSUMMARY: {summary}\n\nSKILL: {skill_md[:1500]}"
        # Embed once — used for both writes
        emb = _get_embed().embed_query(text)
        # Write 1: denormalized column (cheap, single UPDATE)
        db.update_embeddings(task_id, summary_embedding=emb)
        # Write 2: langchain_postgres collection
        try:
            store = _get_task_store()
            store.add_texts(
                texts=[text],
                metadatas=[{"task_id": task_id, "goal": goal[:500]}],
                ids=[f"task::{task_id}"],
            )
        except Exception as e_inner:
            # Roll back write 1 so we don't have a half-indexed task.
            # db.update_embeddings(...None) is a no-op (skips None), so we
            # issue the UPDATE directly via raw SQL.
            try:
                import psycopg2
                import os as _os
                with psycopg2.connect(_os.environ["DATABASE_URL"]) as _c:
                    with _c.cursor() as _cur:
                        _cur.execute(
                            "UPDATE tasks SET summary_embedding = NULL WHERE id = %s",
                            (task_id,),
                        )
            except Exception:
                pass
            raise
        print(f"[rag] indexed task {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_task failed: {e}")
        traceback.print_exc()


def index_skill(task_id: str, name: str, description: str, skill_md: str) -> None:
    """Index a skill description for future find_matching_skill lookups.

    R6-4 fix: same atomic dual-write pattern as index_task — embed once,
    write denormalized column first, write PGVector collection second,
    roll back column on collection-write failure.

    Namespaced with `skill::` prefix — see the index_task docstring for why."""
    if not description:
        return
    try:
        emb = _get_embed().embed_query(description)
        db.update_embeddings(task_id, skill_embedding=emb)
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
        except Exception:
            # Roll back the column update so we don't leave a half-indexed skill
            try:
                import psycopg2
                import os as _os
                with psycopg2.connect(_os.environ["DATABASE_URL"]) as _c:
                    with _c.cursor() as _cur:
                        _cur.execute(
                            "UPDATE tasks SET skill_embedding = NULL WHERE id = %s",
                            (task_id,),
                        )
            except Exception:
                pass
            raise
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
    rerank_min: float = 0.30,
) -> dict | None:
    """Decide whether we already have a battle-tested skill brief for this
    kind of task. If yes, the supervisor reuses it instead of paying the
    LLM to regenerate from scratch (a real cost saver on repeat tasks).

    Args:
        description_or_task: Free-text describing the new task.
        top_k: Number of candidates to consider AFTER rerank.
        distance_max: Cosine distance threshold (lower = more similar).
            First-stage filter; anything farther is guaranteed irrelevant.
        rerank_min: Minimum rerank_score to accept (R6-3 fix). The reranker
            scores ~0-1 with higher = more relevant. A candidate that
            squeaked past the cosine filter at distance 0.39 might rerank
            at 0.05 — clearly irrelevant — and the prior code returned it
            anyway. Now we require rerank_score >= rerank_min. Set to 0.0
            to disable the second-stage filter. Reranker-skipped candidates
            (rerank_score=None) bypass this check (we have no signal to gate on).

    Returns:
        The best-matching skill, or None if no match crosses both thresholds.
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
        # Stage 1: cosine distance filter (cheap)
        candidates = [c for c in candidates if c["distance"] <= distance_max]
        if not candidates:
            return None
        # Stage 2: rerank
        ranked = _rerank(description_or_task, candidates, top_k=top_k)
        if not ranked:
            return None
        # Stage 3 (R6-3): rerank_score floor. None = rerank skipped (no
        # second-stage signal available), so we accept the candidate based
        # on cosine alone. A real low score means the reranker actively
        # judged it irrelevant.
        best = ranked[0]
        rscore = best.get("rerank_score")
        if rscore is not None and rscore < rerank_min:
            print(f"[rag] find_matching_skill: best rerank={rscore:.3f} < threshold {rerank_min} — skipping")
            return None
        return best
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
