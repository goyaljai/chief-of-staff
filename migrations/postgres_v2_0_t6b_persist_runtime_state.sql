-- T6b (Phase 3 audit r3): persist the rest of the runtime TaskState
-- fields that audit r3 surfaced as gaps.
--
-- Pre-T6b, several user-visible / correctness-critical fields lived
-- only in process memory and were silently lost on a server crash:
--
--   user_notes         — side-notes the user dropped via POST /task/{id}/note
--   corrections        — accumulator of past correction prompts (loop context)
--   escalation_answer  — user's answer to a pending escalation (race window
--                        between /escalation and the supervisor resume that
--                        consumes it)
--   claude_plan        — Claude's pending plan items, surfaced in the dashboard
--   skill_preview      — phase-1 meta-thinking preview
--
-- All nullable so existing rows keep working. Idempotent so it can re-run
-- on every CI boot.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS user_notes        JSONB,
  ADD COLUMN IF NOT EXISTS corrections       JSONB,
  ADD COLUMN IF NOT EXISTS escalation_answer TEXT,
  ADD COLUMN IF NOT EXISTS claude_plan       JSONB,
  ADD COLUMN IF NOT EXISTS skill_preview     TEXT;

-- Useful index for the /tasks list — the dashboard sorts by recency a lot.
-- IF NOT EXISTS guard means re-running is safe.
CREATE INDEX IF NOT EXISTS idx_tasks_started_at_desc ON tasks (started_at DESC);

-- Useful for log_entries stream queries that filter by kind.
CREATE INDEX IF NOT EXISTS idx_log_entries_task_kind ON log_entries (task_id, kind);
