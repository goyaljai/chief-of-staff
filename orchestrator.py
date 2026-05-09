"""Orchestrator + Reviewer — both run on Databricks via OpenAI SDK.
Two LLM roles, two prompts, one underlying model.

The HUMAN USER is the Supervisor (the boss). The LLMs serve them:
  Orchestrator (manager): asks questions, builds brief, builds correction prompt,
                          handles auto-resolve when escalation times out.
  Reviewer (independent QA):  per-action review, final review.
                              Sees only goal + action stream — blind to brief details.
"""
import json
import re
from openai import OpenAI

from config import (
    DATABRICKS_TOKEN,
    DATABRICKS_BASE_URL,
    DATABRICKS_MODEL,
    PROMPTS_DIR,
    PROJECT_ROOT,
)

SKILLS_DIR = PROJECT_ROOT / "skills"

SKILL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "android": ("android", "kotlin", "gradle", "apk", "androidmanifest", "jetpack", "compose"),
    "python": ("python", " pip ", "pytest", "django", "flask", "fastapi", "venv", "poetry"),
    "web": ("react", "next.js", "vue", "npm", "yarn", "package.json", "typescript", "javascript"),
    "data": ("sql", "pandas", "dataframe", "etl", "warehouse", "dataset", "bigquery", "snowflake"),
    "research": ("research", "compare", "investigate", "find out", "analyze", "report on"),
}


def _client() -> OpenAI:
    return OpenAI(api_key=DATABRICKS_TOKEN, base_url=DATABRICKS_BASE_URL)


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def _detect_skills(text: str) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    if (SKILLS_DIR / "general.md").exists():
        found.append("general")
    for name, keywords in SKILL_KEYWORDS.items():
        if any(k in lowered for k in keywords) and (SKILLS_DIR / f"{name}.md").exists():
            found.append(name)
    return found


def _load_skills(text: str) -> str:
    names = _detect_skills(text)
    if not names:
        return ""
    parts = []
    for name in names:
        path = SKILLS_DIR / f"{name}.md"
        parts.append(f"## Skill applied: {name}\n\n{path.read_text()}")
    return "\n\n---\n\n".join(parts)


def _chat(system: str, user: str, max_tokens: int = 2048, skills_context: str = "") -> str:
    full_system = system
    if skills_context:
        full_system = (
            f"{system}\n\n"
            "## Domain skills available for this task\n"
            "These are accumulated learnings about quality patterns specific to the task type. "
            "Treat them as authoritative supplements to your judgment.\n\n"
            f"{skills_context}"
        )
    response = _client().chat.completions.create(
        model=DATABRICKS_MODEL,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": full_system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content or ""


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


class Orchestrator:
    """Manager role — questions, brief, correction prompts, auto-resolve."""

    def __init__(self):
        self.system = _load_prompt("orchestrator")

    def ask_clarifying_questions(self, task: str) -> list[str]:
        skills = _load_skills(task)
        user = (
            f"User's task:\n{task}\n\n"
            "Phase: Job 1. Output the JSON object exactly per the format. "
            "At most 5 questions. Fewer is better."
        )
        raw = _chat(self.system, user, max_tokens=1024, skills_context=skills)
        data = _extract_json(raw)
        return data.get("questions", [])[:5]

    def build_brief(self, task: str, clarifications: dict[str, str]) -> str:
        clarif_text = "\n".join(f"- Q: {q}\n  A: {a}" for q, a in clarifications.items())
        skills = _load_skills(task + " " + clarif_text)
        user = (
            f"User's task:\n{task}\n\n"
            f"Clarifications gathered:\n{clarif_text}\n\n"
            "Phase: Job 2. Output the brief as plain markdown. No preamble."
        )
        return _chat(self.system, user, max_tokens=4096, skills_context=skills).strip()

    def build_correction_prompt(self, brief: str, supervisor_issues: list[str]) -> str:
        issues_text = "\n".join(f"- {i}" for i in supervisor_issues) or "- (no specific issues recorded)"
        return (
            "Continue working on the original task. The independent reviewer found issues "
            "in your previous attempt that must be fixed before the work can be marked done.\n\n"
            f"Original brief:\n{brief}\n\n"
            f"Issues to fix:\n{issues_text}\n\n"
            "Address every issue. After fixing, run the verification step (build / test / "
            "whatever proves the goal was met) and show its output."
        )

    def auto_resolve_escalation(
        self,
        goal: str,
        brief: str,
        action_log_summary: str,
        question: str,
        option_a: str,
        option_b: str,
    ) -> str:
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
        raw = _chat(self.system, user, max_tokens=512)
        data = _extract_json(raw)
        choice = (data.get("choice") or "a").lower()
        return choice if choice in ("a", "b") else "a"


class Reviewer:
    """Independent QA reviewer — sees only goal + action log. Blind to the brief."""

    def __init__(self):
        self.system = _load_prompt("reviewer")

    def review_action(
        self,
        goal: str,
        tool_name: str,
        tool_input: dict,
        tool_output: str | None = None,
        recent_actions: str = "",
    ) -> dict:
        skills = _load_skills(goal)
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

    def final_review(self, goal: str, action_log: str) -> dict:
        skills = _load_skills(goal + " " + action_log[:1000])
        user = (
            f"Goal: {goal}\n\n"
            f"Full action log:\n{action_log}\n\n"
            "Phase: final review. Output JSON only.\n"
            "Set passed=true ONLY if BOTH conditions hold:\n"
            "  (a) there is demonstrable verification evidence in the log, AND\n"
            "  (b) you have NO issues to list.\n"
            "If you list ANY issues, you MUST set passed=false. Issues are blockers, not warnings. "
            "Quality issues (workarounds, masking, hardcoded values, weakened settings) are blockers."
        )
        raw = _chat(self.system, user, max_tokens=2048, skills_context=skills)
        data = _extract_json(raw)
        issues = data.get("issues", []) or []
        passed = bool(data.get("passed", False))
        if issues:
            passed = False
        return {
            "passed": passed,
            "issues": issues,
            "summary": data.get("summary", ""),
        }
