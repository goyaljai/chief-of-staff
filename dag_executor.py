"""V3.5: DAG executor using LangGraph for parallel fan-out (map-reduce pattern).

Replaces V3's asyncio.gather. Topology:

    START → dispatcher ──┬─[Send]→ step_node ─┐
                         │                    │
                         │                    └─→ dispatcher (loop, barrier)
                         └─[END]

Why a dispatcher node and not a conditional edge straight off step_node:
LangGraph's per-branch conditional edges fire with PARTIAL state — each
parallel branch sees only its own completion in `state.results`, never its
siblings'. A regular node ("dispatcher") receiving edges from all parallel
step_node invocations runs ONCE per superstep, AFTER all parallels merge.
Its outgoing conditional edge then sees the full merged state. This is the
canonical map-reduce idiom in LangGraph.

LangGraph integration gives us:
- Native parallel execution via Send (true fan-out, in one superstep)
- Automatic LangSmith tracing of every node
- Retry/checkpointing primitives
- Visual graph (graph.get_graph().draw_mermaid())
"""
import uuid
from pathlib import Path
from typing import Annotated, Callable, TypedDict

from langgraph.graph import START, END, StateGraph
from langgraph.types import Send

from claude_runner import ClaudeRunner


# Per-execution callback registry. State is JSON-serialized through LangGraph,
# so callables can't ride along — we stash them under a UUID that DOES flow in
# state, and step_node looks the callback back up.
_CALLBACKS: dict[str, Callable[[str, object], None]] = {}

# Per-execution registry of in-flight ClaudeRunner instances. Lets the host
# (supervisor / task_store) interrupt every running step subprocess on cancel
# without each step needing to be tracked individually.
_RUNNERS: dict[str, set] = {}


def get_runners(exec_id: str) -> set:
    """Return the live set of ClaudeRunner instances for an execution. The
    set mutates as steps start and finish — callers that iterate must take
    a snapshot."""
    return _RUNNERS.setdefault(exec_id, set())


def _merge_lists(a: list, b: list) -> list:
    return (a or []) + (b or [])


class DagState(TypedDict):
    """State that flows through the graph. List fields use append reducers so
    parallel step_node invocations within a superstep merge cleanly."""
    steps: list[dict]
    workspace: str
    hook_log_dir: str
    exec_id: str
    shared_context: str  # brief excerpt prepended to every step's prompt
    results: Annotated[list[dict], _merge_lists]
    failed_step_ids: Annotated[list[str], _merge_lists]


def _dispatcher_node(state: DagState) -> dict:
    """Barrier node. Runs once per superstep after all parallel step_nodes merge.
    Does not mutate state — its sole purpose is to be the join point so the
    outgoing conditional edge sees fully merged results."""
    return {}


async def _step_node(state: dict) -> dict:
    """Run one DAG step as a separate Claude Code subprocess.

    All steps share the SAME working directory (the task workspace), because
    the orchestrator's brief specifies deliverable filenames at the workspace
    root — putting each step in `<workspace>/<step_id>/` would scatter the
    artifacts where neither the reviewer nor the user can find them. Hook
    logs ARE per-step (parallel runners must not share a hook log path or
    their permission-hook writes interleave into torn JSON lines).

    Each step's prompt is enriched with shared context (the brief's Objective
    + Deliverables, plus sibling step IDs) so parallel branches don't cold-
    start without knowing the larger goal. Without enrichment, Claude tends
    to reinvent context per step.

    If a step_event_cb was registered, every ClaudeEvent from each step's
    runner is forwarded tagged with step_id so the supervisor can stream
    progress to SSE / STORE.append_log.
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
    sibling_ids = [s["id"] for s in state.get("all_steps", []) if s.get("id") != step.get("id")]
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
        # Pass on_event when the runner supports it (sequential path uses this
        # too). If a runner build doesn't accept the kwarg, fall back silently.
        try:
            result = await runner.run(prompt=enriched_prompt, timeout_secs=timeout, on_event=_on_event)
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
    """Conditional edge from dispatcher. Sees merged state from all completed
    step_nodes. Returns Send(...) for every newly-ready step, or END."""
    done_ids = {r["step_id"] for r in state.get("results", [])}
    pending = [s for s in state["steps"] if s["id"] not in done_ids]
    if not pending:
        return END
    ready = [s for s in pending if all(d in done_ids for d in s.get("depends_on", []))]
    if not ready:
        # Pending steps with unsatisfiable deps → cycle/missing prereq. End so
        # `not_run` accounting in execute_dag surfaces them as failed.
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


def _build_graph():
    builder = StateGraph(DagState)
    builder.add_node("dispatcher", _dispatcher_node)
    builder.add_node("step_node", _step_node)
    builder.add_edge(START, "dispatcher")
    builder.add_conditional_edges("dispatcher", _dispatch_ready, ["step_node", END])
    builder.add_edge("step_node", "dispatcher")
    return builder.compile()


_GRAPH = None


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _build_graph()
    return _GRAPH


def interrupt_all_for(exec_id: str) -> int:
    """Interrupt every live ClaudeRunner registered under this execution.
    Used by the cancel path to actually stop in-flight DAG step subprocesses."""
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
    """Run a DAG via LangGraph. Steps with satisfied deps fan out in parallel
    within a single superstep.

    Args:
        steps: list of {"id", "action", "depends_on", "timeout_secs"?} dicts.
        task_workspace: parent dir; each step gets its own subdir under this.
        hook_log_dir: dir where each step's isolated hook log file lives.
        on_step_event: optional callback `(step_id, ClaudeEvent) -> None` for
            live event streaming. Each parallel runner forwards every event.
            Errors inside the callback are swallowed so they cannot kill steps.

    Returns:
        {"ok": bool, "results": {step_id: result_payload}, "failed": [step_id, ...]}
    """
    if not steps:
        return {"ok": True, "results": {}, "failed": []}

    # V3.5 round-3 fix #10: bound the DAG so an LLM-hallucinated 1000-step
    # brief can't OOM us or blow past the LangGraph recursion limit.
    if len(steps) > 50:
        return {"ok": False, "error": f"dag has too many steps ({len(steps)}; max 50)",
                "results": {}, "failed": [s["id"] for s in steps]}

    try:
        _validate_dag(steps)
    except ValueError as e:
        return {"ok": False, "error": str(e), "results": {},
                "failed": [s["id"] for s in steps]}

    Path(hook_log_dir).mkdir(parents=True, exist_ok=True)

    # Caller-supplied exec_id (typically task_id) lets the cancel path locate
    # in-flight runners. Fall back to UUID for callers that don't care.
    exec_id = exec_id or uuid.uuid4().hex
    if on_step_event is not None:
        _CALLBACKS[exec_id] = on_step_event

    graph = _get_graph()
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

    try:
        final = await graph.ainvoke(initial, config={"recursion_limit": recursion_limit})
        results_by_id = {r["step_id"]: r for r in final.get("results", [])}
        failed = list(final.get("failed_step_ids", []))
        not_run = [s["id"] for s in steps if s["id"] not in results_by_id]
        failed.extend(x for x in not_run if x not in failed)
        return {"ok": len(failed) == 0, "results": results_by_id, "failed": failed}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"ok": False, "error": str(e), "results": {},
                "failed": [s["id"] for s in steps]}
    finally:
        _CALLBACKS.pop(exec_id, None)
        _RUNNERS.pop(exec_id, None)


_SAFE_STEP_ID = __import__("re").compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _validate_dag(steps: list[dict]) -> None:
    ids = {s["id"] for s in steps}
    if len(ids) != len(steps):
        raise ValueError("dag has duplicate step ids")
    for s in steps:
        sid = s.get("id", "")
        if not isinstance(sid, str) or not _SAFE_STEP_ID.match(sid):
            # LLM-hallucinated IDs containing "/", "..", spaces, etc. could
            # escape the workspace via Path(workspace) / step_id. Reject early.
            raise ValueError(f"step id {sid!r} must match [A-Za-z0-9_-]{{1,64}}")
        for dep in s.get("depends_on", []):
            if dep not in ids:
                raise ValueError(f"step {sid!r} depends on unknown step {dep!r}")
    indeg = {s["id"]: 0 for s in steps}
    for s in steps:
        for d in s.get("depends_on", []):
            indeg[s["id"]] += 1
    children: dict[str, list[str]] = {sid: [] for sid in ids}
    for s in steps:
        for d in s.get("depends_on", []):
            children[d].append(s["id"])
    queue = [sid for sid, n in indeg.items() if n == 0]
    seen = 0
    while queue:
        sid = queue.pop()
        seen += 1
        for child in children[sid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    if seen != len(steps):
        raise ValueError("dag has a cycle")


def get_graph_mermaid() -> str:
    try:
        return _get_graph().get_graph().draw_mermaid()
    except Exception:
        return ""
