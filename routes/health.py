"""Liveness, root redirect, and task list — the cheapest, no-side-effect endpoints.

Co-located because they share the same trivial concern (read-only status).
Splitting them further would be ceremony.

Mounted in main.py via:
    from routes.health import health_router
    app.include_router(health_router)
"""
import time

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from task_store import STORE


health_router = APIRouter(tags=["health"])


@health_router.get("/health")
def health():
    """Liveness probe. Reports the in-memory task count so a stuck hydrate
    on startup is visible without needing a DB query."""
    return {
        "status": "ok",
        "version": "2.5",
        "tasks_in_memory": len(STORE.all()),
    }


@health_router.get("/")
def root():
    """Send browsers to the dashboard. The cache-busting `?v=` query param
    forces a fresh load of static/index.html when the server restarts —
    otherwise users sit on a stale UI through deploys."""
    return RedirectResponse(url=f"/static/index.html?v={int(time.time())}")


@health_router.get("/tasks")
def list_tasks():
    """Compact list of all known tasks (in-memory hydrated). Used by the
    dashboard sidebar and by ops scripts grepping task IDs."""
    return [s.to_public() for s in STORE.all()]
