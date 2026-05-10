"""Graceful shutdown — extracted from main.py during the v2.0 refactor.

V3.5 F1: SIGINT/SIGTERM handlers + drain coroutine. When the server gets
a kill signal we want to:

  1. Flip a flag so /task/run starts returning 503 immediately
  2. Iterate every non-terminal task and:
     - interrupt the registered sequential-path runner (R4-2)
     - interrupt every in-flight DAG step runner (dag_executor.interrupt_all_for)
     - mark the task `interrupted` (with a checkpoint reference if F6 wired)
  3. Final-flush the E5 log buffer
  4. Close the DB pool

The flag stays True forever after a signal — see #70 in the open-task list
for a known issue: process never exits, just sits in 503-mode. Workaround
is `lsof -ti :8000 | xargs kill -9` until that bug is fixed.

Public API:
  IS_SHUTTING_DOWN()       — query the flag (used by /task/run)
  install_signal_handlers(loop) — wire SIGINT/SIGTERM to the drain coroutine
  drain_inflight()         — async coroutine, the actual cleanup work
"""
import asyncio
import time

import persistence as db
from persistence import STORE


# Module-level flag. Read via IS_SHUTTING_DOWN() so callers don't import
# the variable directly (which would snapshot at import time and never
# update — this is the bug pattern that caused yesterday's drama).
_SHUTTING_DOWN: bool = False


def IS_SHUTTING_DOWN() -> bool:
    """Functional accessor so callers always see the live value, even if
    they imported this module long ago."""
    return _SHUTTING_DOWN


def install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Wire SIGINT and SIGTERM to the drain coroutine.

    Should be called from FastAPI startup hook AFTER the event loop is up.
    Silently degrades on platforms (Windows) that don't support
    add_signal_handler — those just get the FastAPI shutdown_hook fallback."""
    try:
        import signal
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal, sig)
        print("[main] F1: SIGINT/SIGTERM graceful shutdown handlers installed")
    except Exception as e:
        print(f"[main] signal handlers not installed (ok on Windows): {e}")


def _on_signal(sig):
    """Triggered by SIGINT/SIGTERM. Marks shutdown, schedules drain coroutine,
    and lets the FastAPI shutdown hook do the cleanup. Does NOT exit the
    process — uvicorn handles that after its own shutdown completes.

    Known issue (open-task #70): we steal SIGINT from uvicorn via
    add_signal_handler, so uvicorn never sees the signal and the process
    doesn't exit. Workaround in CLAUDE.md."""
    global _SHUTTING_DOWN
    if _SHUTTING_DOWN:
        return  # second signal — let uvicorn force-exit
    _SHUTTING_DOWN = True
    print(f"[main] F1: signal {sig} received — draining in-flight work")
    asyncio.create_task(drain_inflight())


async def drain_inflight() -> None:
    """V3.5 F1: drain in-flight work before the process exits.

    Steps:
      1. Iterate non-terminal tasks. For each:
         a) Interrupt the sequential-path runner (registered via
            STORE.register_runner). Round-2 audit fix #R4-2 — was missed
            previously, leaking sequential runners until force-kill.
         b) Interrupt every DAG step runner via dag_executor.interrupt_all_for.
         c) Mark the task as 'interrupted' so /task/{id}/resume can pick
            it up later (F6 checkpoint flows here).
      2. Final-flush the E5 log buffer so nothing is lost in memory.
      3. Close the DB pool to release connections.

    Pairs with F6: 'interrupted' tasks have DAG checkpoints, so resume
    after restart picks up at the last completed superstep instead of
    restarting from step 1.
    """
    import dag_executor

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
