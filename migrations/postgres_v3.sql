-- V3 #5: Postgres + pgvector migration
-- Run this on a fresh Postgres 15+ instance with pgvector extension.
-- Migration script (sqlite → postgres) is db_migrate.py.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    clarifications  JSONB NOT NULL DEFAULT '{}'::jsonb,
    workspace       TEXT,
    status          TEXT NOT NULL,
    skill_md        TEXT,
    skill_name      TEXT,
    skill_description TEXT,
    brief           TEXT,
    result          JSONB,
    started_at      DOUBLE PRECISION,
    finished_at     DOUBLE PRECISION,
    cost_databricks_in  INTEGER DEFAULT 0,
    cost_databricks_out INTEGER DEFAULT 0,
    cost_claude_usd     DOUBLE PRECISION DEFAULT 0,
    keep_workspace      BOOLEAN DEFAULT false,
    summary_embedding   vector(384),
    skill_embedding     vector(384)
);

CREATE INDEX IF NOT EXISTS tasks_started_idx ON tasks(started_at DESC);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_goal_trgm_idx ON tasks USING gin (goal gin_trgm_ops);
CREATE INDEX IF NOT EXISTS tasks_brief_trgm_idx ON tasks USING gin (brief gin_trgm_ops);
CREATE INDEX IF NOT EXISTS tasks_summary_emb_idx ON tasks USING ivfflat (summary_embedding vector_cosine_ops) WITH (lists=100);
CREATE INDEX IF NOT EXISTS tasks_skill_emb_idx ON tasks USING ivfflat (skill_embedding vector_cosine_ops) WITH (lists=100);

CREATE TABLE IF NOT EXISTS log_entries (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    ts          DOUBLE PRECISION NOT NULL,
    kind        TEXT NOT NULL,
    payload     JSONB
);
CREATE INDEX IF NOT EXISTS log_entries_task_idx ON log_entries(task_id, ts);
