"""In-memory task state. Survives process lifetime, lost on restart.
V2 will move this to SQLite."""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskState:
    id: str
    goal: str
    clarifications: dict[str, str]
    workspace: str
    status: str = "pending"
    brief: str = ""
    log: list[dict] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    escalation: dict | None = None
    escalation_set_at: float | None = None
    result: dict | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    escalation_event: asyncio.Event = field(default_factory=asyncio.Event)
    escalation_answer: str | None = None

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "workspace": self.workspace,
            "brief": self.brief,
            "log_size": len(self.log),
            "log_tail": self.log[-20:],
            "corrections": self.corrections,
            "escalation": self.escalation,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_secs": (self.finished_at or time.time()) - self.started_at,
        }


class TaskStore:
    def __init__(self):
        self._tasks: dict[str, TaskState] = {}

    def create(self, goal: str, clarifications: dict[str, str], workspace: str) -> TaskState:
        tid = uuid.uuid4().hex[:12]
        state = TaskState(id=tid, goal=goal, clarifications=clarifications, workspace=workspace)
        self._tasks[tid] = state
        return state

    def get(self, tid: str) -> TaskState | None:
        return self._tasks.get(tid)

    def all(self) -> list[TaskState]:
        return list(self._tasks.values())

    def append_log(self, tid: str, entry: dict):
        if tid in self._tasks:
            entry["ts"] = time.time()
            self._tasks[tid].log.append(entry)

    def set_status(self, tid: str, status: str):
        if tid in self._tasks:
            self._tasks[tid].status = status
            if status in ("done", "failed", "abandoned"):
                self._tasks[tid].finished_at = time.time()

    def set_escalation(self, tid: str, escalation: dict):
        if tid in self._tasks:
            self._tasks[tid].escalation = escalation
            self._tasks[tid].escalation_set_at = time.time()
            self._tasks[tid].status = "escalated"
            self._tasks[tid].escalation_event.clear()
            self._tasks[tid].escalation_answer = None

    def answer_escalation(self, tid: str, answer: str) -> bool:
        state = self._tasks.get(tid)
        if not state or state.status != "escalated":
            return False
        state.escalation_answer = answer.lower().strip()
        state.escalation_event.set()
        return True


STORE = TaskStore()
