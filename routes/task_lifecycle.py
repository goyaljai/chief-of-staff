"""Per-task lifecycle endpoints — escalation answers, side-notes, resume, cancel.

These are co-located because they share the same set of preconditions
(`task must exist`, `task must be in a specific status`) and they're the
"task control surface" the dashboard exposes as buttons.

Routes:
  POST /task/{id}/escalation   — D5 free-text or legacy a/b answer
  POST /task/{id}/note         — drop a side-note onto an in-flight task
  POST /task/{id}/resume       — restart an interrupted (F6 checkpointed) task
  POST /task/{id}/cancel       — interrupt all in-flight runners
"""
import asyncio

from fastapi import APIRouter, HTTPException

from routes.schemas import EscalationAnswer, NoteRequest
from supervisor_loop import run_task
from task_store import STORE


task_lifecycle_router = APIRouter(tags=["task"])


# Round-2 audit fix #7: cap user-note size so a giant paste can't blow the
# prompt budget the next loop is built from.
_MAX_NOTE_LEN = 4000


@task_lifecycle_router.post("/task/{task_id}/escalation")
def answer_escalation(task_id: str, body: EscalationAnswer):
    """V3.5 D5: free-form escalation answers.

    Backwards compatible — 'a' / 'b' still routed to the original two-option
    branches in supervisor_loop. Anything longer is forwarded as a free-text
    directive ('do X instead', 'try Y'), which the supervisor injects into
    the next loop's prompt verbatim.
    """
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status != "escalated":
        raise HTTPException(
            status_code=400,
            detail=f"task is not escalated (status={state.status})",
        )
    answer = (body.answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer empty")
    ok = STORE.answer_escalation(task_id, answer)
    return {
        "ok": ok,
        "answer": answer,
        "mode": "binary" if answer.lower() in ("a", "b") else "free_text",
    }


@task_lifecycle_router.post("/task/{task_id}/note")
def add_note(task_id: str, body: NoteRequest):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status in ("done", "failed", "abandoned", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"task already terminal ({state.status}); send as a new task",
        )
    note = body.note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="note empty")
    if len(note) > _MAX_NOTE_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"note too long ({len(note)} chars; max {_MAX_NOTE_LEN})",
        )
    state.user_notes.append(note)
    STORE.append_log(task_id, {"kind": "user_note", "note": note})
    return {
        "ok": True,
        "queued_for_next_loop": True,
        "notes_pending": len(state.user_notes),
    }


@task_lifecycle_router.post("/task/{task_id}/resume")
async def resume_task(task_id: str):
    """V3 #3: resume an interrupted task. Pairs with F6 (DAG checkpointing) —
    re-invoking the supervisor reuses the same task_id as the LangGraph
    thread_id, so DAG steps pick up at the last completed superstep."""
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status != "interrupted":
        raise HTTPException(
            status_code=400,
            detail=f"task is not interrupted (status={state.status})",
        )
    state.status = "pending"
    asyncio.create_task(run_task(state.id))
    return {"ok": True, "task_id": state.id, "status": "resuming"}


@task_lifecycle_router.post("/task/{task_id}/cancel")
def cancel_task(task_id: str):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")
    if state.status in ("done", "failed", "abandoned", "cancelled"):
        return {"ok": False, "reason": f"already terminal: {state.status}"}
    ok = STORE.cancel(task_id)
    return {"ok": ok, "status": state.status}
