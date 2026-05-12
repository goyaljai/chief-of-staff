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
import hashlib
import os
from pathlib import Path

from config import PROJECT_ROOT, PROMPTS_DIR


SKILLS_DIR = PROJECT_ROOT / "skills"
GLOBAL_SKILL_PATH = SKILLS_DIR / "global.md"
LEARNED_HEADER = "## Learned from past runs"
MAX_LEARNED_ENTRIES = 50

# ≈ 2k tokens; tune as the lesson library grows. Truncation always drops
# the LOWEST-frequency entries first, so the cap costs us least-impactful
# rules first.
# P1 #18: tightened from 8000 → 3500 chars (~875 tokens). Was sending
# the full lesson library on EVERY orchestrator + reviewer call.
# 3500 chars holds ~25 high-frequency lessons; lower-frequency ones
# rotate through but don't bloat hot prompts.
_LEARNED_SECTION_CHAR_BUDGET = 3500


def _prompts_dir() -> Path:
    """The directory to load prompts from. Honors COS_PROMPT_OVERRIDE_DIR
    so a staging deploy can run an alternate prompt set without merging
    it (DOC3 — Phase 4 prerequisite for C2)."""
    override = os.environ.get("COS_PROMPT_OVERRIDE_DIR")
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_dir():
            return p
    return PROMPTS_DIR


def _load_prompt(name: str) -> str:
    return (_prompts_dir() / f"{name}.md").read_text()


def prompt_version(name: str) -> str:
    """Return a short content-hash version of the named prompt file
    (DOC3 — Phase 3.5 hardening). Used to record on each task row
    which prompt revision the run used. 12 hex chars is plenty for
    audit-trail uniqueness; collisions don't matter because it's an
    audit aid, not a uniqueness key.
    """
    try:
        text = _load_prompt(name)
    except Exception:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


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
