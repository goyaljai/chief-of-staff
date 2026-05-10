"""GET /task/{id} — full hydrated state for a single task.

Folds in workspace artifacts (via services.artifacts.list_artifacts) and the
last meaningful Claude output so a single GET gives the dashboard everything
it needs to render a task page.
"""
from fastapi import APIRouter, HTTPException

from services.artifacts import list_artifacts, last_claude_output
from task_store import STORE


task_state_router = APIRouter(tags=["task"])


@task_state_router.get("/task/{task_id}")
def get_task(task_id: str):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    public = state.to_public()
    public["artifacts"] = list_artifacts(state.workspace)
    public["final_output"] = last_claude_output(state.log)
    return public
