"""chief-of-staff V2.5 — FastAPI server (modularised v2.0 layout).

This file is intentionally small. It does ONE thing — wire the application
together — by:

  1. Loading .env BEFORE any other import so LangChain/LangSmith tracing
     flags are visible when langchain_* modules initialise tracers at
     import time. Skipping this step silently disables tracing.
  2. Building the FastAPI app + CORS middleware.
  3. Mounting every routes/* APIRouter via include_router.
  4. Wiring startup + shutdown hooks (DB hydrate, skill bootstrap,
     workspace sweep, REST ping, log-flusher, F1 signal handlers).
  5. Mounting the static dashboard.

All routes live under routes/. All non-route helpers live under services/.
The orchestrator + reviewer + RAG + persistence layers each have their own
top-level package.

If you find yourself adding a route handler here, you're in the wrong file —
add it to an appropriate routes/* module and include_router below.
"""
import os
from pathlib import Path

# Load .env before any other import. LangChain reads LANGCHAIN_TRACING_V2 /
# LANGSMITH_API_KEY at import time inside its tracers; if these aren't in
# os.environ when langchain_* modules load, tracing silently no-ops.
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

import asyncio

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import persistence as db
from config import STATIC_DIR
from persistence import STORE

from routes.admin import admin_router
from routes.ask import ask_router
from routes.health import health_router
from routes.task_lifecycle import task_lifecycle_router
from routes.task_questions import task_questions_router
from routes.task_run import task_run_router
from routes.task_state import task_state_router
from routes.task_stream import task_stream_router
from routes.undo import undo_router

from services.shutdown import (
    IS_SHUTTING_DOWN,
    drain_inflight,
    install_signal_handlers,
)
from services.sweeper import (
    run_workspace_sweep_once,
    supabase_rest_ping,
    workspace_sweeper_loop,
)


app = FastAPI(title="chief-of-staff V2.5")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── routers ──────────────────────────────────────────────────────────────
# Order is informational only — FastAPI dispatches by path-and-method match.
app.include_router(health_router)
app.include_router(task_questions_router)
app.include_router(task_run_router)
app.include_router(task_state_router)
app.include_router(task_lifecycle_router)
app.include_router(task_stream_router)
app.include_router(undo_router)
app.include_router(ask_router)
app.include_router(admin_router)


# ─── lifecycle ────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Boot sequence — order matters:

      1. db.init_db   — runs schema migrations + opens the connection pool
      2. STORE hydrate — pull non-terminal tasks back into memory so /tasks
         shows them immediately and SSE subscribers can resume
      3. skill_lessons bootstrap — seed the table from skills/global.md if
         the DB is empty (first-run convenience)
      4. workspace sweep — clear stale workspaces synchronously on boot;
         short-lived servers (CI, local restarts) otherwise never run cleanup
      5. supabase REST ping — light up the dashboard's request widgets
      6. start the E5 log flusher — without this, append_log degrades to
         per-event synchronous DB inserts under load
      7. install F1 signal handlers — SIGINT/SIGTERM trigger drain_inflight
      8. start the hourly background workspace sweeper
    """
    db.init_db()
    STORE.hydrate_from_db()
    print(f"[main] hydrated {len(STORE.all())} tasks from DB")

    try:
        from orchestrator import bootstrap_skill_lessons_from_md
        bootstrap_skill_lessons_from_md()
    except Exception as e:
        print(f"[main] skill_lessons bootstrap failed: {e}")

    try:
        run_workspace_sweep_once()
    except Exception as e:
        print(f"[main] startup workspace sweep failed: {e}")

    try:
        supabase_rest_ping()
    except Exception as e:
        print(f"[main] supabase rest ping failed (ok to ignore): {e}")

    try:
        STORE.start_log_flusher()
        print("[main] log_flusher started (E5: batched log writes)")
    except Exception as e:
        print(f"[main] log_flusher start failed: {e}")

    try:
        install_signal_handlers(asyncio.get_running_loop())
    except Exception as e:
        print(f"[main] signal handlers not installed: {e}")

    asyncio.create_task(workspace_sweeper_loop())


@app.on_event("shutdown")
async def shutdown_hook():
    """Belt-and-braces drain in case the signal handler didn't run (test
    harness, lifespan-only mode). Idempotent — drain_inflight checks
    IS_SHUTTING_DOWN and skips if already drained."""
    if not IS_SHUTTING_DOWN():
        await drain_inflight()


# ─── static dashboard ────────────────────────────────────────────────────

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    print("chief-of-staff V2.5 — http://localhost:8000")
    print("Web UI: http://localhost:8000/")
    print("API docs: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
