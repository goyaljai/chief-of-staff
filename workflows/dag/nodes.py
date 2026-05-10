"""LangGraph nodes that make up the DAG: dispatcher (barrier) + step_node (worker).

Why a dispatcher node and not a conditional edge straight off step_node:
LangGraph's per-branch conditional edges fire with PARTIAL state — each
parallel branch sees only its own completion in `state.results`, never
its siblings'. A regular node ("dispatcher") receiving edges from all
parallel step_node invocations runs ONCE per superstep, AFTER all
parallels merge. Its outgoing conditional edge then sees the full merged
state. This is the canonical map-reduce idiom in LangGraph.

step_node runs ONE step as a separate Claude Code subprocess. All steps
share the SAME working directory (the task workspace) — putting each
step in a per-step subdir would scatter artifacts where neither the
reviewer nor the user can find them. Hook logs ARE per-step (parallel
runners must not share a hook log path or their permission-hook writes
interleave into torn JSON lines).
"""
from pathlib import Path

from langgraph.graph import END
from langgraph.types import Send

from runners import ClaudeRunner

from .state import _CALLBACKS, _RUNNERS, DagState


def _dispatcher_node(state: DagState) -> dict:
    """Barrier node. Runs once per superstep after all parallel step_nodes
    merge. Does not mutate state — its sole purpose is to be the join
    point so the outgoing conditional edge sees fully merged results."""
    return {}


async def _step_node(state: dict) -> dict:
    """Run one DAG step as a separate Claude Code subprocess.

    Each step's prompt is enriched with shared context (the brief's
    Objective + Deliverables, plus sibling step IDs) so parallel branches
    don't cold-start without knowing the larger goal. Without enrichment,
    Claude tends to reinvent context per step.

    If a step_event_cb was registered, every ClaudeEvent from each
    step's runner is forwarded tagged with step_id so the supervisor
    can stream progress to SSE / STORE.append_log.
    """
    step = state["step"]
    workspace = Path(state["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)

    # Per-step hook log — no concurrent writers from sibling steps.
    hook_log_path = Path(state["hook_log_dir"]) / f"hook-{step['id']}.log"
    hook_log_path.parent.mkdir(parents=True, exist_ok=True)
    hook_log_path.touch(exist_ok=True)

    cb = _CALLBACKS.get(state.get("exec_id", ""))

    def _on_event(event):
        if cb is not None:
            try:
                cb(step["id"], event)
            except Exception:
                pass  # never let UI streaming kill the worker

    runner = ClaudeRunner(working_dir=workspace, hook_log_path=hook_log_path)
    timeout = int(step.get("timeout_secs", 600))

    # Enriched prompt: shared context first, then this step's specific action.
    shared_context = state.get("shared_context", "") or ""
    sibling_ids = [
        s["id"] for s in state.get("all_steps", []) if s.get("id") != step.get("id")
    ]
    siblings_text = (", ".join(sibling_ids) or "(none)")
    enriched_prompt = (
        f"{shared_context}\n\n"
        f"You are running ONE step of a larger DAG. Other steps in this run: {siblings_text}.\n"
        f"They run in their own subprocess(es) — do not duplicate their work; trust they will produce their own artifacts.\n"
        f"All steps share THIS workspace: {workspace}\n\n"
        f"YOUR step (`{step['id']}`):\n{step['action']}"
    ).strip()

    exec_id = state.get("exec_id", "")
    runners = _RUNNERS.setdefault(exec_id, set())
    runners.add(runner)
    try:
        # Pass on_event when the runner supports it (sequential path uses
        # this too). If a runner build doesn't accept the kwarg, fall
        # back silently.
        try:
            result = await runner.run(
                prompt=enriched_prompt,
                timeout_secs=timeout,
                on_event=_on_event,
            )
        except TypeError:
            result = await runner.run(prompt=enriched_prompt, timeout_secs=timeout)
    finally:
        runners.discard(runner)

    payload = {
        "step_id": step["id"],
        "success": result.success,
        "output": (result.output or "")[:1500],
        "session_id": result.session_id,
        "hook_log": str(hook_log_path),
        "workspace": str(workspace),
    }
    return {
        "results": [payload],
        "failed_step_ids": [] if result.success else [step["id"]],
    }


def _dispatch_ready(state: DagState):
    """Conditional edge from dispatcher. Sees merged state from all
    completed step_nodes. Returns Send(...) for every newly-ready step,
    or END."""
    done_ids = {r["step_id"] for r in state.get("results", [])}
    pending = [s for s in state["steps"] if s["id"] not in done_ids]
    if not pending:
        return END
    ready = [s for s in pending if all(d in done_ids for d in s.get("depends_on", []))]
    if not ready:
        # Pending steps with unsatisfiable deps → cycle / missing prereq.
        # End so `not_run` accounting in execute_dag surfaces them as failed.
        return END
    return [
        Send("step_node", {
            "step": s,
            "workspace": state["workspace"],
            "hook_log_dir": state["hook_log_dir"],
            "exec_id": state.get("exec_id", ""),
            "shared_context": state.get("shared_context", ""),
            "all_steps": state["steps"],
        })
        for s in ready
    ]
