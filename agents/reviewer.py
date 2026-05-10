"""Reviewer — independent QA LLM role.

The reviewer is deliberately blind to the orchestrator's brief. It sees:
  • the user's goal
  • the action log (what Claude actually did)
  • the workspace artifacts (what Claude actually produced)
  • the SKILL.md (the public plan)

It does NOT see the orchestrator's executor brief. The blindness is the
whole point — if the reviewer reads the brief, they grade against the
brief instead of against the goal, which lets a brief-following but
goal-missing run pass review. Two phases:

  review_action  — per-action sanity check. Returns {decision, message}.
                   Used by the supervisor mid-run to flag drift early.
  final_review   — full pass/fail at end of a loop. Returns
                   {passed, summary, issues, next_steps}. The
                   supervisor uses `passed=true AND issues==[]` as the
                   one-bit pass signal.
"""
import json

from .llm import _chat, _extract_json
from .prompts import _load_prompt, _load_skills


class Reviewer:
    """Independent QA reviewer — sees only goal + action log + Skill.md.
    Blind to the brief."""

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

    def final_review(
        self,
        goal: str,
        action_log: str,
        workspace: str | None = None,
        inline_skill: str = "",
    ) -> dict:
        """Full end-of-loop review. Returns
        {passed, summary, issues, next_steps}.

        Hard rules enforced before returning:
          1. passed=true requires BOTH (a) demonstrable verification
             evidence AND (b) zero issues.
          2. summary is REQUIRED — fall back to a 'reviewer did not
             produce a summary' issue if missing.
          3. next_steps is REQUIRED whenever passed=true.
          4. If only skills/SKILL.md exists and the goal asked for
             something else, mark passed=false.
          5. If a named deliverable file is missing from the workspace,
             mark passed=false.
        """
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
