"""DAG state definition + per-execution registries.

State that flows through the LangGraph dispatcher uses Annotated list
fields with an append reducer so parallel step_node invocations within
a superstep merge cleanly. Without the reducer, LangGraph raises
"two updates to the same key" for any key that multiple parallel
branches write.

Two registries live here too because they hold non-JSON-serializable
state (callables, ClaudeRunner instances) that can't ride along inside
DagState (which is serialized through LangGraph). They're keyed by
exec_id so concurrent task executions don't trample each other:

  _CALLBACKS — step_event callback per execution, set by execute_dag,
               looked up by step_node when forwarding events.
  _RUNNERS   — set of in-flight ClaudeRunner instances per execution,
               used by interrupt_all_for to stop subprocesses on cancel
               or graceful shutdown.
"""
from typing import Annotated, Callable, TypedDict


# Per-execution registries. Lifecycle: execute_dag adds to them on entry
# and pops them in finally. interrupt_all_for inspects _RUNNERS without
# blocking — it takes a snapshot before iterating because the set
# mutates as steps start and finish.
_CALLBACKS: dict[str, Callable[[str, object], None]] = {}
_RUNNERS: dict[str, set] = {}


def get_runners(exec_id: str) -> set:
    """Return the live set of ClaudeRunner instances for an execution.
    The set mutates as steps start and finish — callers that iterate
    must take a snapshot."""
    return _RUNNERS.setdefault(exec_id, set())


def _merge_lists(a: list, b: list) -> list:
    return (a or []) + (b or [])


class DagState(TypedDict):
    """State that flows through the graph. List fields use append reducers
    so parallel step_node invocations within a superstep merge cleanly."""
    steps: list[dict]
    workspace: str
    hook_log_dir: str
    exec_id: str
    shared_context: str  # brief excerpt prepended to every step's prompt
    results: Annotated[list[dict], _merge_lists]
    failed_step_ids: Annotated[list[str], _merge_lists]
