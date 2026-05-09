"""chief-of-staff V2.5 — FastAPI server.

Endpoints:
  POST /task/questions             → meta-think + 3-5 clarifying questions (sync)
  POST /task/run                   → start supervised task, returns task_id (async)
  GET  /task/{id}                  → full state (status, log_tail, escalation, result, cost)
  POST /task/{id}/escalation       → answer pending escalation (a or b)
  POST /task/{id}/cancel           → cancel a running task
  GET  /task/{id}/stream           → Server-Sent Events live event tail
  POST /ask                        → query history (FTS5 + ChromaDB) and synthesize answer
  GET  /tasks                      → list known tasks
  GET  /health                     → ok
  GET  /                           → redirects to /static/index.html (web UI)
"""
import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import db
import rag
from config import STATIC_DIR, WORKSPACE_ROOT, WORKSPACE_TTL_DAYS
from orchestrator import Orchestrator
from supervisor_loop import run_task
from task_store import STORE


app = FastAPI(title="chief-of-staff V2.5")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator_singleton = Orchestrator()

_SKILL_PREVIEW_CACHE: dict[str, tuple[str, float]] = {}
_PREVIEW_TTL_SECS = 600


def _task_hash(task: str) -> str:
    return hashlib.sha256(task.strip().encode()).hexdigest()[:16]


def _stash_preview(task: str, preview: str):
    _SKILL_PREVIEW_CACHE[_task_hash(task)] = (preview, time.time())


def _pop_preview(task: str) -> str:
    h = _task_hash(task)
    entry = _SKILL_PREVIEW_CACHE.pop(h, None)
    if not entry:
        return ""
    preview, ts = entry
    if time.time() - ts > _PREVIEW_TTL_SECS:
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


class AskRequest(BaseModel):
    question: str
    top_k: int = 6


class NoteRequest(BaseModel):
    note: str


@app.on_event("startup")
async def startup():
    db.init_db()
    STORE.hydrate_from_db()
    print(f"[main] hydrated {len(STORE.all())} tasks from DB")
    asyncio.create_task(_workspace_sweeper())


async def _workspace_sweeper():
    """Background loop: every hour, delete workspaces of tasks finished > TTL days ago."""
    if WORKSPACE_TTL_DAYS <= 0:
        return
    while True:
        try:
            await asyncio.sleep(3600)
            max_age = WORKSPACE_TTL_DAYS * 86400
            old = db.cleanup_old_workspaces(max_age)
            for tid, ws in old:
                if ws and Path(ws).exists():
                    try:
                        shutil.rmtree(ws, ignore_errors=True)
                        print(f"[sweeper] removed {ws} (task {tid})")
                    except Exception as e:
                        print(f"[sweeper] failed to remove {ws}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[sweeper] loop error: {e}")


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.5", "tasks_in_memory": len(STORE.all())}


@app.get("/")
def root():
    import time as _t
    return RedirectResponse(url=f"/static/index.html?v={int(_t.time())}")


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
    public = state.to_public()
    public["artifacts"] = _list_artifacts(state.workspace)
    public["final_output"] = _last_claude_output(state.log)
    return public


def _list_artifacts(workspace: str, max_files: int = 20, max_preview: int = 3000) -> list[dict]:
    """List user-facing artifact files in the workspace with previews."""
    if not workspace:
        return []
    p = Path(workspace)
    if not p.exists():
        return []
    skip_dirs = {"skills", ".claude", ".gradle", ".idea", "build", "node_modules",
                 "__pycache__", "venv", ".venv", "intermediates", "outputs"}
    out: list[dict] = []
    for f in sorted(p.rglob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not f.is_file():
            continue
        rel = f.relative_to(p)
        if any(part in skip_dirs or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.name.startswith("."):
            continue
        try:
            size = f.stat().st_size
            preview = ""
            try:
                if size < 200_000:
                    preview = f.read_text(errors="replace")[:max_preview]
            except Exception:
                preview = "(binary)"
            out.append({
                "path": str(rel),
                "abs_path": str(f),
                "size_bytes": size,
                "preview": preview,
            })
            if len(out) >= max_files:
                break
        except Exception:
            continue
    return out


def _last_claude_output(log: list[dict]) -> str:
    """Find the last 'result' or 'text' event Claude produced — useful when there's no file artifact."""
    for entry in reversed(log):
        k = entry.get("kind")
        if k == "result" and entry.get("text"):
            return entry["text"][:8000]
    for entry in reversed(log):
        k = entry.get("kind")
        if k == "text" and entry.get("text"):
            return entry["text"][:8000]
    return ""


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


@app.post("/task/{task_id}/note")
def add_note(task_id: str, body: NoteRequest):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status in ("done", "failed", "abandoned", "cancelled"):
        raise HTTPException(status_code=400, detail=f"task already terminal ({state.status}); send as a new task")
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="note empty")
    state.user_notes.append(note)
    STORE.append_log(task_id, {"kind": "user_note", "note": note})
    return {"ok": True, "queued_for_next_loop": True, "notes_pending": len(state.user_notes)}


@app.post("/task/{task_id}/resume")
async def resume_task(task_id: str):
    """V3 #3: resume an interrupted task (one that was in-flight when server restarted)."""
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status != "interrupted":
        raise HTTPException(status_code=400, detail=f"task is not interrupted (status={state.status})")
    state.status = "pending"
    asyncio.create_task(run_task(state.id))
    return {"ok": True, "task_id": state.id, "status": "resuming"}


@app.post("/task/{task_id}/cancel")
def cancel_task(task_id: str):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status in ("done", "failed", "abandoned", "cancelled"):
        return {"ok": False, "reason": f"already terminal: {state.status}"}
    ok = STORE.cancel(task_id)
    return {"ok": ok, "status": state.status}


@app.get("/task/{task_id}/stream")
async def stream_task(task_id: str, request: Request):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")

    async def event_gen():
        q = STORE.subscribe(task_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": "log", "data": json.dumps(entry, default=str)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ts": time.time()})}
                cur = STORE.get(task_id)
                if cur and cur.status in ("done", "failed", "abandoned", "cancelled"):
                    yield {"event": "terminal", "data": json.dumps({"status": cur.status})}
                    break
        finally:
            STORE.unsubscribe(task_id, q)

    return EventSourceResponse(event_gen())


@app.post("/admin/reindex")
def reindex():
    fts = db.reindex_fts()
    chroma = rag.reindex_all_from_db()
    return {"fts_rows": fts, "chroma": chroma}


@app.post("/task/{task_id}/ask")
def ask_task(task_id: str, req: AskRequest):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    from orchestrator import _chat, _extract_json
    artifacts = _list_artifacts(state.workspace, max_files=10, max_preview=5000)
    final_text = _last_claude_output(state.log)
    log_lines = []
    for e in state.log[-200:]:
        k = e.get("kind", "?")
        if k == "tool_use":
            log_lines.append(f"[tool] {e.get('tool')}: {str(e.get('input',''))[:300]}")
        elif k == "tool_result":
            log_lines.append(f"[result] {(e.get('output') or '')[:300]}")
        elif k == "text":
            log_lines.append(f"[claude] {(e.get('text') or '')[:300]}")
        elif k == "result":
            log_lines.append(f"[final] {(e.get('text') or '')[:300]}")
    artifacts_block = "\n\n".join(f"### {a['path']}\n```\n{a['preview']}\n```" for a in artifacts) or "(no artifacts)"
    user_prompt = (
        f"User question about task `{task_id}`: {req.question}\n\n"
        "Answer using ONLY the context below — this is everything from this single task. "
        "If the answer isn't in the context, say so directly.\n\n"
        f"GOAL: {state.goal}\n\n"
        f"BRIEF:\n{state.brief[:2000]}\n\n"
        f"SKILL.md:\n{(state.skill_md or '')[:2000]}\n\n"
        f"FINAL TEXT FROM CLAUDE:\n{final_text[:3000]}\n\n"
        f"WORKSPACE ARTIFACTS:\n{artifacts_block}\n\n"
        f"ACTION LOG:\n{chr(10).join(log_lines)}\n\n"
        'Output JSON: {"answer": "...", "cited_files": ["path1"]}'
    )
    response = _chat(orchestrator_singleton.system, user_prompt, max_tokens=1024)
    data = _extract_json(response)
    return {
        "task_id": task_id,
        "question": req.question,
        "answer": (data.get("answer") or response or "").strip(),
        "cited_files": data.get("cited_files") or [],
    }


@app.post("/ask")
def ask(req: AskRequest):
    fts_hits = db.fts_search(req.question, limit=req.top_k)
    semantic_hits = rag.search_tasks(req.question, top_k=req.top_k)

    seen = set()
    merged = []
    for h in fts_hits:
        if h["id"] in seen:
            continue
        seen.add(h["id"])
        merged.append({
            "task_id": h["id"],
            "goal": h.get("goal", ""),
            "snip": h.get("snip") or h.get("brief", "")[:300],
            "via": "fts",
        })
    for h in semantic_hits:
        if h["task_id"] in seen:
            continue
        seen.add(h["task_id"])
        merged.append({
            "task_id": h["task_id"],
            "goal": (h.get("meta") or {}).get("goal", ""),
            "snip": h.get("doc", "")[:600],
            "via": "semantic",
            "distance": h.get("distance"),
        })

    answer = orchestrator_singleton.answer_from_history(req.question, merged[:req.top_k])
    return {
        "question": req.question,
        "answer": answer["answer"],
        "cited_task_ids": answer["cited_task_ids"],
        "retrieved": merged[:req.top_k],
    }


@app.get("/tasks")
def list_tasks():
    return [s.to_public() for s in STORE.all()]


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    print("chief-of-staff V2.5 — http://localhost:8000")
    print("Web UI: http://localhost:8000/")
    print("API docs: http://localhost:8000/docs")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
