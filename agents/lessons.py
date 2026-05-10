"""Skill-lesson promotion + rendering.

Lessons live in two places:
  • The skill_lessons table (Postgres) — source of truth, dedupe by
    pattern_hash, frequency increments on duplicates.
  • skills/global.md — auto-rendered from the table, sorted by frequency
    descending. Truncated by `_truncate_learned_section` when injected
    into prompts.

Why the auto-render: orchestrator + reviewer prompts are built by reading
skills/global.md. If we wrote lessons only to the DB, the LLM would never
see them. By re-rendering after every UPSERT we keep the on-disk file in
sync without making prompt assembly a SQL query.

Public API:
  append_to_global(lessons, origin_task_id, domains)  → int (added new)
  bootstrap_skill_lessons_from_md()                   → int (imported)
  save_task_skill(workspace, skill_md)                → no-op kept for
                                                        legacy callers

Internal:
  _rerender_global_md()  — full rewrite of skills/global.md
"""
import os
import re

import persistence as db

from .prompts import (
    GLOBAL_SKILL_PATH,
    LEARNED_HEADER,
    MAX_LEARNED_ENTRIES,
    SKILLS_DIR,
)


_LEARNED_INTRO = (
    "_(Sorted by frequency across runs — patterns hit more often appear first. "
    "`[×N]` shows how many tasks have promoted this lesson.)_"
)
_FREQ_TAG_RE = re.compile(r"^\[×\d+\]\s*")


def append_to_global(
    lessons: list,
    origin_task_id: str | None = None,
    domains: list[str] | None = None,
) -> int:
    """B2: UPSERT each lesson into the skill_lessons table (dedupe by hash,
    increment frequency on duplicates), then re-render skills/global.md so
    the Learned section is sorted by frequency desc.

    `lessons` may be a list of strings (legacy bare-string form) OR a list
    of dicts of shape `{"pattern": str, "remediation": str?, "domains":
    list[str]?}`. Strings inherit the caller-level `domains` kwarg. Dicts
    override per-entry.

    Returns the count of NEWLY-ADDED lessons (duplicates that bumped
    frequency do not count — that's the contract the orchestrator's
    `find_promotable_lessons` caller expects)."""
    if not lessons:
        return 0
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    added = 0
    for raw in lessons:
        if isinstance(raw, dict):
            pattern = (raw.get("pattern") or "").strip().lstrip("-").strip()
            entry_domains = list(raw.get("domains") or domains or [])
            remediation = (raw.get("remediation") or "").strip() or None
        else:
            pattern = str(raw).strip().lstrip("-").strip()
            entry_domains = list(domains or [])
            remediation = None
        pattern = _FREQ_TAG_RE.sub("", pattern)
        if not pattern:
            continue
        try:
            _, _, was_new = db.upsert_skill_lesson(
                pattern,
                origin_task_id=origin_task_id,
                domains=entry_domains,
                remediation=remediation,
            )
            if was_new:
                added += 1
        except Exception as e:
            print(f"[append_to_global] skill_lessons upsert failed: {e}")
    try:
        _rerender_global_md()
    except Exception as e:
        print(f"[append_to_global] re-render failed: {e}")
    return added


def _rerender_global_md() -> None:
    """Rebuild skills/global.md = preserved hand-written prelude +
    LEARNED_HEADER + DB-backed bullets sorted by frequency desc.

    The hand-written prelude (verification rules, scope discipline, etc.)
    is NEVER touched — only everything below LEARNED_HEADER is regenerated.

    Atomic write: `tmp + os.replace` so concurrent learning_promoted events
    don't race the read/partition/write sequence and leave skills/global.md
    half-written."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    existing = (
        GLOBAL_SKILL_PATH.read_text()
        if GLOBAL_SKILL_PATH.exists()
        else "# Global skills\n\n"
    )
    if LEARNED_HEADER in existing:
        prelude, _, _ = existing.partition(LEARNED_HEADER)
    else:
        prelude = existing
    prelude = prelude.rstrip() + "\n\n"

    lessons = db.list_skill_lessons(limit=MAX_LEARNED_ENTRIES)
    if not lessons:
        body = "_(no lessons yet)_\n"
    else:
        bullets = []
        for entry in lessons:
            f = entry["frequency"]
            tag = f"[×{f}] " if f > 1 else ""
            line = f"- {tag}{entry['pattern']}"
            entry_domains = entry.get("domains") or []
            if entry_domains:
                line += f"  _(applies_to: {', '.join(entry_domains)})_"
            remediation = (entry.get("remediation") or "").strip()
            if remediation:
                # Indented sub-bullet so the LLM reading the prompt clearly
                # associates the fix with its rule.
                line += f"\n  - **fix:** {remediation}"
            bullets.append(line)
        body = _LEARNED_INTRO + "\n\n" + "\n".join(bullets) + "\n"

    final = prelude + LEARNED_HEADER + "\n\n" + body
    tmp = GLOBAL_SKILL_PATH.with_suffix(".md.tmp")
    tmp.write_text(final)
    os.replace(tmp, GLOBAL_SKILL_PATH)


def bootstrap_skill_lessons_from_md() -> int:
    """One-time migration: import existing global.md learned bullets into
    the skill_lessons table if the table is empty. Each line becomes
    frequency=1. Safe to call on every startup — no-op when the DB is
    already populated.

    Round-3 fix #12: only top-level bullets are patterns. Skip indented
    sub-bullets like "  - **fix:** ..." (remediation) — bootstrapping those
    as patterns corrupts the lessons table after a wipe + re-import cycle.
    """
    try:
        if db.count_skill_lessons() > 0:
            return 0
    except Exception as e:
        print(f"[bootstrap] cannot read skill_lessons: {e}")
        return 0
    if not GLOBAL_SKILL_PATH.exists():
        return 0
    text = GLOBAL_SKILL_PATH.read_text()
    if LEARNED_HEADER not in text:
        return 0
    _, _, tail = text.partition(LEARNED_HEADER)
    lines = [l.strip() for l in tail.splitlines() if l.startswith("- ")]
    imported = 0
    for line in lines:
        pat = line.lstrip("-").strip()
        pat = _FREQ_TAG_RE.sub("", pat)
        if not pat:
            continue
        try:
            _, _, was_new = db.upsert_skill_lesson(pat)
            if was_new:
                imported += 1
        except Exception as e:
            print(f"[bootstrap] {e}")
    if imported:
        try:
            _rerender_global_md()
        except Exception as e:
            print(f"[bootstrap] re-render failed: {e}")
    print(f"[bootstrap] imported {imported} lessons from global.md into skill_lessons")
    return imported


def save_task_skill(workspace, skill_md: str):
    """V2.5+: SKILL.md is no longer materialized in the workspace — it
    confused Claude (which treated it as a deliverable). The skill_md
    string lives only in task state + DB, and is injected into orchestrator
    + reviewer prompts via _load_skills(). Kept as a no-op so callers
    don't break."""
    return None
