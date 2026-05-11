"""LangGraph graph builder + AsyncPostgresSaver checkpointer.

The graph is built once (singleton) because the StateGraph compile step
is non-trivial and the topology never changes. The checkpointer is
optional — if DATABASE_URL is unset or the langgraph-checkpoint-postgres
package is missing, the graph runs without resume capability and we log
a one-line warning.

F6 (DAG checkpoint + resume) ties every superstep checkpoint to the
caller's exec_id (typically task_id) via thread_id. To resume after
a crash, just call execute_dag with the same exec_id — LangGraph
auto-resumes from the last persisted checkpoint instead of starting
the DAG from step 1.

We use the AsyncPostgresSaver because the DAG runs via graph.ainvoke()
in our async server. The sync PostgresSaver raises NotImplementedError
inside an async invoke.
"""
import os

from langgraph.graph import START, END, StateGraph

from .nodes import _dispatch_ready, _dispatcher_node, _step_node
from .state import DagState


_CHECKPOINTER = None
_CHECKPOINTER_INIT = False
_GRAPH = None


async def _get_async_checkpointer():
    """Singleton AsyncPostgresSaver. Lazily creates the underlying
    checkpoint tables on first use via .setup(). Reuses DATABASE_URL via
    psycopg3 pool. Returns None when DATABASE_URL is unset or setup
    fails — the graph still runs, just without resume capability."""
    global _CHECKPOINTER, _CHECKPOINTER_INIT
    if _CHECKPOINTER_INIT:
        return _CHECKPOINTER
    _CHECKPOINTER_INIT = True
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        return None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=0,
            max_size=4,
            # prepare_threshold=None disables psycopg3's auto-prepared
            # statements entirely. Required for Supabase's transaction
            # pooler (port 6543), which can route consecutive queries from
            # the same client to different backend connections — a PREPARE
            # on connection A then EXECUTE on connection B fails with
            # `prepared statement "_pg3_0" already exists` (or "doesn't
            # exist", depending on direction). Setting 0 ("always prepare")
            # makes the problem worse, not better. None is the disable.
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
        )
        await pool.open()
        cp = AsyncPostgresSaver(conn=pool)
        await cp.setup()
        _CHECKPOINTER = cp
        print("[dag] F6: AsyncPostgresSaver checkpoint tables ready")
    except Exception as e:
        print(f"[dag] checkpoint setup failed (DAGs will run without resume): {e}")
        _CHECKPOINTER = None
    return _CHECKPOINTER


def _build_graph(checkpointer=None):
    builder = StateGraph(DagState)
    builder.add_node("dispatcher", _dispatcher_node)
    builder.add_node("step_node", _step_node)
    builder.add_edge(START, "dispatcher")
    builder.add_conditional_edges("dispatcher", _dispatch_ready, ["step_node", END])
    builder.add_edge("step_node", "dispatcher")
    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()


async def _get_graph():
    """Async because the checkpointer init must run in an async context."""
    global _GRAPH
    if _GRAPH is None:
        cp = await _get_async_checkpointer()
        _GRAPH = _build_graph(checkpointer=cp)
    return _GRAPH


def get_graph_mermaid() -> str:
    """Sync helper that builds an UNCOMPILED graph for visualization only.
    Skips the async checkpointer setup which would require an event loop."""
    try:
        return _build_graph(checkpointer=None).get_graph().draw_mermaid()
    except Exception:
        return ""
