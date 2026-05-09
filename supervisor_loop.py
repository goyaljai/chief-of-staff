"""The supervision loop. Glues together:
  Orchestrator (manager LLM) → builds brief, builds corrections
  ClaudeRunner               → runs Claude headlessly, streams events
  Reviewer (independent QA)  → per-action and final reviews (blind to brief)
  TaskStore                  → state, escalation, log
The HUMAN USER is the Supervisor. This loop runs on their behalf.
"""
import asyncio
import json
import re
import time
from pathlib import Path

from claude_runner import ClaudeEvent, ClaudeRunner
from config import (
    ESCALATION_AUTO_RESOLVE_SECS,
    LOG_ROOT,
    MAX_CORRECTION_LOOPS,
)
from orchestrator import Orchestrator, Reviewer
from task_store import STORE, TaskState

REVIEW_TOOLS = {"Bash"}


def _summarize_log(log: list[dict], limit: int = 80) -> str:
    lines = []
    for entry in log[-limit:]:
        kind = entry.get("kind")
        if kind == "tool_use":
            tname = entry.get("tool")
            tinput = json.dumps(entry.get("input") or {})[:200]
            lines.append(f"[tool] {tname}: {tinput}")
        elif kind == "tool_result":
            out = (entry.get("output") or "")[:200]
            err = " (ERROR)" if entry.get("is_error") else ""
            lines.append(f"[result]{err} {out}")
        elif kind == "text":
            lines.append(f"[claude] {(entry.get('text') or '')[:200]}")
        elif kind == "reviewer":
            lines.append(f"[reviewer:{entry.get('decision')}] {(entry.get('message') or '')[:200]}")
        elif kind == "hook":
            lines.append(f"[hook:{entry.get('decision')}] {entry.get('tool')} -> {(entry.get('reason') or '')[:120]}")
    return "\n".join(lines) or "(no actions)"


def _parse_escalation(message: str) -> dict:
    lines = message.strip().splitlines()
    option_a = next((re.sub(r"^[\s\*\-]*A[\)\.]\s*", "", l).strip()
                     for l in lines if re.match(r"^[\s\*\-]*A[\)\.]", l)),
                    "Proceed as planned")
    option_b = next((re.sub(r"^[\s\*\-]*B[\)\.]\s*", "", l).strip()
                     for l in lines if re.match(r"^[\s\*\-]*B[\)\.]", l)),
                    "Stop and wait for clarification")
    return {"question": message, "option_a": option_a, "option_b": option_b}


def _read_hook_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except Exception:
        return []


class SupervisorLoop:
    def __init__(self, task: TaskState):
        self.task = task
        self.orchestrator = Orchestrator()
        self.reviewer = Reviewer()
        self.workspace = Path(task.workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.hook_log = LOG_ROOT / f"{task.id}.hook.jsonl"
        self.runner = ClaudeRunner(working_dir=self.workspace, hook_log_path=self.hook_log)
        self._action_count = 0
        self._review_pending: dict | None = None

    async def run(self):
        STORE.set_status(self.task.id, "briefing")
        STORE.append_log(self.task.id, {"kind": "phase", "phase": "build_brief"})

        try:
            self.task.brief = self.orchestrator.build_brief(self.task.goal, self.task.clarifications)
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "error", "where": "build_brief", "msg": str(e)})
            STORE.set_status(self.task.id, "failed")
            self.task.result = {"success": False, "error": f"build_brief failed: {e}"}
            return

        STORE.append_log(self.task.id, {"kind": "brief", "text": self.task.brief[:500]})

        prompt = self.task.brief
        session_id: str | None = None

        for loop_num in range(1, MAX_CORRECTION_LOOPS + 1):
            STORE.set_status(self.task.id, f"executing_loop_{loop_num}")
            STORE.append_log(self.task.id, {"kind": "phase", "phase": f"loop_{loop_num}"})

            try:
                result = await self.runner.run(
                    prompt=prompt,
                    session_id=session_id,
                    on_event=self._on_event,
                )
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "error", "where": "claude_run", "msg": str(e)})
                STORE.set_status(self.task.id, "failed")
                self.task.result = {"success": False, "error": f"claude run failed: {e}"}
                return

            session_id = result.session_id

            for entry in _read_hook_log(self.hook_log):
                STORE.append_log(self.task.id, {"kind": "hook", **entry})
            try:
                self.hook_log.write_text("")
            except Exception:
                pass

            if self.task.status == "escalated":
                resolved = await self._await_escalation()
                if resolved:
                    prompt = self._build_post_escalation_prompt()
                    continue
                else:
                    self.task.result = {"success": False, "error": "escalation unresolved"}
                    STORE.set_status(self.task.id, "failed")
                    return

            STORE.set_status(self.task.id, f"reviewing_loop_{loop_num}")
            review = self.reviewer.final_review(
                goal=self.task.goal,
                action_log=_summarize_log(self.task.log, limit=200),
            )
            STORE.append_log(self.task.id, {
                "kind": "final_review",
                "passed": review["passed"],
                "issues": review["issues"],
                "summary": review["summary"],
            })

            if review["passed"]:
                STORE.set_status(self.task.id, "done")
                self.task.result = {
                    "success": True,
                    "summary": review["summary"],
                    "loops": loop_num,
                    "corrections_made": len(self.task.corrections),
                    "workspace": str(self.workspace),
                }
                return

            self.task.corrections.extend(review["issues"])
            STORE.append_log(self.task.id, {"kind": "correction", "issues": review["issues"]})

            if loop_num == MAX_CORRECTION_LOOPS:
                STORE.set_status(self.task.id, "failed")
                self.task.result = {
                    "success": False,
                    "best_effort": True,
                    "summary": review["summary"],
                    "issues": review["issues"],
                    "loops": loop_num,
                    "workspace": str(self.workspace),
                }
                return

            prompt = self.orchestrator.build_correction_prompt(self.task.brief, review["issues"])

    def _on_event(self, event: ClaudeEvent):
        if event.type == "init":
            STORE.append_log(self.task.id, {"kind": "init", "session_id": event.session_id})
            return

        if event.type == "text":
            STORE.append_log(self.task.id, {"kind": "text", "text": event.text or ""})
            return

        if event.type == "tool_use":
            self._action_count += 1
            STORE.append_log(self.task.id, {
                "kind": "tool_use",
                "tool": event.tool_name,
                "input": event.tool_input,
            })

            if event.tool_name in REVIEW_TOOLS:
                try:
                    review = self.reviewer.review_action(
                        goal=self.task.goal,
                        tool_name=event.tool_name or "",
                        tool_input=event.tool_input,
                        recent_actions=_summarize_log(self.task.log, limit=20),
                    )
                except Exception as e:
                    review = {"decision": "approve", "message": f"review failed: {e}"}

                STORE.append_log(self.task.id, {
                    "kind": "reviewer",
                    "tool": event.tool_name,
                    "decision": review["decision"],
                    "message": review["message"],
                })

                if review["decision"] == "escalate":
                    parsed = _parse_escalation(review["message"])
                    STORE.set_escalation(self.task.id, parsed)
                    self.runner.interrupt()
                elif review["decision"] == "correct":
                    self.task.corrections.append(review["message"])
            return

        if event.type == "tool_result":
            STORE.append_log(self.task.id, {
                "kind": "tool_result",
                "output": event.tool_output or "",
                "is_error": event.is_error,
            })
            return

        if event.type == "result":
            STORE.append_log(self.task.id, {
                "kind": "result",
                "text": event.text or "",
                "is_error": event.is_error,
            })
            return

    async def _await_escalation(self) -> bool:
        try:
            await asyncio.wait_for(
                self.task.escalation_event.wait(),
                timeout=ESCALATION_AUTO_RESOLVE_SECS,
            )
            STORE.append_log(self.task.id, {
                "kind": "escalation_resolved",
                "answer": self.task.escalation_answer,
                "via": "user",
            })
            return True
        except asyncio.TimeoutError:
            esc = self.task.escalation or {}
            answer = self.orchestrator.auto_resolve_escalation(
                goal=self.task.goal,
                brief=self.task.brief,
                action_log_summary=_summarize_log(self.task.log, limit=40),
                question=esc.get("question", ""),
                option_a=esc.get("option_a", ""),
                option_b=esc.get("option_b", ""),
            )
            self.task.escalation_answer = answer
            STORE.append_log(self.task.id, {
                "kind": "escalation_resolved",
                "answer": answer,
                "via": "auto_orchestrator",
            })
            return True

    def _build_post_escalation_prompt(self) -> str:
        esc = self.task.escalation or {}
        chosen = esc.get("option_a") if self.task.escalation_answer == "a" else esc.get("option_b")
        STORE.set_status(self.task.id, "executing")
        return (
            "Continue the original task. The reviewer paused you with a question. "
            f"The decision is: {chosen}\n\n"
            f"Original brief:\n{self.task.brief}\n\n"
            "Resume the work using this decision."
        )


async def run_task(task_id: str):
    state = STORE.get(task_id)
    if not state:
        return
    loop = SupervisorLoop(state)
    try:
        await loop.run()
    except Exception as e:
        STORE.append_log(task_id, {"kind": "fatal", "msg": str(e)})
        STORE.set_status(task_id, "failed")
        state.result = {"success": False, "error": f"loop crashed: {e}"}
