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
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401

# CI fix: this test mocks both Databricks (embeddings) and Voyage (rerank)
# clients, so the real env vars aren't needed. But the DatabricksEmbeddings
# constructor (after R6-2) raises RuntimeError if DATABRICKS_TOKEN /
# DATABRICKS_BASE_URL are missing — which they ARE in CI without secrets.
# Inject dummy values that satisfy the truthiness check; the mocked client
# will replace the real one before any HTTP call is made.
os.environ.setdefault("DATABRICKS_TOKEN", "test-token-not-real")
os.environ.setdefault("DATABRICKS_BASE_URL", "http://test-base-url.invalid")


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

    # NOTE: prior R5-1 case `all-empty → ValueError` was removed — R6-1
    # supersedes it. Empty inputs are now substituted with ' ' placeholders
    # to preserve input/output ordering. See the R6-1 case below.

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

    # ── R6-1: empty inputs preserve ordering (no batch misalignment) ──────
    rag._EMBED = None
    fake_resp = MagicMock()
    fake_resp.data = [
        MagicMock(embedding=[0.1] * 1024),
        MagicMock(embedding=[0.2] * 1024),
        MagicMock(embedding=[0.3] * 1024),
    ]
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = fake_resp
    emb = rag.DatabricksEmbeddings()
    emb._client = fake_client
    # Caller passes 3 strings, one empty
    out = emb.embed_documents(["real text 1", "", "real text 2"])
    assert len(out) == 3, f"R6-1: must return 3 vectors for 3 inputs (one empty); got {len(out)}"
    # Inspect the API call — should have substituted ' ' for the empty
    args, _ = fake_client.embeddings.create.call_args
    sent = fake_client.embeddings.create.call_args.kwargs.get("input") or args[0] if args else None
    if sent is None:
        sent = fake_client.embeddings.create.call_args.kwargs["input"]
    assert len(sent) == 3, "API call should still receive 3 inputs (empty replaced with placeholder)"
    print(f"  ✓ R6-1: 3 inputs (1 empty) → 3 vectors, ordering preserved (no metadata misalignment)")

    # Output count mismatch raises loudly (not silently corrupts)
    fake_resp_short = MagicMock()
    fake_resp_short.data = [MagicMock(embedding=[0.0] * 1024)]  # 1 vec for 2 inputs
    fake_client.embeddings.create.return_value = fake_resp_short
    try:
        emb.embed_documents(["a", "b"])
        raise AssertionError("expected RuntimeError on output count mismatch")
    except RuntimeError as e:
        assert "would corrupt" in str(e)
        print("  ✓ R6-1: API count mismatch raises RuntimeError (no silent corruption)")

    # ── R6-2: missing creds → clear RuntimeError, not KeyError ────────────
    saved = (os.environ.get("DATABRICKS_TOKEN"), os.environ.get("DATABRICKS_BASE_URL"))
    try:
        os.environ.pop("DATABRICKS_TOKEN", None)
        os.environ.pop("DATABRICKS_BASE_URL", None)
        try:
            rag.DatabricksEmbeddings()
            raise AssertionError("expected RuntimeError for missing creds")
        except RuntimeError as e:
            assert "DATABRICKS_TOKEN" in str(e) and ".env" in str(e)
            print("  ✓ R6-2: missing DATABRICKS_TOKEN → clear actionable RuntimeError")
        except KeyError:
            raise AssertionError("regressed to bare KeyError")
    finally:
        if saved[0]: os.environ["DATABRICKS_TOKEN"] = saved[0]
        if saved[1]: os.environ["DATABRICKS_BASE_URL"] = saved[1]

    # ── R6-3: rerank_min threshold blocks low-relevance matches ──────────
    fake_voyage = MagicMock()
    fake_voyage_result = MagicMock()
    fake_voyage_result.results = [MagicMock(index=0, relevance_score=0.05)]  # very low!
    fake_voyage.rerank.return_value = fake_voyage_result
    rag._RERANK_CLIENT = fake_voyage
    rag._RERANK_INIT_TRIED = True

    fake_pgv_result = [
        (MagicMock(page_content="weakly related doc", metadata={"task_id": "t99"}), 0.30),
    ]
    fake_skill_store = MagicMock()
    fake_skill_store.similarity_search_with_score.return_value = fake_pgv_result
    with patch.object(rag, "_get_skill_store", return_value=fake_skill_store):
        # rerank_min=0.30 → 0.05 should be rejected
        m = rag.find_matching_skill("query", top_k=1, distance_max=0.40, rerank_min=0.30)
        assert m is None, f"R6-3: rerank=0.05 below threshold 0.30 must reject, got {m}"
        print("  ✓ R6-3: rerank_min threshold rejects low-relevance candidates")

        # rerank_min=0.0 (disabled) → should accept
        m = rag.find_matching_skill("query", top_k=1, distance_max=0.40, rerank_min=0.0)
        assert m is not None
        print("  ✓ R6-3: rerank_min=0.0 disables the second-stage filter (back-compat)")

    # ── R6-4: index_task atomic dual-write — rollback on partial failure ──
    rollback_calls = []

    def _fake_psycopg2_connect(*args, **kwargs):
        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def cursor(self):
                class _Cur:
                    def __enter__(self): return self
                    def __exit__(self, *a): pass
                    def execute(self, sql, params):
                        rollback_calls.append((sql, params))
                return _Cur()
        return _Conn()

    fake_emb = MagicMock()
    fake_emb.embed_query.return_value = [0.1] * 1024
    fake_store = MagicMock()
    fake_store.add_texts.side_effect = Exception("simulated PGVector failure")
    update_called = []
    with patch.object(rag, "_get_embed", return_value=fake_emb), \
         patch.object(rag, "_get_task_store", return_value=fake_store), \
         patch("db.update_embeddings", side_effect=lambda *a, **kw: update_called.append((a, kw))), \
         patch("psycopg2.connect", side_effect=_fake_psycopg2_connect):
        rag.index_task("t_partial", "goal", "summary")

    assert len(update_called) == 1, "denormalized column write must run"
    rollback_sqls = [c[0] for c in rollback_calls]
    assert any("summary_embedding = NULL" in s for s in rollback_sqls), \
        "R6-4: rollback must clear summary_embedding when PGVector write fails"
    print("  ✓ R6-4: index_task partial failure → rolled back denormalized column to NULL")

    print()
    print("=" * 60)
    print("RAG PIPELINE TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
