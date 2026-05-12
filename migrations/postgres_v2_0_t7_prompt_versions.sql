-- T7 (DOC3 + STREAM-TIME — Phase 3.5 hardening + Phase 4 prerequisite for C2):
-- record which prompt versions a task ran against, plus task wall-time + Claude
-- turn count. Both bundled because they're the same kind of change — adding
-- audit-trail columns to tasks.
--
-- DOC3 — prompt_versions jsonb:
--   Maps name → content hash, e.g. {"orchestrator":"a3b1...","reviewer":"9f4e..."}.
--   Computed at task start by hashing prompt file content. We have no audit
--   trail today for "which task used which prompt version" — C2's prompt
--   rewrite needs that rail before it can A/B-roll safely.
--
-- STREAM-TIME — wall_time_secs double, claude_turn_count integer:
--   Until now we tracked tokens + USD but not wall-time or how many tool calls
--   Claude made per task. Both are needed to MEASURE the C1 fast-path savings
--   and to spot pathological tasks (e.g. 200-turn loops).
--
-- All nullable + idempotent.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS prompt_versions   JSONB,
  ADD COLUMN IF NOT EXISTS wall_time_secs    DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS claude_turn_count INTEGER;
