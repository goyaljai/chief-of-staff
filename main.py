"""Supervisor V1 — FastAPI server.
Endpoints:
  POST /task/questions             → ≤5 clarifying questions for a task (sync)
  POST /task/run                   → start task with answers, returns task_id (async)
  GET  /task/{id}                  → full state (status, log_tail, escalation, result)
  POST /task/{id}/escalation       → answer pending escalation (a or b)
  GET  /tasks                      → list all known tasks (debug)
  GET  /health                     → ok
"""
import asyncio
from pathlib import Path
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from config import WORKSPACE_ROOT
from orchestrator import Orchestrator
from supervisor_loop import run_task
from task_store import STORE


app = FastAPI(title="Supervisor V1")
orchestrator_singleton = Orchestrator()

import hashlib, time as _time
_SKILL_PREVIEW_CACHE: dict[str, tuple[str, float]] = {}
_PREVIEW_TTL_SECS = 600


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.strip().encode()).hexdigest()[:16]


def _stash_preview(task: str, preview: str):
    _SKILL_PREVIEW_CACHE[_task_hash(task)] = (preview, _time.time())


def _pop_preview(task: str) -> str:
    h = _task_hash(task)
    entry = _SKILL_PREVIEW_CACHE.pop(h, None)
    if not entry:
        return ""
    preview, ts = entry
    if _time.time() - ts > _PREVIEW_TTL_SECS:
        return ""
    return preview


class TaskQuestionsRequest(BaseModel):
    task: str


class TaskRunRequest(BaseModel):
    task: str
    clarifications: dict[str, str] = {}
    working_dir: str | None = None


class EscalationAnswer(BaseModel):
    answer: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/task/questions")
def questions(req: TaskQuestionsRequest):
    out = orchestrator_singleton.think_and_ask(req.task)
    _stash_preview(req.task, out["skill_preview"])
    return {
        "task": req.task,
        "questions": out["questions"],
        "skill_preview": out["skill_preview"],
        "next": "Send POST /task/run with {task, clarifications: {q1: a1, ...}} to start.",
    }


@app.post("/task/run")
async def run(req: TaskRunRequest):
    state = STORE.create(req.task, req.clarifications, "")
    if req.working_dir:
        state.workspace = req.working_dir
    else:
        state.workspace = str(WORKSPACE_ROOT / state.id)
    Path(state.workspace).mkdir(parents=True, exist_ok=True)
    state.skill_preview = _pop_preview(req.task)
    asyncio.create_task(run_task(state.id))
    return {
        "task_id": state.id,
        "status": state.status,
        "workspace": state.workspace,
        "preview_reused": bool(state.skill_preview),
        "next": f"Poll GET /task/{state.id} until status is 'done', 'failed', or 'escalated'.",
    }


@app.get("/task/{task_id}")
def get_task(task_id: str):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    return state.to_public()


@app.post("/task/{task_id}/escalation")
def answer_escalation(task_id: str, body: EscalationAnswer):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status != "escalated":
        raise HTTPException(status_code=400, detail=f"task is not escalated (status={state.status})")
    if body.answer.lower() not in ("a", "b"):
        raise HTTPException(status_code=400, detail="answer must be 'a' or 'b'")
    ok = STORE.answer_escalation(task_id, body.answer)
    return {"ok": ok, "answer": body.answer}


@app.get("/tasks")
def list_tasks():
    return [s.to_public() for s in STORE.all()]


if __name__ == "__main__":
    import uvicorn
    print("Supervisor V1 — http://localhost:8000")
    print("Docs: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
