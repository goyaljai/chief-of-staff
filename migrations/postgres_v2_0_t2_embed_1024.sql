-- v2.0 Phase 1 (T2): swap embedding columns from 384-dim (HuggingFace MiniLM)
-- to 1024-dim (Databricks gte-large-en, OR Voyage voyage-3 as fallback).
--
-- Why we went 1024-dim:
--   • databricks-gte-large-en is the only embedding model exposed on our
--     Databricks AI Gateway (verified by brute-forcing the model list).
--     It's a strict quality upgrade over local sentence-transformers MiniLM.
--   • voyage-3 also outputs 1024-dim — the same column type covers both,
--     so we can swap between them without another migration.
--   • Our pgvector ivfflat indexes accept any vector dimension as long as
--     all rows in the column match.
--
-- Migration strategy:
--   1. NULL out existing 384-dim values (can't auto-cast across dims).
--      The application gracefully treats NULL as "not yet indexed" and
--      will re-embed on the next /admin/reindex or task RAG query.
--   2. ALTER the column type to vector(1024).
--   3. Re-create the ivfflat indexes (pgvector doesn't auto-rebuild
--      indexes after a dimension change).
--
-- Idempotent: safe to re-run.

DO $$
BEGIN
    -- Drop existing ivfflat indexes (they're dimension-specific)
    EXECUTE 'DROP INDEX IF EXISTS tasks_summary_embedding_idx';
    EXECUTE 'DROP INDEX IF EXISTS tasks_skill_embedding_idx';

    -- Clear stale 384-dim values; app will re-embed lazily
    UPDATE tasks SET summary_embedding = NULL, skill_embedding = NULL
        WHERE summary_embedding IS NOT NULL OR skill_embedding IS NOT NULL;

    -- Bump column type to 1024 dims (idempotent — checks current dim)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tasks' AND column_name = 'summary_embedding'
          AND udt_name = 'vector'
    ) THEN
        ALTER TABLE tasks ALTER COLUMN summary_embedding TYPE vector(1024);
        ALTER TABLE tasks ALTER COLUMN skill_embedding   TYPE vector(1024);
    END IF;
END $$;

-- Re-create the ivfflat indexes with the new dimension
CREATE INDEX IF NOT EXISTS tasks_summary_embedding_idx
    ON tasks USING ivfflat (summary_embedding vector_cosine_ops) WITH (lists = 100);

CREATE INDEX IF NOT EXISTS tasks_skill_embedding_idx
    ON tasks USING ivfflat (skill_embedding vector_cosine_ops) WITH (lists = 100);
