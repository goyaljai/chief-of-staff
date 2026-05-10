"""POST /task/run — start a supervised task (or preview via dry_run=true).

Two modes:

  Live mode (default): create a TaskState, allocate a workspace, queue the
  supervisor loop on the asyncio runtime, return the task_id. Caller polls
  GET /task/{id} (or subscribes via SSE) for progress.

  Dry-run mode (?dry_run=true): build the SKILL brief + executor brief +
  optional ## Steps DAG, return them inline, do NOT spawn Claude. Lets the
  user preview the plan before paying tokens. No row persisted in dry mode.

Refuses new submissions with 503 while shutdown is draining (F1).
"""
import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException

from config import WORKSPACE_ROOT
from dependencies import orchestrator_singleton, pop_preview
from routes.schemas import TaskRunRequest
from services.shutdown import IS_SHUTTING_DOWN
from supervisor_loop import run_task
from task_store import STORE, TaskState


task_run_router = APIRouter(tags=["task"])


@task_run_router.post("/task/run")
async def run(req: TaskRunRequest, dry_run: bool = False):
    if IS_SHUTTING_DOWN():
        raise HTTPException(
            status_code=503,
            detail="server shutting down — try again after restart",
        )

    if dry_run:
        # D6 fix: per-call unique workspace path so concurrent dry runs
        # don't race on save_task_skill writes under a shared _dry_run/ dir.
        dry_id = f"dry_{uuid.uuid4().hex[:8]}"
        transient = TaskState(
            id=dry_id,
            goal=req.task,
            clarifications=req.clarifications,
            workspace=str(WORKSPACE_ROOT / "_dry_run" / dry_id),
        )
        try:
            skill_md = orchestrator_singleton.generate_skill_brief(
                transient.goal,
                transient.clarifications,
                skill_preview="",
                library_match=None,
            )
            transient.skill_md = skill_md
            brief = orchestrator_singleton.build_brief(
                transient.goal,
                transient.clarifications,
                workspace=transient.workspace,
                inline_skill=skill_md,
            )
            try:
                steps = orchestrator_singleton.parse_dag(brief)
            except Exception:
                steps = None
            return {
                "dry_run": True,
                "task_id": transient.id,
                "skill_md": skill_md,
                "brief": brief,
                "dag_steps": steps or [],
                "would_use_dag": bool(steps and len(steps) > 1),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"dry_run failed: {e}")

    state = STORE.create(req.task, req.clarifications, "")
    if req.working_dir:
        state.workspace = req.working_dir
    else:
        state.workspace = str(WORKSPACE_ROOT / state.id)
    Path(state.workspace).mkdir(parents=True, exist_ok=True)
    state.skill_preview = pop_preview(req.task)
    asyncio.create_task(run_task(state.id))
    return {
        "task_id": state.id,
        "status": state.status,
        "workspace": state.workspace,
        "preview_reused": bool(state.skill_preview),
        "next": f"Poll GET /task/{state.id} until status is 'done', 'failed', or 'escalated'.",
    }
