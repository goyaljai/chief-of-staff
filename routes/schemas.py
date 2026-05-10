"""Pydantic request/response schemas shared across multiple route modules.

Centralizing these means a body-shape change touches one file instead of
two or three. None of these have computed fields or validators today —
they're pure data contracts — so a single module is the simplest home.
"""
from pydantic import BaseModel


class TaskQuestionsRequest(BaseModel):
    """POST /task/questions — produce 3-5 sharp clarifying questions."""
    task: str


class TaskRunRequest(BaseModel):
    """POST /task/run — start a supervised task.

    `working_dir` is optional; when omitted the server allocates a fresh
    workspace under WORKSPACE_ROOT/<task_id>/. When supplied, the caller is
    responsible for the directory existing and being safe to mutate.
    """
    task: str
    clarifications: dict[str, str] = {}
    working_dir: str | None = None


class EscalationAnswer(BaseModel):
    """POST /task/{id}/escalation — D5 free-text answers (not just a/b)."""
    answer: str


class AskRequest(BaseModel):
    """POST /ask and POST /task/{id}/ask — natural-language query."""
    question: str
    top_k: int = 6


class NoteRequest(BaseModel):
    """POST /task/{id}/note — drop a side-note onto an in-flight task."""
    note: str
