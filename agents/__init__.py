"""Agents package — the LLM-powered roles in chief-of-staff.

PIPELINE
========

  user task
      │
      ▼  Phase 1: think_and_ask        (Orchestrator)
  3-5 clarifying questions
      │
      ▼  Phase 2: generate_skill_brief (Orchestrator)
  per-task SKILL.md
      │
      ▼  Phase 3: build_brief          (Orchestrator)
  executor brief (with optional ## Steps DAG)
      │
      ▼  Claude Code subprocess(es) execute the brief
  workspace artifacts + action log
      │
      ▼  per-action: review_action     (Reviewer)
      │  end-of-loop: final_review     (Reviewer)
  pass / fail + issues
      │
      ▼  Phase 4: find_promotable_lessons → append_to_global
  skill_lessons UPSERT, skills/global.md re-rendered

LAYOUT
======
  agents/llm.py          — Databricks chat gateway (_chat, _extract_json)
  agents/prompts.py      — prompt + skills file loaders
  agents/lessons.py      — skill_lessons UPSERT + global.md re-render +
                           bootstrap migration + save_task_skill no-op
  agents/orchestrator.py — class Orchestrator (manager role)
  agents/reviewer.py     — class Reviewer (independent QA)

PUBLIC API
==========
Re-exported here so callers can write `from agents import X` without
caring which submodule X lives in.
"""
from .lessons import (
    _FREQ_TAG_RE,
    _LEARNED_INTRO,
    _rerender_global_md,
    append_to_global,
    bootstrap_skill_lessons_from_md,
    save_task_skill,
)
from .llm import (
    _chat,
    _client,
    _extract_json,
    _maybe_init_langsmith,
    set_usage_callback,
)
from .orchestrator import Orchestrator
from .prompts import (
    GLOBAL_SKILL_PATH,
    LEARNED_HEADER,
    MAX_LEARNED_ENTRIES,
    SKILLS_DIR,
    _load_prompt,
    _load_skills,
    _truncate_learned_section,
)
from .reviewer import Reviewer


__all__ = [
    "Orchestrator", "Reviewer",
    "append_to_global", "bootstrap_skill_lessons_from_md",
    "save_task_skill",
    "set_usage_callback",
    "GLOBAL_SKILL_PATH", "LEARNED_HEADER", "MAX_LEARNED_ENTRIES", "SKILLS_DIR",
    # private but exposed for tests / advanced callers:
    "_chat", "_extract_json", "_client",
    "_load_prompt", "_load_skills", "_truncate_learned_section",
    "_rerender_global_md", "_LEARNED_INTRO", "_FREQ_TAG_RE",
    "_maybe_init_langsmith",
]
