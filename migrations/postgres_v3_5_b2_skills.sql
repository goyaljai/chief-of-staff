-- V3.5 B2: structured skill lessons with frequency tracking.
-- The hand-written prelude of skills/global.md (verification, scope, etc.) is
-- NOT stored here — only the auto-promoted lessons that need versioning,
-- deduplication, and frequency-based ordering.

CREATE TABLE IF NOT EXISTS skill_lessons (
    pattern_hash    TEXT PRIMARY KEY,
    pattern         TEXT NOT NULL,
    frequency       INT NOT NULL DEFAULT 1 CHECK (frequency > 0),
    domains         JSONB NOT NULL DEFAULT '[]'::jsonb,
    remediation     TEXT,
    origin_task_id  TEXT,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_archived     BOOLEAN NOT NULL DEFAULT false
);

-- Hot-path index: SELECT ... ORDER BY frequency DESC, last_seen DESC.
CREATE INDEX IF NOT EXISTS skill_lessons_rank_idx
    ON skill_lessons (frequency DESC, last_seen DESC)
    WHERE is_archived = false;
