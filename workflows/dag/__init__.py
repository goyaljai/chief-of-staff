"""LangGraph DAG executor — parallel fan-out via map-reduce dispatcher.

Replaces V3's asyncio.gather with a proper LangGraph topology:

    START → dispatcher ──┬─[Send]→ step_node ─┐
                         │                    │
                         │                    └─→ dispatcher (loop, barrier)
                         └─[END]

LangGraph integration gives us:
  - Native parallel execution via Send (true fan-out, in one superstep)
  - Automatic LangSmith tracing of every node
  - Retry / checkpointing primitives (AsyncPostgresSaver — F6)
  - Visual graph (graph.get_graph().draw_mermaid())

PUBLIC API
==========
The package re-exports the three public-facing names so callers keep
writing `import dag_executor; dag_executor.execute_dag(...)` after the
split — except now they import `from workflows.dag import execute_dag`
or use the top-level `dag_executor` shim if any caller still does so.

  execute_dag(steps, task_workspace, hook_log_dir, on_step_event=None,
              exec_id=None, shared_context="") -> dict
  interrupt_all_for(exec_id) -> int (count of runners interrupted)
  get_graph_mermaid() -> str (for visualization / docs)

INTERNAL LAYOUT
===============
  workflows/dag/state.py    — DagState + per-execution registries
                              (_CALLBACKS, _RUNNERS) + _merge_lists reducer
  workflows/dag/nodes.py    — _step_node + _dispatcher_node + _dispatch_ready
  workflows/dag/graph.py    — _build_graph + _get_graph + AsyncPostgresSaver
  workflows/dag/validate.py — _validate_dag (id-safety, deps, cycles)
"""
import uuid
from pathlib import Path
from typing import Callable

from .graph import _get_graph, get_graph_mermaid
from .state import _CALLBACKS, _RUNNERS, DagState, get_runners
from .validate import _validate_dag


def interrupt_all_for(exec_id: str) -> int:
    """Interrupt every live ClaudeRunner registered under this execution.
    Used by the cancel path to actually stop in-flight DAG step
    subprocesses."""
    if not exec_id:
        return 0
    snapshot = list(_RUNNERS.get(exec_id, set()))
    n = 0
    for runner in snapshot:
        try:
            runner.interrupt()
            n += 1
        except Exception as e:
            print(f"[dag] interrupt failed: {e}")
    return n


async def execute_dag(
    steps: list[dict],
    task_workspace: Path,
    hook_log_dir: Path,
    on_step_event: Callable[[str, object], None] | None = None,
    exec_id: str | None = None,
    shared_context: str = "",
) -> dict:
    """Run a DAG via LangGraph. Steps with satisfied deps fan out in
    parallel within a single superstep.

    Args:
        steps: list of {"id", "action", "depends_on", "timeout_secs"?} dicts.
        task_workspace: parent dir; each step gets its own subdir under
            this.
        hook_log_dir: dir where each step's isolated hook log file lives.
        on_step_event: optional callback `(step_id, ClaudeEvent) -> None`
            for live event streaming. Each parallel runner forwards every
            event. Errors inside the callback are swallowed so they
            cannot kill steps.

    Returns:
        {"ok": bool, "results": {step_id: result_payload},
         "failed": [step_id, ...]}
    """
    if not steps:
        return {"ok": True, "results": {}, "failed": []}

    # Round-3 fix #10: bound the DAG so an LLM-hallucinated 1000-step
    # brief can't OOM us or blow past the LangGraph recursion limit.
    if len(steps) > 50:
        return {
            "ok": False,
            "error": f"dag has too many steps ({len(steps)}; max 50)",
            "results": {},
            "failed": [s["id"] for s in steps],
        }

    try:
        _validate_dag(steps)
    except ValueError as e:
        return {
            "ok": False,
            "error": str(e),
            "results": {},
            "failed": [s["id"] for s in steps],
        }

    Path(hook_log_dir).mkdir(parents=True, exist_ok=True)

    # Caller-supplied exec_id (typically task_id) lets the cancel path
    # locate in-flight runners. Fall back to UUID for callers that don't
    # care.
    exec_id = exec_id or uuid.uuid4().hex
    if on_step_event is not None:
        _CALLBACKS[exec_id] = on_step_event

    graph = await _get_graph()
    initial: DagState = {
        "steps": steps,
        "workspace": str(task_workspace),
        "hook_log_dir": str(hook_log_dir),
        "exec_id": exec_id,
        "shared_context": shared_context or "",
        "results": [],
        "failed_step_ids": [],
    }

    max_layers = max(1, len(steps))
    recursion_limit = max(50, 2 * max_layers + 10)

    # F6: thread_id ties every superstep checkpoint to this task. If the
    # task is re-invoked later (after a crash/restart), passing the same
    # thread_id resumes from the last persisted checkpoint instead of
    # starting from step 1.
    invoke_config: dict = {
        "recursion_limit": recursion_limit,
        "configurable": {"thread_id": exec_id},
    }
    try:
        final = await graph.ainvoke(initial, config=invoke_config)
        results_by_id = {r["step_id"]: r for r in final.get("results", [])}
        failed = list(final.get("failed_step_ids", []))
        not_run = [s["id"] for s in steps if s["id"] not in results_by_id]
        failed.extend(x for x in not_run if x not in failed)
        return {
            "ok": len(failed) == 0,
            "results": results_by_id,
            "failed": failed,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "ok": False,
            "error": str(e),
            "results": {},
            "failed": [s["id"] for s in steps],
        }
    finally:
        _CALLBACKS.pop(exec_id, None)
        _RUNNERS.pop(exec_id, None)


__all__ = [
    "DagState",
    "execute_dag",
    "get_graph_mermaid",
    "get_runners",
    "interrupt_all_for",
]
