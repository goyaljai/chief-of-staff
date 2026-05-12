# Migrations

Idempotent SQL files in `migrations/`. Apply in order; CI runs them
on every boot.

| File | Adds | Why |
|---|---|---|
| `postgres_v3.sql` | tasks, log_entries, embedding columns | Initial schema after V3 modularization |
| `postgres_v3_5_b2_skills.sql` | skill_lessons table + indexes | B2/B3 — auto-promoted lessons |
| `postgres_v2_0_t2_embed_1024.sql` | summary_embedding & skill_embedding 384 → 1024 dim | T2 — Databricks gte-large-en replaces local MiniLM |
| `postgres_v2_0_t6_escalation.sql` | escalation jsonb, escalation_set_at double | T6 — fix a server-crash data-loss bug where `state.escalation` was in-memory only |
| `postgres_v2_0_t6b_persist_runtime_state.sql` | user_notes, corrections, escalation_answer, claude_plan, skill_preview + indexes | T6b — same audit r3 lesson, five more TaskState fields needed columns |

## Adding a migration

1. Name: `postgres_v<N>_<short_name>.sql`. Use IF NOT EXISTS for
   ALTER TABLE / CREATE INDEX so re-running is safe.
2. Apply locally first to confirm it parses against your dev DB.
3. Add the file path to `.github/workflows/ci.yml` in BOTH the
   unit-tests job and the fast-eval job (two `psql -f` lines each).
4. If you added columns to `tasks`, run the schema-drift check
   (`python3 eval/test_schema_drift.py`) — it'll fail if you didn't
   also update `upsert_task` and the hydration block. See [PERSISTENCE.md](PERSISTENCE.md).
