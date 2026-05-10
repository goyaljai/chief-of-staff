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
    out = orchestrator_singleton.think_and_ask(req.task)
    stash_preview(req.task, out["skill_preview"])
    return {
        "task": req.task,
        "questions": out["questions"],
        "skill_preview": out["skill_preview"],
        "next": "Send POST /task/run with {task, clarifications: {q1: a1, ...}} to start.",
    }
