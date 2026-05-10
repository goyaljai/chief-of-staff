"""Prompt + skills loaders for orchestrator and reviewer.

Two on-disk sources feed the LLM system context:

  prompts/orchestrator.md    — orchestrator's system prompt (manager role)
  prompts/reviewer.md        — reviewer's system prompt (independent QA)
  skills/global.md           — universal patterns + auto-promoted lessons
                               (rendered from the skill_lessons table; see
                               agents/lessons.py:_rerender_global_md)

The Learned section at the bottom of skills/global.md is appended each
time a task promotes a generic lesson, sorted by frequency descending.
That section grows unbounded over time, so we cap it at
_LEARNED_SECTION_CHAR_BUDGET via _truncate_learned_section before injecting
into prompts. Truncation drops lowest-frequency entries first — exactly
the right priority since they're the least-impactful.

Public API:
  _load_prompt(name)             — read prompts/{name}.md
  _load_skills(workspace, inline_skill="") — assemble the skills_context
                                              for the LLM call
  GLOBAL_SKILL_PATH, LEARNED_HEADER, MAX_LEARNED_ENTRIES — surfaced for
                                              callers that re-render or
                                              parse global.md (lessons.py,
                                              regression tests).
"""
from pathlib import Path

from config import PROJECT_ROOT, PROMPTS_DIR


SKILLS_DIR = PROJECT_ROOT / "skills"
GLOBAL_SKILL_PATH = SKILLS_DIR / "global.md"
LEARNED_HEADER = "## Learned from past runs"
MAX_LEARNED_ENTRIES = 50

# ≈ 2k tokens; tune as the lesson library grows. Truncation always drops
# the LOWEST-frequency entries first, so the cap costs us least-impactful
# rules first.
_LEARNED_SECTION_CHAR_BUDGET = 8000


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text()


def _load_skills(workspace: str | Path | None = None, inline_skill: str = "") -> str:
    """Assemble the skills context to inject into the system prompt.

    The returned string is two markdown sections:
      ## Global skills (universal patterns)
      <prelude + truncated Learned section>
      ---
      ## Skill brief for THIS task (your private playbook)
      <inline_skill, when supplied>
    """
    parts: list[str] = []
    if GLOBAL_SKILL_PATH.exists():
        text = GLOBAL_SKILL_PATH.read_text()
        text = _truncate_learned_section(text, _LEARNED_SECTION_CHAR_BUDGET)
        parts.append(f"## Global skills (universal patterns)\n\n{text}")
    if inline_skill:
        parts.append(
            f"## Skill brief for THIS task (your private playbook)\n\n{inline_skill}"
        )
    return "\n\n---\n\n".join(parts)


def _truncate_learned_section(text: str, char_budget: int) -> str:
    """Keep the hand-written prelude intact; cap only the Learned section
    so prompt size doesn't grow unbounded as skill_lessons accumulates.
    Because the Learned section is sorted by frequency desc, truncation
    drops the least-impactful (lowest-frequency) entries first — exactly
    what we want."""
    if LEARNED_HEADER not in text:
        return text
    prelude, _, learned = text.partition(LEARNED_HEADER)
    if len(learned) <= char_budget:
        return text
    head = learned[:char_budget]
    # Don't cut a bullet mid-line.
    cut = head.rfind("\n- ")
    if cut > 0:
        head = head[:cut]
    return (
        prelude
        + LEARNED_HEADER
        + head
        + "\n\n_…older low-frequency lessons omitted to fit prompt budget…_\n"
    )
