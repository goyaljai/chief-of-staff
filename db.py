"""V3.5: Postgres-only persistence layer (no SQLite fallback).

Tables (created via migrations/postgres_v3.sql):
  tasks         — one row per task (with summary_embedding, skill_embedding columns)
  log_entries   — append-only event log per task

Search:
  pg_trgm + GIN indexes on goal/brief for keyword
  pgvector cosine similarity on summary_embedding/skill_embedding for semantic
"""
import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extras

_LOCK = threading.RLock()  # kept for legacy direct callers; new code uses _POOL

# V3.5 E5: ThreadedConnectionPool replaces per-call psycopg2.connect. The
# previous design serialized every DB call through a global RLock — under
# concurrent DAG step events this caused observed 6.27s wall time on parallel
# workloads (gaps L2 + L3). Pool size tuned for our task volume; bump if
# Supabase shows connection-limit pressure.
_POOL: "psycopg2.pool.ThreadedConnectionPool | None" = None


def _get_pool():
    global _POOL
    if _POOL is None:
        from psycopg2 import pool as _pool
        _POOL = _pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=8,
            dsn=_get_dsn(),
            connect_timeout=10,
        )
    return _POOL


def _close_pool():
    global _POOL
    if _POOL is not None:
        try:
            _POOL.closeall()
        except Exception:
            pass
        _POOL = None


def _get_dsn() -> str:
    """Read DATABASE_URL at call time, not module load time (config.py loads .env later)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set. V3.5 requires Postgres + pgvector.")
    return url


def init_db() -> None:
    """Verify Postgres connection + extensions. Schema must be applied via migrations/postgres_v3.sql."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_trgm');")
        exts = {r[0] for r in cur.fetchall()}
        missing = {"vector", "pg_trgm"} - exts
        if missing:
            raise RuntimeError(f"Postgres missing extensions: {missing}. Apply migrations/postgres_v3.sql.")
        cur.execute("SELECT to_regclass('public.tasks');")
        if cur.fetchone()[0] is None:
            raise RuntimeError("Tables missing. Apply migrations/postgres_v3.sql.")
        cur.execute("SELECT to_regclass('public.skill_lessons');")
        if cur.fetchone()[0] is None:
            raise RuntimeError(
                "skill_lessons table missing. Apply migrations/postgres_v3_5_b2_skills.sql."
            )


@contextmanager
def _conn() -> Iterator[psycopg2.extensions.connection]:
    """V3.5 E5: pool-backed connection. Pool reuse removes the global-lock
    serialization that was the root cause of the 6.27s parallel-DAG hot-spot.
    Bounded retry on transient errors stays — Supabase free tier still drops
    idle conns occasionally."""
    delays = [0.0, 0.5, 1.5]
    pool = _get_pool()
    last_exc: Exception | None = None
    c: psycopg2.extensions.connection | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            c = pool.getconn()
            # Sanity check — if the pool handed us a closed/broken conn
            # (Supabase idle-disconnect), discard and retry.
            if c.closed != 0:
                pool.putconn(c, close=True)
                c = None
                raise psycopg2.OperationalError("pool returned closed conn")
            last_exc = None
            break
        except (psycopg2.OperationalError, Exception) as e:
            # PoolError("connection pool exhausted") is also transient under load
            last_exc = e
            print(f"[db] pool getconn transient error (will retry): {e}")
            c = None
    if c is None:
        assert last_exc is not None
        raise last_exc
    try:
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            pool.putconn(c)
        except Exception as e:
            print(f"[db] pool putconn failed: {e}")


def upsert_task(state) -> None:
    summary = ""
    if state.result and isinstance(state.result, dict):
        summary = state.result.get("summary") or ""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks (id, goal, clarifications, workspace, status,
                               skill_md, skill_name, skill_description, brief, result,
                               started_at, finished_at,
                               cost_databricks_in, cost_databricks_out, cost_claude_usd,
                               keep_workspace)
            VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
              goal=EXCLUDED.goal, clarifications=EXCLUDED.clarifications,
              workspace=EXCLUDED.workspace, status=EXCLUDED.status,
              skill_md=EXCLUDED.skill_md, skill_name=EXCLUDED.skill_name,
              skill_description=EXCLUDED.skill_description, brief=EXCLUDED.brief,
              result=EXCLUDED.result, started_at=EXCLUDED.started_at,
              finished_at=EXCLUDED.finished_at,
              cost_databricks_in=EXCLUDED.cost_databricks_in,
              cost_databricks_out=EXCLUDED.cost_databricks_out,
              cost_claude_usd=EXCLUDED.cost_claude_usd,
              keep_workspace=EXCLUDED.keep_workspace
            """,
            (
                state.id, state.goal, json.dumps(state.clarifications), state.workspace, state.status,
                state.skill_md, getattr(state, "skill_name", "") or "",
                getattr(state, "skill_description", "") or "",
                state.brief,
                json.dumps(state.result) if state.result else None,
                state.started_at, state.finished_at,
                int(getattr(state, "cost_databricks_in", 0) or 0),
                int(getattr(state, "cost_databricks_out", 0) or 0),
                float(getattr(state, "cost_claude_usd", 0.0) or 0.0),
                bool(getattr(state, "keep_workspace", False)),
            ),
        )


def append_log(task_id: str, kind: str, payload: dict, ts: float | None = None) -> None:
    if ts is None:
        ts = time.time()
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO log_entries (task_id, ts, kind, payload) VALUES (%s,%s,%s,%s::jsonb)",
            (task_id, ts, kind, json.dumps(payload)),
        )


def append_log_batch(rows: list[tuple]) -> int:
    """V3.5 E5: bulk INSERT for log_entries. The hot path during a DAG run
    fires 30+ events per task; previously each was a single round-trip to
    Supabase plus a global RLock. With this batched path the task_store
    flusher coalesces 50-200 rows per round-trip — the difference between
    serialized (6.27s parallel-DAG observed) and pool-friendly throughput.

    Each row is (task_id, ts, kind, payload_dict). payload is JSON-serialized
    here so the caller can buffer plain dicts."""
    if not rows:
        return 0
    serialized = [
        (tid, ts if ts is not None else time.time(), kind, json.dumps(payload))
        for (tid, ts, kind, payload) in rows
    ]
    with _conn() as c, c.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO log_entries (task_id, ts, kind, payload) VALUES %s",
            serialized,
            template="(%s, %s, %s, %s::jsonb)",
            page_size=200,
        )
    return len(serialized)


def load_task(task_id: str) -> dict | None:
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT ts, kind, payload FROM log_entries WHERE task_id = %s ORDER BY id", (task_id,))
        logs = [dict(r) for r in cur.fetchall()]
        return {"row": dict(row), "logs": logs}


def list_tasks(limit: int = 200) -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, goal, status, workspace, started_at, finished_at,
                   cost_databricks_in, cost_databricks_out, cost_claude_usd
            FROM tasks ORDER BY started_at DESC LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


_FTS_STOPWORDS = {
    "what", "when", "where", "which", "who", "why", "how",
    "is", "are", "was", "were", "the", "a", "an", "to", "of",
    "in", "on", "at", "for", "we", "us", "you", "i", "me", "did",
    "do", "does", "and", "or", "but", "this", "that", "these", "those",
    "had", "have", "has", "with", "from", "by", "about", "as",
}


def _sanitize_tokens(query: str) -> list[str]:
    import re
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]+", query.lower())
    return [t for t in tokens if t not in _FTS_STOPWORDS and len(t) > 1]


def fts_search(query: str, limit: int = 10) -> list[dict]:
    """Postgres trigram-based keyword search over goal/brief/skill_md/summary."""
    if not query.strip():
        return []
    tokens = _sanitize_tokens(query)
    if not tokens:
        return []
    pattern = "%" + "%".join(tokens) + "%"
    or_clauses = " OR ".join([f"goal ILIKE %s OR brief ILIKE %s OR skill_md ILIKE %s OR result->>'summary' ILIKE %s"] * 1)
    params = [pattern, pattern, pattern, pattern]
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, goal, brief, COALESCE(result->>'summary','') AS summary,
                   substring(goal from 1 for 200) AS snip,
                   GREATEST(
                     similarity(goal, %s),
                     similarity(COALESCE(brief,''), %s)
                   ) AS rank
            FROM tasks
            WHERE ({or_clauses})
            ORDER BY rank DESC
            LIMIT %s
            """,
            (query, query, *params, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def cleanup_old_workspaces(max_age_secs: float) -> list[tuple[str, str]]:
    cutoff = time.time() - max_age_secs
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT id, workspace FROM tasks
            WHERE finished_at IS NOT NULL
              AND finished_at < %s
              AND COALESCE(keep_workspace, false) = false
              AND status IN ('done', 'failed', 'abandoned', 'cancelled')
            """,
            (cutoff,),
        )
        return [(r[0], r[1]) for r in cur.fetchall() if r[1]]


def mark_keep(task_id: str, keep: bool = True) -> None:
    with _conn() as c, c.cursor() as cur:
        cur.execute("UPDATE tasks SET keep_workspace = %s WHERE id = %s", (bool(keep), task_id))


def all_tasks_with_skill_md() -> list[dict]:
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT id, goal, skill_name, skill_description, skill_md,
                   COALESCE(result->>'summary','') AS summary,
                   started_at
            FROM tasks WHERE skill_md IS NOT NULL AND skill_md <> ''
            """
        )
        return [dict(r) for r in cur.fetchall()]


def update_embeddings(task_id: str, summary_embedding: list[float] | None = None, skill_embedding: list[float] | None = None):
    """V3.5: store pgvector embeddings on task row (computed by rag.py via LangChain)."""
    with _conn() as c, c.cursor() as cur:
        if summary_embedding is not None:
            cur.execute("UPDATE tasks SET summary_embedding = %s::vector WHERE id = %s",
                        (summary_embedding, task_id))
        if skill_embedding is not None:
            cur.execute("UPDATE tasks SET skill_embedding = %s::vector WHERE id = %s",
                        (skill_embedding, task_id))


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


def reindex_fts() -> int:
    """Postgres FTS is in-table (no separate FTS index to rebuild). Returns row count."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tasks;")
        return cur.fetchone()[0]


# ---------- V3.5 B2: structured skill lessons ----------

import hashlib as _hashlib


def _normalize_pattern(pattern: str) -> str:
    """Normalize a lesson pattern so trivial whitespace/case differences don't
    create duplicate rows. Hash uses lowercased, single-spaced text."""
    return " ".join(pattern.lower().split())


def _pattern_hash(pattern: str) -> str:
    return _hashlib.sha256(_normalize_pattern(pattern).encode("utf-8")).hexdigest()[:32]


def upsert_skill_lesson(
    pattern: str,
    origin_task_id: str | None = None,
    domains: list[str] | None = None,
    remediation: str | None = None,
) -> tuple[str, int, bool]:
    """UPSERT a lesson. On hash collision (duplicate), increments frequency,
    bumps last_seen, and merges new domains into the existing array.

    Returns (pattern_hash, new_frequency, was_new).
    """
    pattern = pattern.strip().lstrip("-").strip()
    if not pattern:
        raise ValueError("empty pattern")
    h = _pattern_hash(pattern)
    domains = list(domains or [])
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO skill_lessons (pattern_hash, pattern, frequency, domains, remediation, origin_task_id)
            VALUES (%s, %s, 1, %s::jsonb, %s, %s)
            ON CONFLICT (pattern_hash) DO UPDATE SET
                frequency = skill_lessons.frequency + 1,
                last_seen = now(),
                domains   = (
                    SELECT COALESCE(jsonb_agg(DISTINCT d), '[]'::jsonb)
                    FROM jsonb_array_elements_text(
                        skill_lessons.domains || EXCLUDED.domains
                    ) AS d
                ),
                remediation = COALESCE(skill_lessons.remediation, EXCLUDED.remediation)
            RETURNING frequency, (xmax = 0) AS was_new
            """,
            (h, pattern, json.dumps(domains), remediation, origin_task_id),
        )
        row = cur.fetchone()
        return h, row[0], bool(row[1])


def list_skill_lessons(limit: int = 200, include_archived: bool = False) -> list[dict]:
    """Return lessons sorted by (frequency DESC, last_seen DESC). Drives the
    learned-section render in skills/global.md and prompt injection order."""
    with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        where = "" if include_archived else "WHERE is_archived = false"
        cur.execute(
            f"""
            SELECT pattern_hash, pattern, frequency, domains, remediation,
                   origin_task_id, first_seen, last_seen, is_archived
            FROM skill_lessons
            {where}
            ORDER BY frequency DESC, last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def count_skill_lessons() -> int:
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM skill_lessons WHERE is_archived = false;")
        return cur.fetchone()[0]


def archive_skill_lesson(pattern_hash: str) -> bool:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE skill_lessons SET is_archived = true WHERE pattern_hash = %s",
            (pattern_hash,),
        )
        return cur.rowcount > 0
