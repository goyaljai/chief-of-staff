"""Phase 2 B2+B3 acceptance test.

Proves:
  1. Hand-written prelude of skills/global.md is preserved across re-renders.
  2. New lessons UPSERT into Postgres skill_lessons (frequency=1, was_new=True).
  3. Duplicate-by-hash lessons bump frequency without adding rows.
  4. Re-rendered global.md lists lessons by frequency desc with [×N] tags.
  5. Bootstrap importer is idempotent: 2nd call does nothing.
  6. The first lessons in _load_skills() output match the highest-frequency rows.
"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401
import persistence as db
import agents as orchestrator  # noqa: F401 — keeps any `orchestrator.X` referenced below resolving
from agents import (
    GLOBAL_SKILL_PATH, LEARNED_HEADER, append_to_global,
    bootstrap_skill_lessons_from_md, _load_skills,
)

# --- backup current state, restore at end ---
GLOBAL_BACKUP = GLOBAL_SKILL_PATH.read_text() if GLOBAL_SKILL_PATH.exists() else ""


def _truncate_lessons():
    """V3.5 round-2 fix #8: refuse to TRUNCATE on a non-test DB.
    Without this guard, running `python eval/test_skill_lessons.py` with the
    project's DATABASE_URL pointed at production would wipe all curated
    skill_lessons rows. We allow the truncate only when:
      - DATABASE_URL host contains 'localhost' (CI service container), OR
      - env var COS_ALLOW_DESTRUCTIVE_TESTS=1 is set explicitly, OR
      - DATABASE_URL database name contains 'test'."""
    import os
    import psycopg2
    dsn = os.environ["DATABASE_URL"]
    permitted = (
        "localhost" in dsn
        or os.environ.get("COS_ALLOW_DESTRUCTIVE_TESTS") == "1"
        or "/test" in dsn
        or "_test" in dsn
    )
    if not permitted:
        raise RuntimeError(
            "Refusing to TRUNCATE skill_lessons on a non-test DATABASE_URL.\n"
            "  Set COS_ALLOW_DESTRUCTIVE_TESTS=1 to override (you'll lose curated lessons),\n"
            "  or point DATABASE_URL at a localhost/test database for this run."
        )
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("TRUNCATE TABLE skill_lessons")
    conn.commit()
    conn.close()


def _restore():
    GLOBAL_SKILL_PATH.write_text(GLOBAL_BACKUP)
    _truncate_lessons()


def main():
    print("=" * 60)
    print("B2 + B3 ACCEPTANCE TEST")
    print("=" * 60)

    try:
        _truncate_lessons()

        # Setup: write a known global.md with prelude + a small learned section
        global_seed = (
            "# Global skills\n\n"
            "## Verification proof\nAlways prove with output, not claims.\n\n"
            "## Scope discipline\nDo not add libraries the goal does not require.\n\n"
            f"{LEARNED_HEADER}\n\n"
            "- Existing rule one about checking files exist before editing.\n"
            "- Existing rule two about avoiding bare except clauses.\n"
        )
        GLOBAL_SKILL_PATH.write_text(global_seed)

        # 1. Bootstrap imports the existing 2 lessons
        n = bootstrap_skill_lessons_from_md()
        assert n == 2, f"bootstrap should import 2; got {n}"
        assert db.count_skill_lessons() == 2
        print("✓ Bootstrap imported 2 existing lessons")

        # 1b. Idempotent
        n2 = bootstrap_skill_lessons_from_md()
        assert n2 == 0
        assert db.count_skill_lessons() == 2
        print("✓ Bootstrap is idempotent (no double-import)")

        # 2. Promote a brand-new lesson via append_to_global
        added = append_to_global(
            ["Always run the verification command and show its output in the log."],
            origin_task_id="task_abc",
        )
        assert added == 1
        assert db.count_skill_lessons() == 3
        print("✓ New lesson added (count 2 → 3)")

        # 3. Promote a duplicate (case + whitespace different) — bumps freq
        added_dup = append_to_global(
            ["  always run THE verification command and SHOW its output in the log.  "],
            origin_task_id="task_xyz",
        )
        assert added_dup == 0, f"duplicate should add 0 new rows; got {added_dup}"
        assert db.count_skill_lessons() == 3
        # …and the freq incremented
        rows = db.list_skill_lessons(limit=5)
        top = rows[0]
        assert top["frequency"] == 2, f"top should be freq=2; got {top['frequency']}"
        print(f"✓ Duplicate bumped frequency: top freq={top['frequency']}")

        # 4. Bump again — freq should reach 3
        append_to_global(["always run the verification command and show its output in the log."])
        rows = db.list_skill_lessons(limit=5)
        assert rows[0]["frequency"] == 3
        print("✓ Frequency now 3 after another duplicate")

        # 5. global.md re-rendered:
        #    - prelude preserved verbatim
        #    - learned section ordered by freq desc
        #    - [×N] tag visible for freq>1
        text = GLOBAL_SKILL_PATH.read_text()
        assert "## Verification proof" in text and "## Scope discipline" in text
        prelude, _, learned = text.partition(LEARNED_HEADER)
        assert "Always prove with output" in prelude
        # The first bullet (highest freq) should be the verification one with [×3] tag
        first_bullet = next(l for l in learned.splitlines() if l.startswith("- "))
        assert "[×3]" in first_bullet, f"first bullet missing [×3] tag: {first_bullet!r}"
        assert "verification command" in first_bullet.lower()
        print(f"✓ global.md sorted by frequency; first bullet:")
        print(f"    {first_bullet}")

        # 5b. Structured input: dict with remediation + domains
        added_struct = append_to_global([
            {
                "pattern": "Always print psql command output verbatim before claiming a migration applied.",
                "remediation": "Run the SQL with -e -v ON_ERROR_STOP=1 and paste the entire stderr/stdout block in the action log.",
                "domains": ["data", "ops"],
            }
        ])
        assert added_struct == 1
        rows = db.list_skill_lessons(limit=10)
        struct = next(r for r in rows if "psql command" in r["pattern"])
        assert struct["remediation"] and "ON_ERROR_STOP=1" in struct["remediation"]
        assert sorted(struct["domains"]) == ["data", "ops"]
        text = GLOBAL_SKILL_PATH.read_text()
        assert "applies_to: data, ops" in text
        assert "**fix:**" in text and "ON_ERROR_STOP=1" in text
        print("✓ Structured lesson with remediation + domains rendered to global.md")

        # 6. _load_skills() output puts highest-freq lesson before others
        loaded = _load_skills()
        # Find positions of high-freq vs low-freq lesson text
        pos_high = loaded.find("verification command")
        pos_low = loaded.find("avoiding bare except")
        assert pos_high > 0 and pos_low > 0
        assert pos_high < pos_low, "B3: high-frequency lesson must appear before low-frequency"
        print("✓ _load_skills places high-frequency lesson before low-frequency (B3)")

        # 7. Round-3 fix #12: bootstrap MUST NOT re-import "  - **fix:** ..."
        # sub-bullets as standalone patterns. Truncate then re-bootstrap from
        # the freshly-rendered global.md and confirm zero garbage entries.
        _truncate_lessons()
        re_imported = bootstrap_skill_lessons_from_md()
        assert re_imported > 0, "expected re-bootstrap to import lessons"
        bad = [l for l in db.list_skill_lessons(limit=100)
               if l["pattern"].startswith("**fix:**") or "**fix:**" in l["pattern"][:30]]
        assert not bad, f"bootstrap re-imported remediation as patterns: {bad}"
        print(f"✓ Bootstrap re-import: {re_imported} clean lessons, zero **fix:** garbage")

        print("\n" + "=" * 60)
        print("B2 + B3 ACCEPTANCE: PASS ✓")
        print("=" * 60)
    finally:
        _restore()
        print("[restored original global.md and cleared skill_lessons]")


if __name__ == "__main__":
    main()
