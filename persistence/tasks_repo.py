"""Tasks table CRUD + log_entries + Postgres FTS keyword search.

Public API:
  upsert_task(state)                 — insert/update a task row
  append_log(task_id, kind, payload) — single log_entries insert
  append_log_batch(rows)             — batched log_entries insert (E5)
  load_task(task_id)                 — fetch row + logs
  list_tasks(limit=200)              — id/goal/status/cost summary
  fts_search(query, limit=10)        — pg_trgm-based keyword search
  cleanup_old_workspaces(max_age)    — finished tasks past TTL
  mark_keep(task_id, keep=True)      — pin a workspace from sweep
  all_tasks_with_skill_md()          — bulk read for /admin/reindex
  reindex_fts()                      — row count (FTS is in-table)
"""
import json
import re
import time

import psycopg2
import psycopg2.extras

from .pool import _conn


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
                state.id, state.goal, json.dumps(state.clarifications),
                state.workspace, state.status,
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
    fires 30+ events per task; previously each was a single round-trip plus
    a global RLock. With this batched path the task_store flusher coalesces
    50-200 rows per round-trip — the difference between serialized and
    pool-friendly throughput.

    Each row is (task_id, ts, kind, payload_dict). Payload is JSON-serialized
    here so callers can buffer plain dicts."""
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
        cur.execute(
            "SELECT ts, kind, payload FROM log_entries WHERE task_id = %s ORDER BY id",
            (task_id,),
        )
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
    or_clauses = " OR ".join(
        ["goal ILIKE %s OR brief ILIKE %s OR skill_md ILIKE %s OR result->>'summary' ILIKE %s"]
    )
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
        cur.execute(
            "UPDATE tasks SET keep_workspace = %s WHERE id = %s",
            (bool(keep), task_id),
        )


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


def reindex_fts() -> int:
    """Postgres FTS is in-table (no separate FTS index to rebuild). Returns
    row count for the /admin/reindex telemetry payload."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM tasks;")
        return cur.fetchone()[0]
