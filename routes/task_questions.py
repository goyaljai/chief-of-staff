"""POST /task/questions — meta-think + 3-5 sharp clarifying questions.

This is the synchronous "what do you actually want?" step. The orchestrator
runs meta-think to decide which Skill profile applies, then generates 3-5
questions whose answers will materially change the executor brief.

The skill_preview returned here is stashed in dependencies' preview cache
keyed by hash(task) so the matching POST /task/run can reuse it without
re-running meta-think.
"""
from fastapi import APIRouter

from dependencies import orchestrator_singleton, stash_preview
from routes.schemas import TaskQuestionsRequest


task_questions_router = APIRouter(tags=["task"])


@task_questions_router.post("/task/questions")
def questions(req: TaskQuestionsRequest):
    """G9 adaptive — pass clarifications={} on first call, prior answers
    on subsequent calls. Returns one question at a time until done=true."""
    answers = req.clarifications or {}
    out = orchestrator_singleton.think_and_ask(req.task, answers_so_far=answers)
    # Stash the skill_preview only on the first call (when we generated
    # it). Follow-up calls return empty preview, so we don't overwrite.
    if out.get("skill_preview"):
        stash_preview(req.task, out["skill_preview"])
    next_hint = (
        "All clarifications gathered. Send POST /task/run with {task, clarifications} to start."
        if out["done"]
        else "Append the user's answer to clarifications and POST /task/questions again to get the next."
    )
    return {
        "task": req.task,
        "questions": out["questions"],
        "done": out["done"],
        "asked_count": out["asked_count"],
        "skill_preview": out["skill_preview"],
        "next": next_hint,
    }
