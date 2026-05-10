"""Task state store. V2.5: persists to SQLite via db.py.
Also holds in-memory: SSE subscribers, runner registry for cancel, escalation events.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import db


@dataclass
class TaskState:
    id: str
    goal: str
    clarifications: dict[str, str]
    workspace: str
    status: str = "pending"
    skill_preview: str = ""
    skill_md: str = ""
    skill_name: str = ""
    skill_description: str = ""
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
    cost_databricks_in: int = 0
    cost_databricks_out: int = 0
    cost_claude_usd: float = 0.0
    keep_workspace: bool = False
    user_notes: list[str] = field(default_factory=list)
    claude_plan: list[dict] = field(default_factory=list)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "workspace": self.workspace,
            "skill_md": self.skill_md,
            "skill_name": self.skill_name,
            "skill_description": self.skill_description,
            "brief": self.brief,
            "log_size": len(self.log),
            "log_tail": _build_log_tail(self.log),
            "dag_progress": _build_dag_progress(self.log),
            "claude_plan": self.claude_plan,
            "corrections": self.corrections,
            "escalation": self.escalation,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_secs": (self.finished_at or time.time()) - self.started_at,
            "cost": {
                "databricks_input_tokens": self.cost_databricks_in,
                "databricks_output_tokens": self.cost_databricks_out,
                "claude_usd": round(self.cost_claude_usd, 4),
            },
        }


# ---------- V3.5 E7: smart log_tail + dag_progress ----------

_LOG_TAIL_MAX = 60
_LOG_TAIL_STEP_PER_ID = 5
_LOG_TAIL_WINDOW = 250


def _build_log_tail(log: list[dict]) -> list[dict]:
    """Build a 60-entry tail that keeps ALL recent non-step events plus at most
    5 dag_step_event entries per step_id. Without this, a long DAG run floods
    log_tail with step events and the UI loses signal (phase/correction/error
    entries get pushed out of the visible window and the page lags)."""
    if len(log) <= _LOG_TAIL_MAX:
        return list(log)
    window = log[-_LOG_TAIL_WINDOW:]
    keep: list[dict] = []
    step_count: dict[str, int] = {}
    for e in reversed(window):
        if e.get("kind") == "dag_step_event" and e.get("step_id"):
            sid = e["step_id"]
            if step_count.get(sid, 0) >= _LOG_TAIL_STEP_PER_ID:
                continue
            step_count[sid] = step_count.get(sid, 0) + 1
            keep.append(e)
        else:
            keep.append(e)
        if len(keep) >= _LOG_TAIL_MAX:
            break
    keep.reverse()
    return keep


def _build_dag_progress(log: list[dict]) -> dict:
    """Per-step summary: count of events, last event text, first-seen ts.
    Cheap structured field for the dashboard list view ("step 2/3: building")
    so the UI doesn't have to walk the log to figure out per-step state."""
    progress: dict[str, dict] = {}
    for e in log:
        if e.get("kind") == "dag_step_event" and e.get("step_id"):
            sid = e["step_id"]
            p = progress.setdefault(sid, {"count": 0, "started_at": e.get("ts"), "last": ""})
            p["count"] += 1
            t = e.get("text") or e.get("tool") or ""
            if t:
                p["last"] = str(t)[:160]
    return progress


class TaskStore:
    def __init__(self):
        self._tasks: dict[str, TaskState] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._runners: dict[str, Any] = {}

    def create(self, goal: str, clarifications: dict[str, str], workspace: str) -> TaskState:
        tid = uuid.uuid4().hex[:12]
        state = TaskState(id=tid, goal=goal, clarifications=clarifications, workspace=workspace)
        self._tasks[tid] = state
        self._persist(state)
        return state

    def get(self, tid: str) -> TaskState | None:
        return self._tasks.get(tid)

    def all(self) -> list[TaskState]:
        return list(self._tasks.values())

    def append_log(self, tid: str, entry: dict):
        if tid not in self._tasks:
            return
        entry.setdefault("ts", time.time())
        self._tasks[tid].log.append(entry)
        try:
            db.append_log(tid, entry.get("kind", "?"), entry, entry.get("ts"))
        except Exception:
            pass
        for q in list(self._subscribers.get(tid, [])):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def set_status(self, tid: str, status: str):
        if tid not in self._tasks:
            return
        self._tasks[tid].status = status
        if status in ("done", "failed", "abandoned", "cancelled"):
            self._tasks[tid].finished_at = time.time()
        self._persist(self._tasks[tid])
        for q in list(self._subscribers.get(tid, [])):
            try:
                q.put_nowait({"kind": "status", "status": status, "ts": time.time()})
            except asyncio.QueueFull:
                pass

    def set_escalation(self, tid: str, escalation: dict):
        if tid not in self._tasks:
            return
        s = self._tasks[tid]
        s.escalation = escalation
        s.escalation_set_at = time.time()
        s.status = "escalated"
        s.escalation_event.clear()
        s.escalation_answer = None
        self._persist(s)
        for q in list(self._subscribers.get(tid, [])):
            try:
                q.put_nowait({"kind": "escalation", "escalation": escalation, "ts": time.time()})
            except asyncio.QueueFull:
                pass

    def answer_escalation(self, tid: str, answer: str) -> bool:
        s = self._tasks.get(tid)
        if not s or s.status != "escalated":
            return False
        s.escalation_answer = answer.lower().strip()
        s.escalation_event.set()
        return True

    def add_cost(self, tid: str, in_tokens: int = 0, out_tokens: int = 0, claude_usd: float = 0.0):
        s = self._tasks.get(tid)
        if not s:
            return
        s.cost_databricks_in += in_tokens
        s.cost_databricks_out += out_tokens
        s.cost_claude_usd += claude_usd

    def subscribe(self, tid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.setdefault(tid, []).append(q)
        s = self._tasks.get(tid)
        if s:
            for entry in s.log[-30:]:
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    break
        return q

    def unsubscribe(self, tid: str, q: asyncio.Queue):
        subs = self._subscribers.get(tid, [])
        if q in subs:
            subs.remove(q)

    def register_runner(self, tid: str, runner):
        self._runners[tid] = runner

    def unregister_runner(self, tid: str):
        self._runners.pop(tid, None)

    def cancel(self, tid: str) -> bool:
        runner = self._runners.get(tid)
        interrupted_any = False
        if runner is not None:
            try:
                runner.interrupt()
                interrupted_any = True
            except Exception:
                pass
        # V3.5 audit fix: also interrupt every live DAG step runner. Without
        # this, /cancel returned ok=True while parallel subprocesses kept
        # running because the supervisor's `self.runner` wasn't the one in use.
        try:
            import dag_executor
            n = dag_executor.interrupt_all_for(tid)
            if n:
                interrupted_any = True
                print(f"[cancel] interrupted {n} DAG step runners for {tid}")
        except Exception as e:
            print(f"[cancel] dag interrupt error: {e}")
        if not interrupted_any:
            return False
        self.append_log(tid, {"kind": "cancelled", "ts": time.time()})
        self.set_status(tid, "cancelled")
        return True

    def _persist(self, state: TaskState):
        try:
            db.upsert_task(state)
        except Exception as e:
            print(f"[task_store] persist error: {e}")

    def hydrate_from_db(self):
        """Load recent unfinished tasks from DB so /tasks shows them after restart."""
        try:
            rows = db.list_tasks(limit=200)
        except Exception:
            return
        import json as _json

        def _as_obj(v, default):
            # psycopg2 returns JSONB columns already-parsed (dict/list).
            # Be defensive in case the column comes back as a string.
            if v is None or v == "":
                return default
            if isinstance(v, (dict, list)):
                return v
            try:
                return _json.loads(v)
            except Exception:
                return default

        for r in rows:
            if r["id"] in self._tasks:
                continue
            full = db.load_task(r["id"])
            if not full:
                continue
            row = full["row"]
            state = TaskState(
                id=row["id"],
                goal=row["goal"] or "",
                clarifications=_as_obj(row["clarifications"], {}),
                workspace=row["workspace"] or "",
                status=row["status"] or "unknown",
                skill_md=row["skill_md"] or "",
                skill_name=row["skill_name"] or "",
                skill_description=row["skill_description"] or "",
                brief=row["brief"] or "",
                result=_as_obj(row["result"], None),
                started_at=row["started_at"] or time.time(),
                finished_at=row["finished_at"],
                cost_databricks_in=row["cost_databricks_in"] or 0,
                cost_databricks_out=row["cost_databricks_out"] or 0,
                cost_claude_usd=row["cost_claude_usd"] or 0.0,
                keep_workspace=bool(row["keep_workspace"]),
            )
            for l in full["logs"][-100:]:
                p = _as_obj(l["payload"], None)
                if p is not None:
                    state.log.append(p)
            in_flight = state.status in (
                "pending", "skilling", "briefing",
                "executing_loop_1", "executing_loop_2", "executing_loop_3",
                "reviewing_loop_1", "reviewing_loop_2", "reviewing_loop_3",
            )
            if in_flight:
                # V3: don't auto-abandon. Mark as 'interrupted' and let the user decide
                # via /task/{id}/resume to attempt a Claude --resume from the captured session_id.
                state.status = "interrupted"
            self._tasks[state.id] = state


STORE = TaskStore()
