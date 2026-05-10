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
import os
from pathlib import Path

# V3.5: load .env BEFORE any other import so LangChain/LangSmith tracing flags
# are visible when langchain_* modules initialize their tracers at import time.
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

import asyncio
import hashlib
import json
import shutil
import time

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
    try:
        from orchestrator import bootstrap_skill_lessons_from_md
        bootstrap_skill_lessons_from_md()
    except Exception as e:
        print(f"[main] skill_lessons bootstrap failed: {e}")
    # V3.5 C4: sweep stale workspaces immediately on boot, not just hourly.
    # Short-lived servers (CI, local restarts) otherwise never run cleanup.
    try:
        _run_workspace_sweep_once()
    except Exception as e:
        print(f"[main] startup workspace sweep failed: {e}")
    # V3.5: ping Supabase REST API so the dashboard's request counters show
    # we're alive. psycopg2 over port 5432 doesn't register on the dashboard's
    # Total Requests / Database Requests widgets (those count PostgREST hits).
    try:
        _supabase_rest_ping()
    except Exception as e:
        print(f"[main] supabase rest ping failed (ok to ignore): {e}")
    # V3.5 E5: start the log-write batch flusher. Without this, append_log
    # falls back to per-event synchronous DB inserts.
    try:
        STORE.start_log_flusher()
        print("[main] log_flusher started (E5: batched log writes)")
    except Exception as e:
        print(f"[main] log_flusher start failed: {e}")
    asyncio.create_task(_workspace_sweeper())


def _supabase_rest_ping() -> None:
    """One PostgREST GET at startup so Supabase dashboard sees traffic."""
    import os, urllib.request, urllib.error
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not url or not key:
        return
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/tasks?select=id&limit=1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "chief-of-staff/V2.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            print(f"[supabase] REST ping ok ({r.status}) — dashboard will now show 1+ requests")
    except urllib.error.HTTPError as e:
        # Even a 401/403 is a hit on the REST API and registers in metrics.
        print(f"[supabase] REST ping returned {e.code} (still counts as a request)")


def _run_workspace_sweep_once() -> int:
    """Single-pass workspace cleanup. Reusable from the startup hook AND the
    hourly background loop so the policy lives in one place."""
    if WORKSPACE_TTL_DAYS <= 0:
        return 0
    max_age = WORKSPACE_TTL_DAYS * 86400
    old = db.cleanup_old_workspaces(max_age)
    swept = 0
    for tid, ws in old:
        if ws and Path(ws).exists():
            try:
                shutil.rmtree(ws, ignore_errors=True)
                print(f"[sweeper] removed {ws} (task {tid})")
                swept += 1
            except Exception as e:
                print(f"[sweeper] failed to remove {ws}: {e}")
    # V3.5 round-2 fix #5: dry_run dirs aren't in the DB so cleanup_old_workspaces
    # never touches them. Sweep them by mtime here — anything under
    # WORKSPACE_ROOT/_dry_run older than 1 day is fair game.
    import time as _time
    dry_root = WORKSPACE_ROOT / "_dry_run"
    if dry_root.exists():
        cutoff = _time.time() - 86400  # 1 day TTL for dry-run scratch
        for p in dry_root.iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    swept += 1
                    print(f"[sweeper] removed stale dry_run dir {p}")
            except Exception as e:
                print(f"[sweeper] failed to remove {p}: {e}")
    if swept:
        print(f"[sweeper] swept {swept} stale workspaces (TTL={WORKSPACE_TTL_DAYS}d)")
    return swept


async def _workspace_sweeper():
    """Background loop: every hour, delete workspaces of tasks finished > TTL days ago."""
    if WORKSPACE_TTL_DAYS <= 0:
        return
    while True:
        try:
            await asyncio.sleep(3600)
            _run_workspace_sweep_once()
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
async def run(req: TaskRunRequest, dry_run: bool = False):
    """V3.5 D6: with `?dry_run=true`, generate Skill.md + brief and return them
    WITHOUT spawning Claude. Lets the user preview the plan before paying tokens.
    No task row persisted in dry-run mode (we use a transient TaskState)."""
    if dry_run:
        from task_store import TaskState as _TS
        import uuid as _uuid
        # V3.5 D6 fix: per-call unique workspace path — concurrent dry runs
        # otherwise share `_dry_run/` and race in save_task_skill writes.
        dry_id = f"dry_{_uuid.uuid4().hex[:8]}"
        transient = _TS(
            id=dry_id,
            goal=req.task,
            clarifications=req.clarifications,
            workspace=str(WORKSPACE_ROOT / "_dry_run" / dry_id),
        )
        try:
            skill_md = orchestrator_singleton.generate_skill_brief(
                transient.goal, transient.clarifications, skill_preview="", library_match=None,
            )
            transient.skill_md = skill_md
            brief = orchestrator_singleton.build_brief(
                transient.goal, transient.clarifications,
                workspace=transient.workspace, inline_skill=skill_md,
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


DELIVERABLE_BINARY_EXTS = (".apk", ".ipa", ".jar", ".aar", ".zip", ".tar.gz", ".tgz", ".whl", ".dmg", ".pkg")


def _list_artifacts(workspace: str, max_files: int = 30, max_preview: int = 3000) -> list[dict]:
    """List user-facing artifact files in the workspace.
    Skips build noise EXCEPT for high-value binary deliverables (APK, JAR, etc.)
    which are surfaced with metadata only (no content preview)."""
    if not workspace:
        return []
    p = Path(workspace)
    if not p.exists():
        return []
    skip_dirs = {"skills", ".claude", ".gradle", ".idea", "build", "node_modules",
                 "__pycache__", "venv", ".venv", "intermediates"}
    out: list[dict] = []
    deliverables: list[dict] = []
    for f in sorted(p.rglob("*"), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
        if not f.is_file():
            continue
        rel = f.relative_to(p)
        name = rel.name.lower()
        is_deliverable = any(name.endswith(ext) for ext in DELIVERABLE_BINARY_EXTS)
        if any(part in skip_dirs or part.startswith(".") for part in rel.parts[:-1]):
            if is_deliverable:
                try:
                    deliverables.append({
                        "path": str(rel),
                        "abs_path": str(f),
                        "size_bytes": f.stat().st_size,
                        "preview": "(binary deliverable — not previewed)",
                    })
                except Exception:
                    pass
            continue
        if rel.name.startswith("."):
            continue
        try:
            size = f.stat().st_size
            preview = ""
            try:
                if is_deliverable:
                    preview = "(binary deliverable — not previewed)"
                elif size < 200_000:
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
    return deliverables + out


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
    """V3.5 D5: free-form escalation answers, not just 'a'/'b'.

    Backwards compatible: 'a' / 'b' still routed to the original two-option
    branches in supervisor_loop's _build_post_escalation_prompt. Anything
    longer is forwarded as a free-text directive ('do X instead', 'try Y'),
    which the supervisor injects into the next loop's prompt verbatim."""
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status != "escalated":
        raise HTTPException(status_code=400, detail=f"task is not escalated (status={state.status})")
    answer = (body.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer empty")
    ok = STORE.answer_escalation(task_id, answer)
    return {"ok": ok, "answer": answer, "mode": "binary" if answer.lower() in ("a", "b") else "free_text"}


_MAX_NOTE_LEN = 4000


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
    # V3.5 round-2 fix #7: cap note length to keep prompt budget + memory bounded.
    if len(note) > _MAX_NOTE_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"note too long ({len(note)} chars; max {_MAX_NOTE_LEN})",
        )
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
def reindex(request: Request):
    """V3.5 round-2 fix #6: gate behind ADMIN_TOKEN if env var set.
    Reindex is expensive (re-embeds every task into PGVector) and shouldn't
    be open to network."""
    _check_admin_token(request)
    fts = db.reindex_fts()
    chroma = rag.reindex_all_from_db()
    return {"fts_rows": fts, "chroma": chroma}


_MAX_PATTERN_LEN = 1000
_MAX_REMEDIATION_LEN = 2000


class PromoteLessonRequest(BaseModel):
    pattern: str
    remediation: str | None = None
    domains: list[str] = []
    origin_task_id: str | None = None


def _check_admin_token(request: Request) -> None:
    """V3.5 D7 fix: require ADMIN_TOKEN header for /admin/* routes if the env
    var is set. Self-hosted single-user setups can leave it unset; production
    or shared hosts MUST set it. Closes the no-auth concern on /admin/promote."""
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        return
    got = request.headers.get("x-admin-token", "").strip()
    if got != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Token")


@app.post("/admin/promote")
def admin_promote_lesson(req: PromoteLessonRequest, request: Request):
    """V3.5 D7: seed/curate skill_lessons directly without running a task.

    UPSERTs the lesson (frequency increments on duplicates) and re-renders
    skills/global.md. Closes gap L10."""
    _check_admin_token(request)
    from orchestrator import append_to_global
    pattern = (req.pattern or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    if len(pattern) > _MAX_PATTERN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"pattern too long ({len(pattern)} chars; max {_MAX_PATTERN_LEN})",
        )
    remediation = (req.remediation or "").strip() or None
    if remediation and len(remediation) > _MAX_REMEDIATION_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"remediation too long ({len(remediation)} chars; max {_MAX_REMEDIATION_LEN})",
        )
    entry: dict = {"pattern": pattern}
    if remediation:
        entry["remediation"] = remediation
    if req.domains:
        entry["domains"] = [d for d in req.domains if isinstance(d, str) and d.strip()][:8]
    added = append_to_global([entry], origin_task_id=req.origin_task_id or "admin")
    total = db.count_skill_lessons()
    return {"ok": True, "added_new": added, "total_lessons": total}


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
