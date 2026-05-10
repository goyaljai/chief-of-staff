"""Stage 3: Voyage rerank-2 cross-encoder.

After pgvector cosine search returns ~20 candidates (the pre-rerank fetch
size in `_stores._PRE_RERANK_FETCH`), this module re-scores them with a
cross-encoder that reads the actual (query, doc) pair text. Empirically
+15-30% retrieval accuracy on standard RAG benchmarks vs cosine alone.

Cleanly degrades to "skip rerank" when VOYAGE_API_KEY is unset — callers
still get top_k cosine candidates, just without the second-stage signal.

Module state (singletons via _RERANK_LOCK):
  _RERANK_CLIENT     — voyageai.Client instance (or None)
  _RERANK_INIT_TRIED — flag so we don't repeat the import-and-init dance
                       on every call when the SDK is missing or the key
                       is unset

R-fixes baked in:
  R5-2 — thread-safe init via lock + double-checked condition (we used to
         flip the tried-flag before init success — two concurrent callers
         could race past it and both attempt initialization)
  R5-3 — every returned candidate ALWAYS carries a `rerank_score` field
         (None when rerank was skipped). Callers always know whether they
         got a real signal vs a fallback ordering, and can't crash on a
         missing key.
"""
import os
import threading


_RERANK_LOCK = threading.RLock()
_RERANK_CLIENT = None
_RERANK_INIT_TRIED = False


def _get_rerank_client():
    """Singleton Voyage client. Returns None when VOYAGE_API_KEY is missing
    or the SDK can't be imported — callers gracefully skip reranking in
    that case (cosine results are still returned).

    R5-2: thread-safe via _RERANK_LOCK with a double-checked condition.
    Previously we flipped _RERANK_INIT_TRIED before init succeeded with
    no lock, so two concurrent callers could race past the flag and both
    initialize.
    """
    global _RERANK_CLIENT, _RERANK_INIT_TRIED
    if _RERANK_INIT_TRIED:
        return _RERANK_CLIENT
    with _RERANK_LOCK:
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
        query: the user's query / task description.
        candidates: list of candidate dicts from pgvector. Each must have a
            'doc' field with the text content. Other fields pass through.
        top_k: how many candidates to return after re-ranking.

    Returns:
        Top_k candidates from the input list, re-ordered by reranker
        relevance. Each entry gets a `rerank_score` field added (float
        between 0 and 1; higher = more relevant).

    Behaviour when reranker is unavailable:
        Returns candidates[:top_k] with `rerank_score=None` so callers can
        always rely on the field being present (R5-3 contract). None means
        "rerank was skipped"; downgrades quality but maintains the API.
    """
    client = _get_rerank_client()
    if client is None or not candidates:
        # R5-3: preserve the rerank_score field on the fallback path so
        # callers don't crash on KeyError or get silently misleading data.
        return [{**c, "rerank_score": None} for c in candidates[:top_k]]
    try:
        docs = [c.get("doc", "") for c in candidates]
        # rerank-2 accepts top_k up to len(documents); we ask for top_k so
        # we don't pay for re-scoring documents we'll throw away.
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
        # R5-3: same contract on error path.
        return [{**c, "rerank_score": None} for c in candidates[:top_k]]
