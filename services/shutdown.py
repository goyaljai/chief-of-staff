"""Graceful shutdown — drain coroutine + a shared shutdown flag.

When uvicorn receives SIGINT/SIGTERM it begins its native shutdown
sequence: stop accepting new connections, wait for in-flight requests
up to `timeout_graceful_shutdown`, then fire FastAPI's
`@app.on_event("shutdown")` hook. main.py's hook calls `drain_inflight()`,
which is the cleanup work this module owns:

  1. Flip _SHUTTING_DOWN = True so any /task/run that's already past
     uvicorn's connection-stop but not yet rejected sees a 503 (defence
     in depth — uvicorn's own connection-close already covers the common
     case).
  2. Iterate every non-terminal task and:
     - interrupt the registered sequential-path runner (R4-2)
     - interrupt every in-flight DAG step runner via interrupt_all_for
     - mark the task `interrupted` (with a checkpoint reference if F6 wired)
  3. Final-flush the E5 log buffer so nothing is lost in memory.
  4. Close the DB pool to release connections.

#70 fix (2026-05-11): the previous implementation called
`loop.add_signal_handler(SIGINT, _on_signal)` which OVERWROTE uvicorn's
own signal handler — uvicorn never saw the signal, never started its
shutdown sequence, and the process sat in 503-mode forever after drain.
Workaround used to be `lsof -ti :8000 | xargs kill -9`. Now we don't
hijack signals at all — uvicorn handles them natively and our drain
runs through FastAPI's lifecycle.

Public API:
  IS_SHUTTING_DOWN() — query the flag (used by /task/run to return 503)
  drain_inflight()   — async coroutine, the actual cleanup work
"""
import time

from persistence import STORE


# Module-level flag. Read via IS_SHUTTING_DOWN() so callers don't import
# the variable directly (which would snapshot at import time and never
# update). drain_inflight() flips it True at start.
_SHUTTING_DOWN: bool = False


def IS_SHUTTING_DOWN() -> bool:
    """Functional accessor so callers always see the live value, even if
    they imported this module long ago."""
    return _SHUTTING_DOWN


async def drain_inflight() -> None:
    """Drain in-flight work before the process exits.

    Steps:
      0. Flip _SHUTTING_DOWN = True so /task/run starts returning 503.
      1. Iterate non-terminal tasks. For each:
         a) Interrupt the sequential-path runner (registered via
            STORE.register_runner). R4-2 — was missed previously,
            leaking sequential runners until force-kill.
         b) Interrupt every DAG step runner via interrupt_all_for.
         c) Mark the task as 'interrupted' so /task/{id}/resume can
            pick it up later (F6 checkpoint flows here).
      2. Final-flush the E5 log buffer so nothing is lost in memory.
      3. Close the DB pool to release connections.

    Pairs with F6: 'interrupted' tasks have DAG checkpoints, so resume
    after restart picks up at the last completed superstep instead of
    restarting from step 1.

    Idempotent — if called twice (e.g. signal handler + shutdown hook)
    the second call is a no-op.
    """
    global _SHUTTING_DOWN
    if _SHUTTING_DOWN:
        return
    _SHUTTING_DOWN = True

    import persistence as db
    import workflows.dag as dag_executor

    print("[shutdown] enumerating in-flight tasks…")
    interrupted: list[str] = []

    for state in list(STORE.all()):
        if state.status in ("done", "failed", "abandoned", "cancelled"):
            continue

        # R4-2: interrupt the sequential-path runner
        try:
            seq_runner = STORE._runners.get(state.id)
            if seq_runner is not None:
                seq_runner.interrupt()
        except Exception as e:
            print(f"[shutdown] seq_runner.interrupt({state.id}) failed: {e}")

        # Interrupt DAG step runners (no-op if task wasn't using DAG path)
        try:
            dag_executor.interrupt_all_for(state.id)
        except Exception as e:
            print(f"[shutdown] dag_interrupt({state.id}) failed: {e}")

        # Mark interrupted
        try:
            STORE.append_log(state.id, {"kind": "interrupted_by_shutdown", "ts": time.time()})
            STORE.set_status(state.id, "interrupted")
            interrupted.append(state.id)
        except Exception as e:
            print(f"[shutdown] mark interrupted({state.id}) failed: {e}")

    print(f"[shutdown] marked {len(interrupted)} tasks as interrupted")

    # Final E5 log flush
    try:
        await STORE._flush_log_buffer()
    except Exception as e:
        print(f"[shutdown] final log flush failed: {e}")

    # Close DB pool
    try:
        db._close_pool()
    except Exception as e:
        print(f"[shutdown] db pool close failed: {e}")

    print("[shutdown] drain complete — uvicorn will now exit")
