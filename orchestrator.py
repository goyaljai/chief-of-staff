"""Orchestrator + Reviewer — both run on Databricks via OpenAI SDK.

V2 dynamic skills model:
- Per-task Skill.md generated fresh by orchestrator (meta-thinking)
- Stored at <workspace>/skills/Skill.md
- Global skill at chief-of-staff/skills/global.md grows via auto-promotion
- No keyword-based domain detection. The orchestrator decides what each task needs.

The HUMAN USER is the Supervisor. The LLMs serve them:
  Orchestrator (manager): meta-thinks Skill.md, asks Qs, builds brief, builds corrections,
                          finds promotable lessons after task completion.
  Reviewer (independent QA):  per-action review, final review.
                              Sees only goal + action stream + Skill.md (the public plan).
"""
import json
import re
from pathlib import Path

from openai import OpenAI

from config import (
    DATABRICKS_BASE_URL,
    DATABRICKS_MODEL,
    DATABRICKS_TOKEN,
    PROJECT_ROOT,
    PROMPTS_DIR,
)

SKILLS_DIR = PROJECT_ROOT / "skills"
GLOBAL_SKILL_PATH = SKILLS_DIR / "global.md"
LEARNED_HEADER = "## Learned from past runs"
MAX_LEARNED_ENTRIES = 50


_LANGSMITH_TRACED = False


def _maybe_init_langsmith() -> bool:
    """V3 #14: enable LangSmith tracing if env vars say so. Wraps OpenAI client globally."""
    global _LANGSMITH_TRACED
    if _LANGSMITH_TRACED:
        return True
    import os as _os
    if _os.environ.get("LANGSMITH_TRACING", "").lower() not in ("true", "1", "yes"):
        return False
    if not _os.environ.get("LANGSMITH_API_KEY"):
        print("[langsmith] LANGSMITH_TRACING=true but LANGSMITH_API_KEY missing; skipping")
        return False
    try:
        from langsmith.wrappers import wrap_openai  # noqa: F401
        _LANGSMITH_TRACED = True
        print(f"[langsmith] tracing enabled (project={_os.environ.get('LANGSMITH_PROJECT', 'chief-of-staff')})")
        return True
    except Exception as e:
        print(f"[langsmith] failed to init: {e}")
        return False


def _client() -> OpenAI:
    base = OpenAI(
        api_key=DATABRICKS_TOKEN,
        base_url=DATABRICKS_BASE_URL,
        max_retries=5,
        timeout=120.0,
    )
    if _maybe_init_langsmith():
        try:
            from langsmith.wrappers import wrap_openai
            return wrap_openai(base)
        except Exception:
            return base
    return base


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def _load_skills(workspace: str | Path | None = None, inline_skill: str = "") -> str:
    parts: list[str] = []
    if GLOBAL_SKILL_PATH.exists():
        parts.append(f"## Global skills (universal patterns)\n\n{GLOBAL_SKILL_PATH.read_text()}")
    if inline_skill:
        parts.append(f"## Skill brief for THIS task (your private playbook)\n\n{inline_skill}")
    return "\n\n---\n\n".join(parts)


import contextvars
_USAGE_CALLBACK_CTX: contextvars.ContextVar = contextvars.ContextVar("usage_callback", default=None)


def set_usage_callback(cb):
    """V3: register a per-context callback (via contextvars). Each task's supervisor loop
    runs in its own asyncio.Task with its own context — callbacks don't leak across parallel tasks."""
    _USAGE_CALLBACK_CTX.set(cb)


def _chat(system: str, user: str, max_tokens: int = 2048, skills_context: str = "") -> str:
    full_system = system
    if skills_context:
        full_system = (
            f"{system}\n\n"
            "## Available skills / knowledge for this task\n"
            "Treat these as authoritative knowledge supplementing your judgment.\n\n"
            f"{skills_context}"
        )

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            response = _client().chat.completions.create(
                model=DATABRICKS_MODEL,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": full_system},
                    {"role": "user", "content": user},
                ],
            )
            try:
                cb = _USAGE_CALLBACK_CTX.get()
                if cb and getattr(response, "usage", None):
                    cb(
                        getattr(response.usage, "prompt_tokens", 0) or 0,
                        getattr(response.usage, "completion_tokens", 0) or 0,
                    )
            except Exception:
                pass
            return response.choices[0].message.content or ""
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            transient = any(s in err_str for s in (
                "rate limit", "rate_limit", "429",
                "503", "502", "504", "timeout", "timed out",
                "connection", "temporary", "overloaded",
            ))
            if not transient or attempt == 2:
                raise
            wait = 2 ** (attempt + 1)
            print(f"[orchestrator] transient Databricks error (attempt {attempt+1}): {e}. retrying in {wait}s")
            import time as _time
            _time.sleep(wait)
    if last_err:
        raise last_err
    return ""


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def append_to_global(lessons: list[str]) -> int:
    """Append lessons to skills/global.md under the Learned section. Dedupes. Caps at MAX_LEARNED_ENTRIES."""
    if not lessons:
        return 0
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    existing = GLOBAL_SKILL_PATH.read_text() if GLOBAL_SKILL_PATH.exists() else "# Global skills\n\n"

    if LEARNED_HEADER not in existing:
        existing = existing.rstrip() + f"\n\n{LEARNED_HEADER}\n\n"

    head, _, tail = existing.partition(LEARNED_HEADER)
    learned_section = (LEARNED_HEADER + tail).strip()
    lines = [l for l in learned_section.splitlines() if l.startswith("- ")]
    seen = set(lines)
    added = 0
    for l in lessons:
        clean = l.strip().lstrip("-").strip()
        if not clean:
            continue
        new_line = f"- {clean}"
        if new_line in seen:
            continue
        lines.append(new_line)
        seen.add(new_line)
        added += 1
    if len(lines) > MAX_LEARNED_ENTRIES:
        lines = lines[-MAX_LEARNED_ENTRIES:]

    rebuilt = head.rstrip() + "\n\n" + LEARNED_HEADER + "\n\n" + "\n".join(lines) + "\n"
    GLOBAL_SKILL_PATH.write_text(rebuilt)
    return added


def save_task_skill(workspace: str | Path, skill_md: str) -> Path | None:
    """V2.5+: SKILL.md is no longer materialized in the workspace — it confused Claude
    (which treated it as a deliverable). The skill_md string lives only in task state + DB,
    and is injected into orchestrator/reviewer prompts via _load_skills(). This function is
    kept as a no-op so callers don't break."""
    return None


class Orchestrator:
    """Manager role — meta-thinking, questions, brief, correction prompts, auto-resolve."""

    def __init__(self):
        self.system = _load_prompt("orchestrator")

    def think_and_ask(self, task: str) -> dict:
        """Phase 1 — meta-think + ask clarifying questions in one LLM call.
        The orchestrator first thinks about what the task NEEDS, then asks sharper Qs.
        Returns {'skill_preview': str, 'questions': [str]}.
        """
        skills = _load_skills()
        user = (
            "Phase 1 — meta-think + clarify.\n\n"
            f"User's task:\n{task}\n\n"
            "Step A: think about what this task actually requires. Identify:\n"
            "  - the kind of work (code build, research, writing, data, ops, etc.)\n"
            "  - the likely 'done' criteria\n"
            "  - failure patterns specific to THIS kind of task\n"
            "  - what verification will prove the goal was met\n"
            "  - gotchas and edge cases\n\n"
            "Step B: propose 3-5 clarifying questions that GROUND the LLM's understanding of the user's intent. "
            "Even when you think you can decide an answer yourself, ask anyway — questions force the user to commit to specifics, "
            "which reduces hallucination and drift during execution. Aim for 3-5 questions, not 1-2.\n\n"
            "Output JSON exactly:\n"
            '{"skill_preview": "<concise markdown summary of your meta-thinking — what this task needs>", '
            '"questions": ["...", "..."]}\n'
            "Keep skill_preview under 1200 chars. Questions: 0-5 items, fewer is better."
        )
        raw = _chat(self.system, user, max_tokens=2048, skills_context=skills)
        data = _extract_json(raw)
        return {
            "skill_preview": (data.get("skill_preview") or "").strip(),
            "questions": (data.get("questions") or [])[:5],
        }

    def generate_skill_brief(
        self,
        task: str,
        clarifications: dict[str, str],
        skill_preview: str = "",
        library_match: dict | None = None,
    ) -> str:
        """Phase 2 — produce the final SKILL.md given task + answers.
        Follows Anthropic's skill-creator format: YAML frontmatter + body.

        If skill_preview (from think_and_ask) is provided, refine it instead of regenerating —
        saves an LLM round of meta-thinking.
        """
        skills = _load_skills()
        clarif_text = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications.items()) or "(none)"
        library_section = ""
        if library_match:
            library_section = (
                "\n\nA past task had a similar SKILL.md you can reuse as a starting point "
                f"(matched on description, distance={library_match.get('distance', 0):.3f}):\n\n"
                f"{(library_match.get('meta') or {}).get('skill_md_preview', '')[:1500]}\n\n"
                "Refine that for THIS task. If the past skill doesn't actually fit, ignore it.\n"
            )
        if skill_preview:
            user = (
                "Phase 2 — refine your earlier meta-thinking into the final SKILL.md.\n\n"
                f"Task: {task}\n\n"
                f"Your earlier preview (from question-asking phase):\n{skill_preview}\n"
                f"{library_section}\n"
                f"Clarifications you got from the user:\n{clarif_text}\n\n"
                "REFINE the preview into a final SKILL.md, incorporating what the clarifications tell you. "
                "Don't restart from zero — reuse the structure and insights from the preview, sharpen them with the answers.\n\n"
                "Output the SKILL.md exactly in this format:\n\n"
                "```\n"
                "---\n"
                "name: <short-kebab-case-id-derived-from-task>\n"
                "description: <one sentence saying when to apply this skill — be 'pushy' (Anthropic skill-creator convention) so it triggers reliably on similar future tasks>\n"
                "---\n\n"
                "# <Title>\n\n"
                "## Objective\n<concrete goal for THIS task>\n\n"
                "## What 'done' means\n<verifiable end state>\n\n"
                "## Knowledge / standards that apply\n<only what's relevant>\n\n"
                "## Failure patterns to watch for\n<specific to this kind of work>\n\n"
                "## Verification required\n<what proof you'll demand>\n\n"
                "## Gotchas\n<common mistakes>\n\n"
                "## Scope boundaries\n<in / out>\n"
                "```\n\n"
                "Output only the SKILL.md content (frontmatter + body). No preamble, no code fences around the whole thing."
            )
        else:
            user = (
                "Phase 2 — generate the SKILL.md for this task (Anthropic skill-creator format).\n\n"
                f"Task: {task}\n\n"
                f"{library_section}\n"
                f"Clarifications:\n{clarif_text}\n\n"
                "Output the SKILL.md exactly in this format:\n\n"
                "```\n"
                "---\n"
                "name: <short-kebab-case-id-derived-from-task>\n"
                "description: <one sentence saying when to apply this skill — be 'pushy' so it triggers reliably on similar future tasks>\n"
                "---\n\n"
                "# <Title>\n\n"
                "## Objective\n## What 'done' means\n## Knowledge / standards that apply\n"
                "## Failure patterns to watch for\n## Verification required\n## Gotchas\n## Scope boundaries\n"
                "```\n\n"
                "Be specific to THIS task. Output only the SKILL.md content. No preamble."
            )
        return _chat(self.system, user, max_tokens=3500, skills_context=skills).strip()

    def parse_dag(self, brief: str) -> list[dict] | None:
        """V3 #6: parse a DAG out of the brief if the brief has a 'Steps' section.
        Each step has id, action, depends_on (list). Returns None if brief is plain prose.
        Used by V3 #7 to fan out independent steps."""
        import re as _re
        m = _re.search(r"##\s+(?:Steps|DAG|Plan)\s*\n(.*?)(?:\n##\s|\Z)", brief, _re.IGNORECASE | _re.DOTALL)
        if not m:
            return None
        body = m.group(1)
        steps = []
        for line in body.splitlines():
            mm = _re.match(r"^\s*[-*]?\s*([A-Za-z0-9_\-]+):\s*(.+?)(?:\s*\(deps?:\s*([^)]*)\))?\s*$", line)
            if mm:
                deps = [d.strip() for d in (mm.group(3) or "").split(",") if d.strip()]
                steps.append({"id": mm.group(1), "action": mm.group(2).strip(), "depends_on": deps})
        return steps or None

    def build_brief(self, task: str, clarifications: dict[str, str], workspace: str, inline_skill: str = "") -> str:
        """Build the executor brief. The brief is what gets passed to Claude Code as the prompt."""
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        clarif_text = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications.items()) or "(none)"
        user = (
            "Phase 3 — build the executor brief.\n\n"
            f"Task: {task}\n\n"
            f"Clarifications:\n{clarif_text}\n\n"
            "Write a precise brief that an executor (Claude Code) can run with no further questions. "
            "Use the Skill brief above as your authoritative guide. Structure:\n"
            "- Objective\n"
            "- **Deliverable file(s)** — exact filename(s) the executor must produce in the workspace. Be explicit.\n"
            "- What needs to be built / produced\n"
            "- Done / acceptance criteria (numbered, verifiable)\n"
            "- Constraints\n"
            "- Quality bar\n"
            "- Recommended implementation shape (only if useful)\n\n"
            "Be explicit about deliverable files. The executor's job is to produce those files. "
            "The workspace will be empty when the executor starts — anything it should produce, name it explicitly.\n\n"
            "Output markdown only, no preamble."
        )
        return _chat(self.system, user, max_tokens=4096, skills_context=skills).strip()

    def build_correction_prompt(
        self,
        brief: str,
        supervisor_issues: list[str],
        user_notes: list[str] | None = None,
        grounding_nudge: str = "",
    ) -> str:
        issues_text = "\n".join(f"- {i}" for i in supervisor_issues) or "- (no specific issues recorded)"
        notes_section = ""
        if user_notes:
            notes_text = "\n".join(f"- {n}" for n in user_notes)
            notes_section = (
                "\n\nThe user also added these notes mid-task — fold them into your continued work:\n"
                f"{notes_text}\n"
            )
        nudge_section = ""
        if grounding_nudge:
            nudge_section = (
                "\n\n--- Manager check-in (1:1 from your orchestrator) ---\n"
                f"{grounding_nudge}\n"
                "Pause and answer this in your head before you continue.\n"
                "----------------------------------------\n"
            )
        return (
            "Continue working on the original task. The independent reviewer found issues "
            "in your previous attempt that must be fixed before the work can be marked done.\n\n"
            f"Original brief:\n{brief}\n\n"
            f"Issues to fix:\n{issues_text}{notes_section}{nudge_section}\n\n"
            "Address every issue. If user notes were provided, fold them in too. "
            "After fixing, run the verification step and show its output."
        )

    def generate_grounding_nudge(self, task: str, brief: str, action_log_summary: str, loop_num: int) -> str:
        """Periodic 1:1 between orchestrator and Claude — manager-style check-in.
        Picks ONE grounding question or directive based on what Claude has done so far."""
        skills = _load_skills()
        user = (
            "You're acting as Claude Code's manager doing a brief 1:1 check-in. "
            "You've watched Claude work for a loop. Pick ONE grounding intervention that would actually help — "
            "either a question that exposes a hidden assumption, a directive to validate something specific, "
            "or a nudge to refocus on the original goal. Keep it short (1-3 sentences). Be specific to what "
            "Claude has actually done, not generic.\n\n"
            f"Original task: {task}\n\n"
            f"Brief excerpt:\n{brief[:1500]}\n\n"
            f"What Claude did in loop {loop_num} (recent actions):\n{action_log_summary[:2000]}\n\n"
            "Output JUST the nudge text — no preamble, no JSON, just the words you'd say to Claude."
        )
        return _chat(self.system, user, max_tokens=300, skills_context=skills).strip()

    def auto_resolve_escalation(
        self,
        goal: str,
        brief: str,
        action_log_summary: str,
        question: str,
        option_a: str,
        option_b: str,
        workspace: str | None = None,
    ) -> str:
        skills = _load_skills(workspace=workspace)
        user = (
            "An escalation timed out (user did not respond within 30 minutes). "
            "Make the best judgment for the user given the full context. "
            "Pick option A or option B based on which serves the goal better.\n\n"
            f"Goal: {goal}\n\n"
            f"Brief: {brief}\n\n"
            f"What has happened so far:\n{action_log_summary}\n\n"
            f"Escalation question: {question}\n"
            f"A) {option_a}\n"
            f"B) {option_b}\n\n"
            'Respond with JSON only: {"choice": "a" or "b", "rationale": "one sentence"}'
        )
        raw = _chat(self.system, user, max_tokens=512, skills_context=skills)
        data = _extract_json(raw)
        choice = (data.get("choice") or "a").lower()
        return choice if choice in ("a", "b") else "a"

    def find_promotable_lessons(
        self,
        task: str,
        skill_md: str,
        brief: str,
        action_log_summary: str,
        review_summary: str,
        review_issues: list[str],
        passed: bool,
        workspace: str | None = None,
    ) -> list[str]:
        """After a task, identify lessons GENERAL enough to apply to future different tasks.
        Filter aggressively. Return list of 0-3 actionable sentences."""
        skills = _load_skills(workspace=workspace)
        outcome = "PASSED" if passed else "FAILED"
        issues_text = "\n".join(f"- {i}" for i in review_issues[:5]) or "- (none)"
        user = (
            "Phase 4 — promote lessons.\n\n"
            "A task just completed. Identify lessons GENERAL enough to apply to future DIFFERENT tasks. "
            "Filter aggressively — most things are too specific. We promote only if a future "
            "task in a different domain would benefit. Avoid restating things already in the global skills above.\n\n"
            f"Task: {task}\n"
            f"Outcome: {outcome}\n"
            f"Skill brief that guided this task:\n{skill_md[:2000]}\n\n"
            f"Brief excerpt:\n{brief[:1000]}\n\n"
            f"Action log summary:\n{action_log_summary[:2000]}\n\n"
            f"Review summary: {review_summary}\n"
            f"Issues caught:\n{issues_text}\n\n"
            "Output JSON: {\"lessons\": [\"...\", \"...\"]}.\n"
            "Each lesson:\n"
            "  - One sentence\n"
            "  - Actionable for a future reviewer/orchestrator\n"
            "  - General — applies to a DIFFERENT task, not just a similar one\n"
            "  - NOT already in the global skills above\n"
            "Empty list is the right answer most of the time."
        )
        raw = _chat(self.system, user, max_tokens=1024, skills_context=skills)
        data = _extract_json(raw)
        lessons = data.get("lessons") or []
        return [l.strip() for l in lessons if isinstance(l, str) and l.strip()][:3]


    def answer_from_history(self, question: str, retrieved: list[dict]) -> dict:
        """Synthesize a /ask answer from retrieved past task snippets. Returns {answer, cited_task_ids}."""
        if not retrieved:
            return {"answer": "I have no past tasks matching that question yet. Try running a relevant task first.", "cited_task_ids": []}
        ctx = []
        for i, r in enumerate(retrieved[:6]):
            tid = r.get("task_id") or (r.get("meta") or {}).get("task_id") or "?"
            goal = r.get("goal") or (r.get("meta") or {}).get("goal") or ""
            snip = r.get("doc") or r.get("snip") or r.get("brief") or ""
            ctx.append(f"[Source {i+1}] task_id={tid}\nGoal: {goal}\nContent: {snip[:1500]}")
        sources_block = "\n\n".join(ctx)
        user = (
            f"User question: {question}\n\n"
            f"Past tasks retrieved from history (most relevant first):\n\n{sources_block}\n\n"
            "Answer the user's question using ONLY these sources. Be concise. "
            'If the sources cover it, give a direct answer and end with "Sources: <task_id>, <task_id>". '
            'If the sources do not cover it, say so directly — do not fabricate.\n\n'
            'Output JSON: {"answer": "...", "cited_task_ids": ["...", "..."]}'
        )
        raw = _chat(self.system, user, max_tokens=1024)
        data = _extract_json(raw)
        return {
            "answer": (data.get("answer") or raw or "").strip(),
            "cited_task_ids": data.get("cited_task_ids") or [],
        }


class Reviewer:
    """Independent QA reviewer — sees only goal + action log + Skill.md. Blind to the brief."""

    def __init__(self):
        self.system = _load_prompt("reviewer")

    def review_action(
        self,
        goal: str,
        tool_name: str,
        tool_input: dict,
        tool_output: str | None = None,
        recent_actions: str = "",
        workspace: str | None = None,
        inline_skill: str = "",
    ) -> dict:
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        user = (
            f"Goal: {goal}\n\n"
            f"Recent actions:\n{recent_actions}\n\n"
            f"Action under review:\n"
            f"  Tool: {tool_name}\n"
            f"  Input: {json.dumps(tool_input)[:500]}\n"
            f"  Output: {(tool_output or '')[:500]}\n\n"
            "Phase: per-action review. Output JSON only."
        )
        raw = _chat(self.system, user, max_tokens=512, skills_context=skills)
        data = _extract_json(raw)
        return {
            "decision": data.get("decision", "approve"),
            "message": data.get("message", ""),
        }

    def final_review(self, goal: str, action_log: str, workspace: str | None = None, inline_skill: str = "") -> dict:
        skills = _load_skills(workspace=workspace, inline_skill=inline_skill)
        user = (
            f"Goal: {goal}\n\n"
            f"Full action log + workspace artifacts:\n{action_log}\n\n"
            "Phase: final review. Output JSON only with this exact shape:\n"
            '{\n'
            '  "passed": true|false,\n'
            '  "summary": "<one paragraph: what was produced vs the goal>",\n'
            '  "issues": ["..."],\n'
            '  "next_steps": "<USER-FACING: how to actually USE the deliverable. Be specific.\n'
            '                 If it\'s a webapp: how to run it (exact command + port).\n'
            '                 If it\'s an Android app: how to build/install (./gradlew installDebug etc.).\n'
            '                 If it\'s a doc/research: where the file is + what it contains.\n'
            '                 If it depends on another service running: state that + the start command.\n'
            '                 Always include exact file paths and commands. Multi-line OK.>"\n'
            '}\n\n'
            "Rules:\n"
            "1. passed=true ONLY if BOTH (a) demonstrable verification evidence exists, AND (b) zero issues.\n"
            "2. summary is REQUIRED — describe what was produced.\n"
            "3. next_steps is REQUIRED whenever passed=true — tell the user EXACTLY how to use the deliverable.\n"
            "   Imagine you handed off to a colleague who has never seen this. What 3-5 commands or links do they need?\n"
            "4. If only skills/SKILL.md exists and the goal asked for something else, mark passed=false.\n"
            "5. If a named deliverable file doesn't exist in the workspace, mark passed=false."
        )
        raw = _chat(self.system, user, max_tokens=2048, skills_context=skills)
        data = _extract_json(raw)
        issues = data.get("issues", []) or []
        passed = bool(data.get("passed", False))
        summary = (data.get("summary") or "").strip()
        next_steps = (data.get("next_steps") or "").strip()
        if not summary:
            summary = "(reviewer did not produce a summary)"
            issues = list(issues) + ["Reviewer summary missing — re-review required."]
            passed = False
        if issues:
            passed = False
        return {
            "passed": passed,
            "issues": issues,
            "summary": summary,
            "next_steps": next_steps,
        }
