"""v2.0 Phase 1 regression tests — RAG pipeline (T1 rerank + T2 embeddings) +
the 4 round-5 fixes:

  R5-1 DatabricksEmbeddings retries on transient errors (RateLimitError, conn,
       5xx) instead of failing on the first blip.
  R5-2 _get_rerank_client is thread-safe — concurrent callers don't double-init.
  R5-3 _rerank ALWAYS returns rerank_score field (None when reranker skipped),
       so callers don't break on KeyError or get silent inconsistency.
  R5-4 reindex_all_from_db batches embed calls instead of one-per-task.

These tests don't talk to real Databricks/Voyage — they mock the clients so
the suite is fast, deterministic, and safe in CI without secrets.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401


def main():
    print("=" * 60)
    print("RAG PIPELINE REGRESSION (v2.0 Phase 1)")
    print("=" * 60)

    import rag

    # ── R5-1: DatabricksEmbeddings retry on transient errors ──────────────
    from openai import APIConnectionError, RateLimitError
    fake_resp = MagicMock(); fake_resp.data = [MagicMock(embedding=[0.1] * 1024)]
    rate_err = RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    conn_err = APIConnectionError(request=MagicMock())

    fake_client = MagicMock()
    fake_client.embeddings.create.side_effect = [rate_err, conn_err, fake_resp]
    emb = rag.DatabricksEmbeddings()
    emb._client = fake_client

    # Speed up retries in test
    import time as _t
    orig_sleep = _t.sleep
    _t.sleep = lambda s: None
    try:
        result = emb.embed_query("hello")
    finally:
        _t.sleep = orig_sleep
    assert len(result) == 1024
    assert fake_client.embeddings.create.call_count == 3
    print("  ✓ R5-1: 1 RateLimitError + 1 ConnectionError → retried, succeeded on attempt 3")

    # 4 consecutive 429s → raises (no infinite retry)
    fake_client.embeddings.create.reset_mock()
    fake_client.embeddings.create.side_effect = [rate_err] * 4
    _t.sleep = lambda s: None
    try:
        try:
            emb.embed_query("hello")
            raise AssertionError("expected RateLimitError after 4 attempts")
        except RateLimitError:
            assert fake_client.embeddings.create.call_count == 4
            print("  ✓ R5-1: 4× 429 → raises after 4 attempts (no infinite retry)")
    finally:
        _t.sleep = orig_sleep

    # Empty input → ValueError, no API call
    fake_client.embeddings.create.reset_mock()
    try:
        emb.embed_documents(["", "   ", ""])
        raise AssertionError("expected ValueError for all-empty input")
    except ValueError:
        assert fake_client.embeddings.create.call_count == 0
        print("  ✓ R5-1: all-empty input → ValueError, no API call")

    # ── R5-2: _get_rerank_client thread-safe (concurrent callers) ─────────
    rag._RERANK_CLIENT = None
    rag._RERANK_INIT_TRIED = False
    init_calls = {"n": 0}

    class _FakeVoyageClient:
        def __init__(self, *a, **kw):
            init_calls["n"] += 1

    fake_voyageai = MagicMock()
    fake_voyageai.Client = _FakeVoyageClient
    with patch.dict("sys.modules", {"voyageai": fake_voyageai}):
        import threading
        clients = []

        def _worker():
            clients.append(rag._get_rerank_client())

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert init_calls["n"] == 1, f"client must init exactly once; got {init_calls['n']}"
    assert all(c is clients[0] for c in clients), "all threads must see the same singleton"
    print(f"  ✓ R5-2: 8 concurrent _get_rerank_client() calls → 1 init, 1 singleton")

    # ── R5-3: _rerank fallback preserves rerank_score field ───────────────
    rag._RERANK_CLIENT = None
    rag._RERANK_INIT_TRIED = True  # force "no client" path
    candidates = [
        {"task_id": "a", "doc": "doc A", "distance": 0.1},
        {"task_id": "b", "doc": "doc B", "distance": 0.2},
    ]
    out = rag._rerank("query", candidates, top_k=2)
    assert all("rerank_score" in c for c in out), \
        "rerank_score field must be present even when reranker is skipped"
    assert all(c["rerank_score"] is None for c in out), \
        "rerank_score must be None to signal 'rerank skipped'"
    print("  ✓ R5-3: skipped-rerank path always includes rerank_score=None")

    # When rerank succeeds, rerank_score is a float
    fake_client = MagicMock()
    fake_result = MagicMock()
    fake_result.results = [
        MagicMock(index=0, relevance_score=0.9),
        MagicMock(index=1, relevance_score=0.4),
    ]
    fake_client.rerank.return_value = fake_result
    rag._RERANK_CLIENT = fake_client
    rag._RERANK_INIT_TRIED = True
    out = rag._rerank("query", candidates, top_k=2)
    assert all(isinstance(c["rerank_score"], float) for c in out)
    print("  ✓ R5-3: successful rerank → rerank_score is a float")

    # When rerank raises, fallback STILL preserves the field
    fake_client.rerank.side_effect = Exception("voyage 503")
    out = rag._rerank("query", candidates, top_k=2)
    assert all("rerank_score" in c and c["rerank_score"] is None for c in out)
    print("  ✓ R5-3: failed-rerank path also includes rerank_score=None (no KeyError for callers)")

    # ── R5-4: reindex_all_from_db batches embed calls ─────────────────────
    # Mock 25 task rows; verify embed_documents is called with batch (not 25 individual times)
    rag._EMBED = None
    fake_emb = MagicMock()
    fake_emb.embed_documents.return_value = [[0.1] * 1024] * 25

    # Mock store.add_texts so it doesn't hit the real DB
    fake_store = MagicMock()

    # Mock db helpers
    fake_rows = [
        {"id": f"t{i}", "goal": f"goal {i}", "summary": f"sum {i}", "skill_md": "", "skill_description": ""}
        for i in range(25)
    ]
    with patch.object(rag, "_get_embed", return_value=fake_emb), \
         patch.object(rag, "_get_task_store", return_value=fake_store), \
         patch.object(rag, "_get_skill_store", return_value=fake_store), \
         patch("db.all_tasks_with_skill_md", return_value=fake_rows), \
         patch("db.update_embeddings"):
        result = rag.reindex_all_from_db()

    # The KEY assertion: embed_documents called ONCE for tasks (batch),
    # not 25 separate times.
    embed_calls = fake_emb.embed_documents.call_count
    assert embed_calls == 1, \
        f"embed_documents must be called once (batched); got {embed_calls} calls"
    # And the batch contained all 25 task texts
    args, _ = fake_emb.embed_documents.call_args
    assert len(args[0]) == 25, f"batch should contain 25 texts; got {len(args[0])}"
    assert result["tasks_indexed"] == 25
    print(f"  ✓ R5-4: reindex of 25 tasks = 1 batched embed call (was 25 sequential before)")

    print()
    print("=" * 60)
    print("RAG PIPELINE TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
