-- T6 (Phase 3 audit r3): persist escalation state.
--
-- Pre-T6 the supervisor's `state.escalation` lived only in memory. A
-- crash mid-escalation lost the structured ESCALATION/WHY/OPTIONS data
-- and the user's inline buttons in Telegram / web UI couldn't be wired
-- back up after a restart — the hydrating store had no record of
-- which task was in escalated status nor what the prompt was.
--
-- This migration adds two nullable columns:
--   - escalation         JSONB      the parsed escalation dict
--   - escalation_set_at  DOUBLE PRECISION   epoch seconds the supervisor flipped the task into 'escalated'
--
-- Both default NULL for tasks that never escalated. Idempotent so it
-- can run on every CI boot without erroring.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS escalation        JSONB,
  ADD COLUMN IF NOT EXISTS escalation_set_at DOUBLE PRECISION;
