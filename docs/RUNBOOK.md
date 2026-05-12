# Runbook

## Start / restart

```bash
# kill old, start fresh, wait for healthcheck
pkill -f "uvicorn main:app" 2>/dev/null
sleep 3
nohup uvicorn main:app --port 8000 > /tmp/cos-server.log 2>&1 &
sleep 6
curl -s http://localhost:8000/health
```

The Telegram bot starts automatically inside the FastAPI lifespan
when `TELEGRAM_BOT_TOKEN` is set.

## Apply migrations

```bash
psql "$DATABASE_URL" -f migrations/postgres_v3.sql
psql "$DATABASE_URL" -f migrations/postgres_v3_5_b2_skills.sql
psql "$DATABASE_URL" -f migrations/postgres_v2_0_t2_embed_1024.sql
psql "$DATABASE_URL" -f migrations/postgres_v2_0_t6_escalation.sql
psql "$DATABASE_URL" -f migrations/postgres_v2_0_t6b_persist_runtime_state.sql
```

All idempotent — safe to re-run.

## Resume an interrupted task

```bash
curl -s -X POST http://localhost:8000/task/{task_id}/resume
```

Picks up from the last LangGraph checkpoint. If status is `escalated`,
answer the escalation first via `POST /task/{id}/escalation`.

## Roll back a workspace mutation

```bash
curl -s -X POST http://localhost:8000/task/{task_id}/undo
```

Replays the `<workspace>/.cos_snapshots.jsonl` log. Binary files were
detected pre-mutation and skipped (R4-3 — no corruption).

## Reset the Promptfoo baseline

Only after a deliberate, intentional prompt change.

```bash
python eval/promptfoo/check_pass_rate.py \
  --baseline eval/promptfoo/baseline.json \
  --run eval/promptfoo/run-orchestrator.json \
  --run eval/promptfoo/run-reviewer.json \
  --init-baseline
```

Without `--init-baseline`, a missing baseline is a hard error
(audit r2 footgun fix).

## Cost guardrail

Every task is capped at `COS_MAX_TASK_USD` (default $25). When the
cumulative Claude spend exceeds the cap, the supervisor halts the
task with status `failed` and writes a `runaway_cost_stop` log entry.

Disable with `COS_MAX_TASK_USD=0`. Raise with e.g. `COS_MAX_TASK_USD=100`.

## Observability

- **LangSmith** — orchestrator + reviewer LLM calls + LangGraph DAG
  nodes + DSPy/litellm calls (when `LANGSMITH_TRACING=true`). See [TRACING.md](../TRACING.md).
- **Promptfoo dashboard** — CI runs auto-share to
  `app.promptfoo.app/eval`.
- **log_entries table** — env_audit, escalation parser, hook events.
  Query: `SELECT * FROM log_entries WHERE task_id=$1 AND kind=$2`.
  **The column is `payload`, not `entry`.**

## Dump cleanup (safe)

```sql
-- preserve curated lessons + skill embeddings; truncate task data
TRUNCATE TABLE log_entries, checkpoints, checkpoint_blobs,
               checkpoint_writes RESTART IDENTITY;
DELETE FROM tasks;
DELETE FROM langchain_pg_embedding
  WHERE collection_id IN (
    SELECT uuid FROM langchain_pg_collection WHERE name = 'task_summaries'
  );
-- Keep skill_lessons + skill_descriptions embeddings untouched.
```
