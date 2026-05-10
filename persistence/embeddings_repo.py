"""pgvector embedding column writes + cosine similarity searches.

The embedding columns (`tasks.summary_embedding`, `tasks.skill_embedding`)
are vector(1024) since v2.0 Phase 1 (T2 — see migrations/postgres_v2_0_t2_embed_1024.sql).
v1.0 used 384-dim local MiniLM; the upgrade to Databricks gte-large-en is
why we have a migration drop+recreate the ivfflat indexes.

These functions are the "denormalized hot path" — same data also lives in
the langchain_pg_embedding table managed by PGVector (see rag/_stores.py),
but the dual write through index_task / index_skill keeps both stores in
sync. Single-table queries here are faster for the common
'have we done this before?' lookup.
"""
import psycopg2
import psycopg2.extras

from .pool import _conn


def update_embeddings(
    task_id: str,
    summary_embedding: list[float] | None = None,
    skill_embedding: list[float] | None = None,
):
    """Write one or both embedding columns. Either argument can be None,
    in which case that column is left untouched (the calls are independent)."""
    with _conn() as c, c.cursor() as cur:
        if summary_embedding is not None:
            cur.execute(
                "UPDATE tasks SET summary_embedding = %s::vector WHERE id = %s",
                (summary_embedding, task_id),
            )
        if skill_embedding is not None:
            cur.execute(
                "UPDATE tasks SET skill_embedding = %s::vector WHERE id = %s",
                (skill_embedding, task_id),
            )


def vector_search_tasks(query_embedding: list[float], top_k: int = 5) -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, goal, COALESCE(result->>'summary','') AS summary,
                   1 - (summary_embedding <=> %s::vector) AS score,
                   (summary_embedding <=> %s::vector) AS distance
            FROM tasks
            WHERE summary_embedding IS NOT NULL
            ORDER BY summary_embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, query_embedding, query_embedding, top_k),
        )
        return [dict(r) for r in cur.fetchall()]


def vector_search_skills(query_embedding: list[float], top_k: int = 5) -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, skill_name, skill_description, skill_md,
                   1 - (skill_embedding <=> %s::vector) AS score,
                   (skill_embedding <=> %s::vector) AS distance
            FROM tasks
            WHERE skill_embedding IS NOT NULL
            ORDER BY skill_embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, query_embedding, query_embedding, top_k),
        )
        return [dict(r) for r in cur.fetchall()]
