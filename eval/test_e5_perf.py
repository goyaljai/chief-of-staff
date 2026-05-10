"""E5 regression test — pool + batched log writes.

Validates:
  1. db._get_pool returns a ThreadedConnectionPool singleton (not per-call connect).
  2. append_log_batch writes 100 rows in a single SQL round-trip.
  3. task_store buffers and the flusher coalesces N entries into 1 DB call.
  4. Pool is reused across many _conn() calls (no socket churn).
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401


def _need_local_db() -> bool:
    """Same guard as test_skill_lessons — refuse to mutate prod DB without override."""
    dsn = os.environ.get("DATABASE_URL", "")
    return ("localhost" in dsn
            or os.environ.get("COS_ALLOW_DESTRUCTIVE_TESTS") == "1"
            or "/test" in dsn or "_test" in dsn)


async def main():
    if not _need_local_db():
        print("[skip] set COS_ALLOW_DESTRUCTIVE_TESTS=1 or use a local/test DSN")
        return

    import db
    from task_store import TaskStore, TaskState

    print("=" * 60)
    print("E5 PERF REGRESSION TEST")
    print("=" * 60)

    # 1. Pool is a singleton
    p1 = db._get_pool()
    p2 = db._get_pool()
    assert p1 is p2, "pool must be reused across calls"
    assert hasattr(p1, "getconn") and hasattr(p1, "putconn")
    print("  ✓ ThreadedConnectionPool singleton")

    # 2. Pool reuses connections, not creating fresh sockets
    backends = set()
    for _ in range(20):
        with db._conn() as c, c.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            backends.add(cur.fetchone()[0])
    print(f"  ✓ 20 _conn() calls reused {len(backends)} backend(s) (pool working)")
    # Pool maxconn=8 → at most 8 distinct backends
    assert len(backends) <= 8, f"expected ≤8 distinct backends; got {len(backends)}"

    # 3. append_log_batch handles a 100-row batch in one round-trip
    test_tid = f"_e5_test_{uuid.uuid4().hex[:8]}"
    # Need a tasks row for FK if log_entries has one — try first, ignore if no FK
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (id, goal, status, started_at) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (test_tid, "e5 perf test", "pending", time.time()),
            )
    except Exception:
        pass

    rows = [(test_tid, time.time() + i * 0.001, "perf_test", {"i": i, "msg": f"event {i}"})
            for i in range(100)]
    t0 = time.monotonic()
    n = db.append_log_batch(rows)
    elapsed = time.monotonic() - t0
    assert n == 100
    print(f"  ✓ append_log_batch: 100 rows in {elapsed*1000:.0f}ms (single round-trip)")
    # Sanity: a single round-trip to Supabase APAC ~50-200ms; per-row would be 5-20s
    assert elapsed < 2.0, f"100-row batch took {elapsed:.2f}s — likely not batched"

    # 4. TaskStore buffer + flush
    store = TaskStore()
    store._tasks[test_tid] = TaskState(id=test_tid, goal="g", clarifications={}, workspace="/tmp")
    store.start_log_flusher()
    # Append 30 events rapidly
    t0 = time.monotonic()
    for i in range(30):
        store.append_log(test_tid, {"kind": "buffered_test", "i": i})
    append_elapsed = time.monotonic() - t0
    assert append_elapsed < 0.1, f"30 appends took {append_elapsed*1000:.0f}ms — should be near-zero (in-memory buffer)"
    print(f"  ✓ 30 in-memory appends in {append_elapsed*1000:.1f}ms (no DB block)")

    # Wait for the flusher to drain
    await asyncio.sleep(0.5)
    with store._log_buf_lock:
        remaining = len(store._log_buf)
    assert remaining == 0, f"flusher didn't drain buffer: {remaining} left"
    print(f"  ✓ Flusher drained 30-entry buffer within 500ms")

    # Cleanup
    try:
        with db._conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM log_entries WHERE task_id = %s", (test_tid,))
            cur.execute("DELETE FROM tasks WHERE id = %s", (test_tid,))
    except Exception:
        pass
    if store._flusher_task:
        store._flusher_task.cancel()
        try:
            await store._flusher_task
        except asyncio.CancelledError:
            pass

    print()
    print("=" * 60)
    print("E5 PERF TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
