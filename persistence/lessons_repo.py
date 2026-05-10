"""skill_lessons table — auto-promoted lessons that cross tasks.

Each lesson is a hash-deduped row. When a generic lesson surfaces from
multiple tasks, frequency increments instead of creating a duplicate row,
which is what drives the `[×N]` tag in skills/global.md (rendered sorted
by frequency descending — most-impactful surfaces first).

Public API:
  upsert_skill_lesson(pattern, ...) → (hash, frequency, was_new)
  list_skill_lessons(limit=200, include_archived=False)
  count_skill_lessons() → int
  archive_skill_lesson(pattern_hash) → bool
"""
import hashlib
import json

import psycopg2.extras

from .pool import _conn


def _normalize_pattern(pattern: str) -> str:
    """Normalize a lesson pattern so trivial whitespace/case differences
    don't create duplicate rows. Hash uses lowercased, single-spaced text."""
    return " ".join(pattern.lower().split())


def _pattern_hash(pattern: str) -> str:
    return hashlib.sha256(_normalize_pattern(pattern).encode("utf-8")).hexdigest()[:32]


def upsert_skill_lesson(
    pattern: str,
    origin_task_id: str | None = None,
    domains: list[str] | None = None,
    remediation: str | None = None,
) -> tuple[str, int, bool]:
    """UPSERT a lesson. On hash collision (duplicate), increments frequency,
    bumps last_seen, and merges new domains into the existing array.

    Returns (pattern_hash, new_frequency, was_new).
    """
    pattern = pattern.strip().lstrip("-").strip()
    if not pattern:
        raise ValueError("empty pattern")
    h = _pattern_hash(pattern)
    domains = list(domains or [])
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO skill_lessons (pattern_hash, pattern, frequency, domains, remediation, origin_task_id)
            VALUES (%s, %s, 1, %s::jsonb, %s, %s)
            ON CONFLICT (pattern_hash) DO UPDATE SET
                frequency = skill_lessons.frequency + 1,
                last_seen = now(),
                domains   = (
                    SELECT COALESCE(jsonb_agg(DISTINCT d), '[]'::jsonb)
                    FROM jsonb_array_elements_text(
                        skill_lessons.domains || EXCLUDED.domains
                    ) AS d
                ),
                remediation = COALESCE(skill_lessons.remediation, EXCLUDED.remediation)
            RETURNING frequency, (xmax = 0) AS was_new
            """,
            (h, pattern, json.dumps(domains), remediation, origin_task_id),
        )
        row = cur.fetchone()
        return h, row[0], bool(row[1])


def list_skill_lessons(limit: int = 200, include_archived: bool = False) -> list[dict]:
    """Return lessons sorted by (frequency DESC, last_seen DESC). Drives the
    learned-section render in skills/global.md and the prompt-injection order."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = "" if include_archived else "WHERE is_archived = false"
        cur.execute(
            f"""
            SELECT pattern_hash, pattern, frequency, domains, remediation,
                   origin_task_id, first_seen, last_seen, is_archived
            FROM skill_lessons
            {where}
            ORDER BY frequency DESC, last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def count_skill_lessons() -> int:
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM skill_lessons WHERE is_archived = false;")
        return cur.fetchone()[0]


def archive_skill_lesson(pattern_hash: str) -> bool:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE skill_lessons SET is_archived = true WHERE pattern_hash = %s",
            (pattern_hash,),
        )
        return cur.rowcount > 0
