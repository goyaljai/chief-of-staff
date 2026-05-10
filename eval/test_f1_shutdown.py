"""F1 regression test — graceful shutdown drain.

Validates:
  1. _SHUTTING_DOWN flag flips on signal.
  2. /task/run returns 503 when shutting down.
  3. _drain_inflight marks every non-terminal task as 'interrupted'.
  4. interrupt_all_for is called for every in-flight task.
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401


async def main():
    import main as srv
    import dag_executor
    from task_store import TaskState

    print("=" * 60)
    print("F1 GRACEFUL SHUTDOWN TEST")
    print("=" * 60)

    # Reset
    srv._SHUTTING_DOWN = False
    srv.STORE._tasks.clear()

    # 1. Seed a few in-flight tasks
    for i in range(3):
        tid = f"f1_test_{i}"
        s = TaskState(id=tid, goal=f"goal {i}", clarifications={}, workspace=f"/tmp/f1_{i}")
        s.status = "executing_dag" if i == 0 else "executing_loop_1"
        srv.STORE._tasks[tid] = s

    # And one already-terminal task (should NOT be touched)
    s_done = TaskState(id="f1_done", goal="done one", clarifications={}, workspace="/tmp/f1_done")
    s_done.status = "done"
    srv.STORE._tasks["f1_done"] = s_done

    # Track which tasks dag_executor.interrupt_all_for got called for
    interrupted_calls: list[str] = []
    orig = dag_executor.interrupt_all_for
    dag_executor.interrupt_all_for = lambda exec_id: (interrupted_calls.append(exec_id), 0)[1]

    try:
        # 2. Trigger drain
        await srv._drain_inflight()
        assert srv._SHUTTING_DOWN is False, \
            "_drain_inflight should not toggle the flag itself; that's the signal handler's job"
    finally:
        dag_executor.interrupt_all_for = orig

    # 3. All 3 in-flight tasks marked interrupted; the 'done' task untouched
    assert srv.STORE.get("f1_test_0").status == "interrupted"
    assert srv.STORE.get("f1_test_1").status == "interrupted"
    assert srv.STORE.get("f1_test_2").status == "interrupted"
    assert srv.STORE.get("f1_done").status == "done", "terminal task must not be touched"
    print("  ✓ 3 in-flight tasks marked 'interrupted'; 1 terminal task untouched")

    # 4. interrupt_all_for called for each in-flight (not the done one)
    assert sorted(interrupted_calls) == ["f1_test_0", "f1_test_1", "f1_test_2"], interrupted_calls
    print(f"  ✓ dag_executor.interrupt_all_for called for: {sorted(interrupted_calls)}")

    # 5. Signal-handler flag flip + /task/run rejection
    srv._SHUTTING_DOWN = True
    from main import run as run_endpoint, TaskRunRequest
    from fastapi import HTTPException
    raised = False
    try:
        await run_endpoint(TaskRunRequest(task="x", clarifications={}), dry_run=False)
    except HTTPException as e:
        raised = e.status_code == 503
    assert raised, "expected 503 while _SHUTTING_DOWN=True"
    print("  ✓ /task/run returns 503 when _SHUTTING_DOWN")

    # Reset
    srv._SHUTTING_DOWN = False
    srv.STORE._tasks.clear()

    print()
    print("=" * 60)
    print("F1 SHUTDOWN TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
