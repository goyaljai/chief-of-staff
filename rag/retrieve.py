"""Retrieval — the actual two-stage pgvector + rerank pipeline.

Public API:
  search_tasks(query, top_k=5)              — used by /ask history-wide search
  find_matching_skill(query, top_k, ...)    — used by orchestrator to decide
                                              whether to reuse a battle-tested
                                              skill brief vs. regenerate

R6-3 fix (find_matching_skill): a candidate that squeaks past the cosine
filter at distance 0.39 might rerank at 0.05 — clearly irrelevant — and
prior code returned it anyway. We now require rerank_score >= rerank_min;
candidates where rerank was skipped (rerank_score=None) bypass this check
because we have no signal to gate on.
"""
from ._stores import _get_task_store, _get_skill_store, _PRE_RERANK_FETCH
from .rerank import _rerank


def search_tasks(query: str, top_k: int = 5) -> list[dict]:
    """Find similar past tasks. pgvector cosine top-N → rerank → top_k.

    Returns a list of dicts with keys: task_id, doc, distance (cosine,
    lower=better), meta. When reranker is configured, also includes
    rerank_score (higher=better).
    """
    if not query.strip():
        return []
    try:
        store = _get_task_store()
        # Stage 2: fetch a wider pool than top_k so the reranker has options.
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
        description_or_task: free-text describing the new task.
        top_k: number of candidates to consider AFTER rerank.
        distance_max: cosine distance threshold (lower = more similar).
            First-stage filter; anything farther is guaranteed irrelevant.
        rerank_min: minimum rerank_score to accept (R6-3). Set to 0.0 to
            disable the second-stage filter. Reranker-skipped candidates
            (rerank_score=None) bypass this check.

    Returns:
        Best-matching skill, or None if no match crosses both thresholds.
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
        # Stage 3 (R6-3): rerank_score floor.
        best = ranked[0]
        rscore = best.get("rerank_score")
        if rscore is not None and rscore < rerank_min:
            print(
                f"[rag] find_matching_skill: best rerank={rscore:.3f} "
                f"< threshold {rerank_min} — skipping"
            )
            return None
        return best
    except Exception as e:
        print(f"[rag] find_matching_skill failed: {e}")
        return None
