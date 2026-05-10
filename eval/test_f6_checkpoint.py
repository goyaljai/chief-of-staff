"""F6 regression test — DAG checkpoint + resume.

Validates:
  1. AsyncPostgresSaver is wired (checkpoint tables exist).
  2. After a successful DAG run, checkpoints are persisted under thread_id.
  3. Resuming with the SAME thread_id returns immediately (no re-run) because
     LangGraph sees the END state already cached.
  4. A NEW thread_id triggers a fresh run.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401


def _need_local_db() -> bool:
    dsn = os.environ.get("DATABASE_URL", "")
    return ("localhost" in dsn
            or os.environ.get("COS_ALLOW_DESTRUCTIVE_TESTS") == "1"
            or "/test" in dsn or "_test" in dsn)


class _FakeResult:
    def __init__(self, success=True, output="ok", session_id="s"):
        self.success = success
        self.output = output
        self.session_id = session_id


class _CountingRunner:
    runs = 0

    def __init__(self, working_dir, hook_log_path):
        pass

    async def run(self, prompt, timeout_secs=600, on_event=None):
        _CountingRunner.runs += 1
        return _FakeResult(success=True, output=f"run {_CountingRunner.runs}")


async def main():
    if not _need_local_db():
        print("[skip] set COS_ALLOW_DESTRUCTIVE_TESTS=1 or use a local/test DSN")
        return

    import workflows.dag as dag_executor
    print("=" * 60)
    print("F6 CHECKPOINT + RESUME TEST")
    print("=" * 60)

    # 1. Checkpointer is configured
    cp = await dag_executor.graph._get_async_checkpointer()
    assert cp is not None, "AsyncPostgresSaver must be configured (DATABASE_URL set)"
    print(f"  ✓ AsyncPostgresSaver wired ({type(cp).__name__})")

    # Replace ClaudeRunner with the counting fake
    dag_executor.nodes.ClaudeRunner = _CountingRunner
    dag_executor.graph._GRAPH = None  # bust cache so it picks up the new runner

    # 2. First run with a fresh thread_id — should run all 3 steps
    thread_id = f"f6_test_{uuid.uuid4().hex[:8]}"
    workspace = Path("/tmp/cos-f6-test") / thread_id
    workspace.mkdir(parents=True, exist_ok=True)
    _CountingRunner.runs = 0
    out1 = await dag_executor.execute_dag(
        steps=[
            {"id": "alpha", "action": "x", "depends_on": []},
            {"id": "beta", "action": "y", "depends_on": ["alpha"]},
            {"id": "gamma", "action": "z", "depends_on": ["beta"]},
        ],
        task_workspace=workspace,
        hook_log_dir=workspace / "_hooks",
        exec_id=thread_id,
    )
    assert out1["ok"], f"first run failed: {out1}"
    runs_first = _CountingRunner.runs
    assert runs_first == 3, f"expected 3 runs first time; got {runs_first}"
    print(f"  ✓ First run with thread_id={thread_id[:12]}…: 3 step_node invocations")

    # 3. Second run with SAME thread_id — checkpointed, should NOT re-execute
    _CountingRunner.runs = 0
    out2 = await dag_executor.execute_dag(
        steps=[
            {"id": "alpha", "action": "x", "depends_on": []},
            {"id": "beta", "action": "y", "depends_on": ["alpha"]},
            {"id": "gamma", "action": "z", "depends_on": ["beta"]},
        ],
        task_workspace=workspace,
        hook_log_dir=workspace / "_hooks",
        exec_id=thread_id,  # SAME thread → resume / no re-execute
    )
    assert out2["ok"], f"resume run failed: {out2}"
    runs_second = _CountingRunner.runs
    # LangGraph should NOT re-execute completed steps.
    assert runs_second == 0, \
        f"expected 0 re-runs (checkpoint should skip done work); got {runs_second}"
    print(f"  ✓ Second invoke same thread_id: 0 re-runs (resumed from checkpoint)")

    # 4. New thread_id → fresh run, 3 invocations
    fresh_thread = f"f6_test_{uuid.uuid4().hex[:8]}"
    fresh_ws = Path("/tmp/cos-f6-test") / fresh_thread
    fresh_ws.mkdir(parents=True, exist_ok=True)
    _CountingRunner.runs = 0
    out3 = await dag_executor.execute_dag(
        steps=[
            {"id": "alpha", "action": "x", "depends_on": []},
            {"id": "beta", "action": "y", "depends_on": ["alpha"]},
        ],
        task_workspace=fresh_ws,
        hook_log_dir=fresh_ws / "_hooks",
        exec_id=fresh_thread,
    )
    assert out3["ok"]
    assert _CountingRunner.runs == 2, f"new thread should run 2 fresh; got {_CountingRunner.runs}"
    print(f"  ✓ New thread_id={fresh_thread[:12]}…: 2 fresh step_node invocations")

    # 5. Verify checkpoint rows exist in Postgres for our thread
    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    try:
        cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (thread_id,))
        n = cur.fetchone()[0]
        assert n > 0, "expected checkpoint rows for the test thread"
        print(f"  ✓ Postgres `checkpoints` has {n} row(s) for thread_id (resume on crash works)")
    finally:
        # Cleanup test thread checkpoints
        try:
            cur.execute("DELETE FROM checkpoints WHERE thread_id IN (%s, %s)", (thread_id, fresh_thread))
            cur.execute("DELETE FROM checkpoint_writes WHERE thread_id IN (%s, %s)", (thread_id, fresh_thread))
            cur.execute("DELETE FROM checkpoint_blobs WHERE thread_id IN (%s, %s)", (thread_id, fresh_thread))
            conn.commit()
        except Exception:
            pass
        conn.close()

    print()
    print("=" * 60)
    print("F6 CHECKPOINT TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
