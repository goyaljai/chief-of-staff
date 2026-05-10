"""Question-answering endpoints — single-task and history-wide.

Two flavours:

  POST /task/{id}/ask
    "Ask anything about that specific task." Builds a context bundle from
    the task's brief, skill_md, final Claude output, workspace artifacts,
    and the last 200 log entries, then asks the orchestrator's chat model
    to produce a JSON answer with cited file paths.

  POST /ask
    "Ask history-wide." Combines pgvector semantic search with FTS5 keyword
    search, dedupes, and feeds the merged hits to the orchestrator's
    answer_from_history method which synthesizes a final answer with
    cited task IDs.
"""
from fastapi import APIRouter, HTTPException

import db
import rag
from dependencies import orchestrator_singleton
from routes.schemas import AskRequest
from services.artifacts import list_artifacts, last_claude_output
from task_store import STORE


ask_router = APIRouter(tags=["ask"])


@ask_router.post("/task/{task_id}/ask")
def ask_task(task_id: str, req: AskRequest):
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")

    from orchestrator import _chat, _extract_json

    artifacts = list_artifacts(state.workspace, max_files=10, max_preview=5000)
    final_text = last_claude_output(state.log)

    log_lines: list[str] = []
    for e in state.log[-200:]:
        k = e.get("kind", "?")
        if k == "tool_use":
            log_lines.append(f"[tool] {e.get('tool')}: {str(e.get('input', ''))[:300]}")
        elif k == "tool_result":
            log_lines.append(f"[result] {(e.get('output') or '')[:300]}")
        elif k == "text":
            log_lines.append(f"[claude] {(e.get('text') or '')[:300]}")
        elif k == "result":
            log_lines.append(f"[final] {(e.get('text') or '')[:300]}")

    artifacts_block = "\n\n".join(
        f"### {a['path']}\n```\n{a['preview']}\n```" for a in artifacts
    ) or "(no artifacts)"

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


@ask_router.post("/ask")
def ask(req: AskRequest):
    fts_hits = db.fts_search(req.question, limit=req.top_k)
    semantic_hits = rag.search_tasks(req.question, top_k=req.top_k)

    seen: set = set()
    merged: list[dict] = []
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
