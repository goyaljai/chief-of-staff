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
from orchestrator import Orchestrator, Reviewer, append_to_global, save_task_skill, set_usage_callback
from task_store import STORE, TaskState
import dag_executor
import rag

REVIEW_TOOLS = {"Bash", "Write", "Edit", "MultiEdit"}


def _is_reviewable_mcp_tool(tool_name: str) -> bool:
    """MCP tools that look like Bash/Write/Edit variants warrant per-action review."""
    if not tool_name or not tool_name.startswith("mcp__"):
        return False
    low = tool_name.lower()
    return any(k in low for k in ("bash", "shell", "exec", "run_command", "write", "edit", "create_file", "delete"))


def _summarize_log(log: list[dict], limit: int = 80, full_text: bool = False) -> str:
    """Build a summary of recent log entries. When full_text=True, do NOT truncate
    text/result/tool_result content — used at final_review time so the reviewer sees
    the actual artifacts, not a truncated approximation."""
    lines = []
    text_cap = 6000 if full_text else 200
    result_cap = 6000 if full_text else 200
    for entry in log[-limit:]:
        kind = entry.get("kind")
        if kind == "tool_use":
            tname = entry.get("tool")
            tinput = json.dumps(entry.get("input") or {})[:300]
            lines.append(f"[tool] {tname}: {tinput}")
        elif kind == "tool_result":
            out = (entry.get("output") or "")[:result_cap]
            err = " (ERROR)" if entry.get("is_error") else ""
            lines.append(f"[result]{err} {out}")
        elif kind == "text":
            lines.append(f"[claude] {(entry.get('text') or '')[:text_cap]}")
        elif kind == "result":
            lines.append(f"[final-text] {(entry.get('text') or '')[:text_cap]}")
        elif kind == "reviewer":
            lines.append(f"[reviewer:{entry.get('decision')}] {(entry.get('message') or '')[:300]}")
        elif kind == "hook":
            lines.append(f"[hook:{entry.get('decision')}] {entry.get('tool')} -> {(entry.get('reason') or '')[:120]}")
    return "\n".join(lines) or "(no actions)"


def _list_workspace_artifacts(workspace: Path, max_files: int = 30, max_bytes_per_file: int = 60000) -> str:
    """List interesting artifact files in the workspace and inline their content for the reviewer.
    V3 bug fix #1: cap raised from 8KB to 60KB so medium-sized markdown/code files aren't truncated.
    V3 bug fix #2: max_files raised 8 → 30. Android projects have 15-25 files; truncating at 8
    caused reviewer to say 'MainActivity.kt missing' when only gradle config files showed up.
    Skips skills/, .claude/, hidden dirs, and known build noise."""
    if not workspace.exists():
        return "(workspace missing)"
    skip_dir_names = {"skills", ".claude", ".gradle", ".idea", "build", "node_modules", "__pycache__", "venv", ".venv"}
    files: list[Path] = []
    for p in workspace.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(workspace)
        if any(part in skip_dir_names or part.startswith(".") for part in rel.parts[:-1]):
            continue
        if rel.name.startswith("."):
            continue
        files.append(p)
    files = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    if not files:
        return "(no user-facing artifacts found)"
    lines = []
    for p in files:
        try:
            size = p.stat().st_size
            content = p.read_text(errors="replace") if size < 200_000 else "(file too large)"
            content = content[:max_bytes_per_file]
            lines.append(f"### {p.relative_to(workspace)} ({size}B)\n```\n{content}\n```")
        except Exception as e:
            lines.append(f"### {p.relative_to(workspace)} (read error: {e})")
    return "\n\n".join(lines)


def _parse_escalation(message: str) -> dict:
    lines = message.strip().splitlines()
    option_a = next((re.sub(r"^[\s\*\-]*A[\)\.]\s*", "", l).strip()
                     for l in lines if re.match(r"^[\s\*\-]*A[\)\.]", l)),
                    "Proceed as planned")
    option_b = next((re.sub(r"^[\s\*\-]*B[\)\.]\s*", "", l).strip()
                     for l in lines if re.match(r"^[\s\*\-]*B[\)\.]", l)),
                    "Stop and wait for clarification")
    return {"question": message, "option_a": option_a, "option_b": option_b}


def _parse_skill_frontmatter(skill_md: str) -> tuple[str, str]:
    """Parse YAML-ish frontmatter from a SKILL.md. Returns (name, description)."""
    if not skill_md:
        return ("", "")
    lines = skill_md.splitlines()
    if not lines or not lines[0].strip().startswith("---"):
        return ("", "")
    name, desc = "", ""
    for line in lines[1:30]:
        s = line.strip()
        if s.startswith("---"):
            break
        if s.lower().startswith("name:"):
            name = s.split(":", 1)[1].strip()
        elif s.lower().startswith("description:"):
            desc = s.split(":", 1)[1].strip()
    return (name, desc)


def _detect_mcp_auth_need(tool_output: str) -> dict | None:
    """If a tool result indicates MCP needs OAuth/auth, extract the URL and return info.
    V3 hotfix: tightened to require BOTH a clear MCP-auth phrase AND an OAuth-shaped URL.
    Avoids false positives on workspace-internal URLs like http://10.0.2.2:5050/api/hello."""
    if not tool_output:
        return None
    low = tool_output.lower()
    strong_signals = (
        "open this url in their browser to authorize",
        "open this url in your browser to authorize",
        "ask the user to open this url",
        "complete the oauth flow",
        "authorize the plugin",
        "to authenticate this mcp server",
        "client_id=mcp_",
    )
    if not any(s in low for s in strong_signals):
        return None
    import re as _re
    url_match = _re.search(r"https?://[^\s\"']*(?:oauth|auth|authorize|authenticate)[^\s\"']*", tool_output, _re.IGNORECASE)
    if not url_match:
        return None
    return {"url": url_match.group(0), "snippet": tool_output[:400]}


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
        self._mcp_auth_requested: dict | None = None
        STORE.register_runner(task.id, self.runner)
        set_usage_callback(self._on_databricks_usage)

    def _on_databricks_usage(self, in_tokens: int, out_tokens: int):
        STORE.add_cost(self.task.id, in_tokens=in_tokens, out_tokens=out_tokens)

    def _should_nudge(self) -> bool:
        """Trigger a manager nudge only when: (a) >20 actions logged without a verification op,
        or (b) elapsed time on this task > 5 minutes."""
        actions = sum(1 for e in self.task.log if e.get("kind") == "tool_use")
        verify_kw = ("gradlew", "gradle", "pytest", "test ", "jest", "build", "assemble", "verify", "check", "lint")
        has_verify = any(
            (e.get("kind") == "tool_use") and any(k in (json.dumps(e.get("input") or {}).lower()) for k in verify_kw)
            for e in self.task.log
        )
        elapsed = time.time() - self.task.started_at
        return (actions > 20 and not has_verify) or elapsed > 300

    async def run(self):
        STORE.set_status(self.task.id, "skilling")
        STORE.append_log(self.task.id, {"kind": "phase", "phase": "generate_skill"})

        try:
            library_match = None
            try:
                library_match = rag.find_matching_skill(
                    self.task.goal + " " + (getattr(self.task, "skill_preview", "") or "")[:500],
                    distance_max=0.40,
                )
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "library_lookup_error", "msg": str(e)})

            if library_match:
                STORE.append_log(self.task.id, {
                    "kind": "library_match",
                    "matched_task_id": library_match.get("task_id"),
                    "distance": library_match.get("distance"),
                })

            skill_md = self.orchestrator.generate_skill_brief(
                self.task.goal,
                self.task.clarifications,
                skill_preview=getattr(self.task, "skill_preview", "") or "",
                library_match=library_match,
            )
            save_task_skill(str(self.workspace), skill_md)
            self.task.skill_md = skill_md
            name, desc = _parse_skill_frontmatter(skill_md)
            self.task.skill_name = name
            self.task.skill_description = desc
            STORE.append_log(self.task.id, {
                "kind": "skill_generated",
                "preview": skill_md[:300],
                "refined_from_preview": bool(getattr(self.task, "skill_preview", "")),
                "library_match": bool(library_match),
                "skill_name": name,
                "skill_description": desc[:200],
            })
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "error", "where": "generate_skill", "msg": str(e)})
            STORE.set_status(self.task.id, "failed")
            self.task.result = {"success": False, "error": f"generate_skill failed: {e}"}
            return

        STORE.set_status(self.task.id, "briefing")
        STORE.append_log(self.task.id, {"kind": "phase", "phase": "build_brief"})

        try:
            self.task.brief = self.orchestrator.build_brief(
                self.task.goal, self.task.clarifications, str(self.workspace),
                inline_skill=self.task.skill_md or "",
            )
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "error", "where": "build_brief", "msg": str(e)})
            STORE.set_status(self.task.id, "failed")
            self.task.result = {"success": False, "error": f"build_brief failed: {e}"}
            return

        STORE.append_log(self.task.id, {"kind": "brief", "text": self.task.brief[:500]})

        # V3.5 #A1: detect DAG brief; if present, fan-out via LangGraph.
        try:
            steps = self.orchestrator.parse_dag(self.task.brief)
        except Exception:
            steps = None
        if steps and len(steps) > 1:
            STORE.append_log(self.task.id, {"kind": "dag_detected", "steps": [s["id"] for s in steps]})
            STORE.set_status(self.task.id, "executing_dag")
            hook_log_dir = self.workspace / "_hooks"

            def _bridge_step_event(step_id: str, ev):
                # Forward every Claude event from a parallel step into the
                # task's STORE log so SSE subscribers and /task/{id} polling
                # see live progress instead of a silent gap until the DAG ends.
                try:
                    payload = {"kind": "dag_step_event", "step_id": step_id}
                    et = getattr(ev, "type", None) or getattr(ev, "kind", None)
                    if et:
                        payload["event_type"] = et
                    text = getattr(ev, "text", None)
                    if text:
                        payload["text"] = text[:1000]
                    tool = getattr(ev, "tool", None)
                    if tool:
                        payload["tool"] = tool
                    STORE.append_log(self.task.id, payload)
                except Exception:
                    pass

            try:
                # V3.5 #3: build shared context from the brief's first
                # Objective/Deliverable sections so each parallel step
                # inherits the larger goal, not just its own one-line action.
                brief = self.task.brief or ""
                shared_ctx_parts: list[str] = [
                    f"OVERALL GOAL: {self.task.goal}",
                ]
                for hdr in ("## Objective", "## Deliverable", "## What needs to be built", "## Done"):
                    idx = brief.find(hdr)
                    if idx < 0:
                        continue
                    nxt = brief.find("\n## ", idx + 1)
                    excerpt = brief[idx: nxt if nxt > 0 else idx + 1200].strip()
                    shared_ctx_parts.append(excerpt[:1200])
                shared_context = "\n\n".join(shared_ctx_parts)

                dag_result = await dag_executor.execute_dag(
                    steps, self.workspace, hook_log_dir,
                    on_step_event=_bridge_step_event,
                    exec_id=self.task.id,
                    shared_context=shared_context,
                )
                STORE.append_log(self.task.id, {
                    "kind": "dag_result",
                    "ok": dag_result.get("ok"),
                    "failed": dag_result.get("failed", []),
                })
            except Exception as e:
                STORE.append_log(self.task.id, {"kind": "error", "where": "dag_executor", "msg": str(e)})
                dag_result = {"ok": False, "error": str(e)}

            # Drain each step's isolated hook log into the task log so
            # permission-hook events from every parallel step are preserved.
            for step_result in (dag_result.get("results") or {}).values():
                step_hook_log = step_result.get("hook_log")
                if not step_hook_log:
                    continue
                try:
                    for entry in _read_hook_log(Path(step_hook_log)):
                        STORE.append_log(self.task.id, {
                            "kind": "hook",
                            "step_id": step_result.get("step_id"),
                            **entry,
                        })
                except Exception:
                    pass

            STORE.set_status(self.task.id, "reviewing_loop_1")
            full_log = _summarize_log(self.task.log, limit=300, full_text=True)
            artifacts = _list_workspace_artifacts(self.workspace)
            combined = f"{full_log}\n\nDAG result: {dag_result}\n\n=== Workspace artifacts ===\n{artifacts}"
            review = self.reviewer.final_review(goal=self.task.goal, action_log=combined, workspace=str(self.workspace))
            STORE.append_log(self.task.id, {"kind": "final_review", "passed": review["passed"], "issues": review["issues"], "summary": review["summary"]})
            if review["passed"]:
                STORE.set_status(self.task.id, "done")
                self._write_learning(1, review)
                self._index_in_rag(review)
                self._maybe_upload_trace(review)
                STORE.unregister_runner(self.task.id)
                self.task.result = {
                    "success": True,
                    "summary": review["summary"],
                    "next_steps": review.get("next_steps", ""),
                    "loops": 1,
                    "execution": "dag_parallel",
                    "workspace": str(self.workspace),
                }
                return
            else:
                # DAG failed review — fall through to normal correction loops
                self.task.corrections.extend(review["issues"])
                STORE.append_log(self.task.id, {"kind": "dag_review_failed_falling_through"})

            # V3.5 audit fix: notes added during executing_dag must not be
            # silently dropped — record them so the sequential fall-through
            # path consumes them, and so the user sees they were preserved.
            if self.task.user_notes:
                STORE.append_log(self.task.id, {
                    "kind": "notes_after_dag",
                    "notes": list(self.task.user_notes),
                    "applied": "queued_for_sequential_loop",
                })

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

            if result.cost_usd:
                STORE.add_cost(self.task.id, claude_usd=float(result.cost_usd))

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
            full_log = _summarize_log(self.task.log, limit=300, full_text=True)
            artifacts = _list_workspace_artifacts(self.workspace)
            combined = f"{full_log}\n\n=== Workspace artifacts (actual files) ===\n{artifacts}"
            review = self.reviewer.final_review(
                goal=self.task.goal,
                action_log=combined,
                workspace=str(self.workspace),
                inline_skill="",
            )
            STORE.append_log(self.task.id, {
                "kind": "final_review",
                "passed": review["passed"],
                "issues": review["issues"],
                "summary": review["summary"],
            })

            if review["passed"]:
                STORE.set_status(self.task.id, "done")
                self._write_learning(loop_num, review)
                self._index_in_rag(review)
                STORE.unregister_runner(self.task.id)
                self.task.result = {
                    "success": True,
                    "summary": review["summary"],
                    "next_steps": review.get("next_steps", ""),
                    "loops": loop_num,
                    "corrections_made": len(self.task.corrections),
                    "workspace": str(self.workspace),
                }
                self._maybe_upload_trace(review)
                return

            self.task.corrections.extend(review["issues"])
            STORE.append_log(self.task.id, {"kind": "correction", "issues": review["issues"]})

            user_notes = list(self.task.user_notes)
            if user_notes:
                self.task.user_notes.clear()
                STORE.append_log(self.task.id, {"kind": "notes_consumed", "notes": user_notes})

            if loop_num == MAX_CORRECTION_LOOPS:
                STORE.set_status(self.task.id, "failed")
                self._write_learning(loop_num, review)
                self._index_in_rag(review)
                STORE.unregister_runner(self.task.id)
                self.task.result = {
                    "success": False,
                    "best_effort": True,
                    "summary": review["summary"],
                    "issues": review["issues"],
                    "loops": loop_num,
                    "workspace": str(self.workspace),
                }
                return

            grounding = ""
            if self._should_nudge():
                try:
                    grounding = self.orchestrator.generate_grounding_nudge(
                        task=self.task.goal,
                        brief=self.task.brief,
                        action_log_summary=_summarize_log(self.task.log, limit=60),
                        loop_num=loop_num,
                    )
                    if grounding:
                        STORE.append_log(self.task.id, {"kind": "grounding_nudge", "nudge": grounding})
                except Exception as e:
                    STORE.append_log(self.task.id, {"kind": "grounding_error", "msg": str(e)})

            prompt = self.orchestrator.build_correction_prompt(
                self.task.brief, review["issues"],
                user_notes=user_notes,
                grounding_nudge=grounding,
            )

    def _maybe_inject_midloop_nudge(self):
        """V3 #8: mid-loop grounding. Every 25 tool_use events without a verification op,
        synthesize a nudge and append to corrections (will be folded into next loop's prompt
        OR — when we add true interrupt — injected directly mid-flight)."""
        actions = sum(1 for e in self.task.log if e.get("kind") == "tool_use")
        if actions == 0 or actions % 25 != 0:
            return
        recent_text = " ".join(json.dumps(e.get("input") or {}).lower() for e in self.task.log[-25:] if e.get("kind") == "tool_use")
        if any(k in recent_text for k in ("gradle", "pytest", "test ", "build", "verify", "check")):
            return
        try:
            nudge = self.orchestrator.generate_grounding_nudge(
                task=self.task.goal,
                brief=self.task.brief,
                action_log_summary=_summarize_log(self.task.log, limit=25),
                loop_num=0,
            )
            if nudge:
                self.task.corrections.append(f"[mid-loop nudge] {nudge}")
                STORE.append_log(self.task.id, {"kind": "midloop_nudge", "nudge": nudge[:300]})
        except Exception:
            pass

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
            self._maybe_inject_midloop_nudge()

            if event.tool_name == "TodoWrite":
                todos = (event.tool_input or {}).get("todos") or []
                if todos:
                    self.task.claude_plan = todos
                    STORE.append_log(self.task.id, {
                        "kind": "plan_updated",
                        "items": [{"content": t.get("content",""), "status": t.get("status","")} for t in todos],
                    })

            if event.tool_name in REVIEW_TOOLS or _is_reviewable_mcp_tool(event.tool_name or ""):
                try:
                    review = self.reviewer.review_action(
                        goal=self.task.goal,
                        tool_name=event.tool_name or "",
                        tool_input=event.tool_input,
                        recent_actions=_summarize_log(self.task.log, limit=20),
                        workspace=str(self.workspace),
                        inline_skill="",
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
                elif review["decision"] == "request_evidence":
                    self.task.corrections.append(f"[evidence-needed] {review['message']}")
            return

        if event.type == "tool_result":
            STORE.append_log(self.task.id, {
                "kind": "tool_result",
                "output": event.tool_output or "",
                "is_error": event.is_error,
            })
            mcp_auth = _detect_mcp_auth_need(event.tool_output or "")
            if mcp_auth and not self._mcp_auth_requested:
                self._mcp_auth_requested = mcp_auth
                STORE.set_escalation(self.task.id, {
                    "question": (
                        f"Claude needs to authenticate an MCP tool to continue. "
                        f"Open this URL to authorize:\n\n{mcp_auth['url']}\n\n"
                        f"After authorizing, reply A. To skip the MCP and have Claude use built-in knowledge instead, reply B."
                    ),
                    "option_a": f"I authorized — please retry the MCP call",
                    "option_b": f"Skip the MCP, use built-in knowledge / fallback approach",
                })
                self.runner.interrupt()
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

    def _maybe_upload_trace(self, review: dict):
        """V3 #11: opt-in anonymized trace upload. Only fires if TRACE_UPLOAD_URL is set."""
        import os as _os
        url = _os.environ.get("TRACE_UPLOAD_URL", "").strip()
        if not url:
            return
        try:
            import urllib.request as _ur, urllib.error as _ue
            payload = {
                "task_id_hash": __import__("hashlib").sha256(self.task.id.encode()).hexdigest()[:16],
                "goal": self.task.goal,
                "loops": self.task.loop_count if hasattr(self.task, "loop_count") else None,
                "passed": review.get("passed", False),
                "summary": review.get("summary", ""),
                "skill_md": self.task.skill_md,
                "cost_in": self.task.cost_databricks_in,
                "cost_out": self.task.cost_databricks_out,
            }
            data = __import__("json").dumps(payload).encode()
            req = _ur.Request(url, data=data, headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_os.environ.get('TRACE_UPLOAD_TOKEN','')}",
            })
            _ur.urlopen(req, timeout=5).read()
            STORE.append_log(self.task.id, {"kind": "trace_uploaded"})
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "trace_upload_error", "msg": str(e)})

    def _index_in_rag(self, review: dict):
        try:
            summary = review.get("summary", "") or ""
            rag.index_task(
                task_id=self.task.id,
                goal=self.task.goal,
                summary=summary,
                skill_md=self.task.skill_md or "",
            )
            if self.task.skill_description:
                rag.index_skill(
                    task_id=self.task.id,
                    name=self.task.skill_name,
                    description=self.task.skill_description,
                    skill_md=self.task.skill_md or "",
                )
            STORE.append_log(self.task.id, {"kind": "rag_indexed"})
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "rag_index_error", "msg": str(e)})

    def _write_learning(self, loop_num: int, review: dict):
        try:
            lessons = self.orchestrator.find_promotable_lessons(
                task=self.task.goal,
                skill_md=getattr(self.task, "skill_md", "") or "",
                brief=self.task.brief,
                action_log_summary=_summarize_log(self.task.log, limit=80),
                review_summary=review.get("summary", ""),
                review_issues=review.get("issues", []) or [],
                passed=bool(review.get("passed")),
                workspace=str(self.workspace),
            )
            if not lessons:
                STORE.append_log(self.task.id, {"kind": "learning_skipped", "reason": "no promotable lessons"})
                return
            added = append_to_global(lessons, origin_task_id=self.task.id)
            STORE.append_log(self.task.id, {
                "kind": "learning_promoted",
                "added_new": added,
                "promoted_total": len(lessons),
                "lessons": lessons,
            })
        except Exception as e:
            STORE.append_log(self.task.id, {"kind": "learning_error", "msg": str(e)})

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
