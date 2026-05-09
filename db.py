"""SQLite persistence layer for chief-of-staff V2.5.

Tables:
  tasks         — one row per task (state, costs, skill metadata)
  log_entries   — append-only event log per task
  tasks_fts     — FTS5 index for keyword /ask retrieval
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import DB_PATH

_LOCK = threading.RLock()
_INIT_DONE = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT,
    clarifications TEXT,
    workspace TEXT,
    status TEXT,
    skill_md TEXT,
    skill_name TEXT,
    skill_description TEXT,
    brief TEXT,
    result TEXT,
    started_at REAL,
    finished_at REAL,
    cost_databricks_in INTEGER DEFAULT 0,
    cost_databricks_out INTEGER DEFAULT 0,
    cost_claude_usd REAL DEFAULT 0,
    keep_workspace INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    ts REAL,
    kind TEXT,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS idx_log_task_ts ON log_entries(task_id, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    id UNINDEXED,
    goal,
    brief,
    skill_md,
    summary,
    tokenize='porter unicode61'
);
"""


def init_db() -> None:
    global _INIT_DONE
    if _INIT_DONE:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    try:
        c.executescript(_SCHEMA)
        cur = c.execute("SELECT sql FROM sqlite_master WHERE name='tasks_fts'").fetchone()
        if cur and "content=''" in (cur[0] or ""):
            print("[db] migrating tasks_fts: dropping old contentless schema")
            c.executescript("DROP TABLE tasks_fts; " + _SCHEMA.split("CREATE VIRTUAL TABLE")[1].replace("IF NOT EXISTS", "").strip())
            c.execute("DELETE FROM tasks_fts")
            for r in c.execute("SELECT id, goal, brief, skill_md, json_extract(result, '$.summary') AS summary FROM tasks"):
                c.execute(
                    "INSERT INTO tasks_fts (id, goal, brief, skill_md, summary) VALUES (?,?,?,?,?)",
                    (r[0], r[1] or "", r[2] or "", r[3] or "", r[4] or ""),
                )
        c.commit()
    finally:
        c.close()
    _INIT_DONE = True


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    init_db()
    with _LOCK:
        c = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def upsert_task(state) -> None:
    summary = ""
    if state.result and isinstance(state.result, dict):
        summary = state.result.get("summary") or ""
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO tasks (
                id, goal, clarifications, workspace, status,
                skill_md, skill_name, skill_description, brief, result,
                started_at, finished_at,
                cost_databricks_in, cost_databricks_out, cost_claude_usd,
                keep_workspace
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                state.id, state.goal, json.dumps(state.clarifications), state.workspace, state.status,
                state.skill_md, getattr(state, "skill_name", "") or "",
                getattr(state, "skill_description", "") or "",
                state.brief,
                json.dumps(state.result) if state.result else None,
                state.started_at, state.finished_at,
                int(getattr(state, "cost_databricks_in", 0)),
                int(getattr(state, "cost_databricks_out", 0)),
                float(getattr(state, "cost_claude_usd", 0.0)),
                int(getattr(state, "keep_workspace", False)),
            ),
        )
        c.execute("DELETE FROM tasks_fts WHERE id = ?", (state.id,))
        c.execute(
            "INSERT INTO tasks_fts (id, goal, brief, skill_md, summary) VALUES (?,?,?,?,?)",
            (state.id, state.goal or "", state.brief or "", state.skill_md or "", summary),
        )


def append_log(task_id: str, kind: str, payload: dict, ts: float | None = None) -> None:
    if ts is None:
        ts = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO log_entries (task_id, ts, kind, payload) VALUES (?,?,?,?)",
            (task_id, ts, kind, json.dumps(payload)),
        )


def load_task(task_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        logs = c.execute(
            "SELECT ts, kind, payload FROM log_entries WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return {"row": dict(row), "logs": [dict(l) for l in logs]}


def list_tasks(limit: int = 200) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT id, goal, status, workspace, started_at, finished_at,
                   cost_databricks_in, cost_databricks_out, cost_claude_usd
            FROM tasks ORDER BY started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


_FTS_STOPWORDS = {
    "what", "when", "where", "which", "who", "why", "how",
    "is", "are", "was", "were", "the", "a", "an", "to", "of",
    "in", "on", "at", "for", "we", "us", "you", "i", "me", "did",
    "do", "does", "and", "or", "but", "this", "that", "these", "those",
    "had", "have", "has", "with", "from", "by", "about", "as",
}


def _sanitize_fts(query: str) -> str:
    """Convert a natural-language question into an FTS5 OR-tokenized query.
    Strips punctuation, drops stopwords, joins remaining tokens with OR."""
    import re as _re
    tokens = _re.findall(r"[a-zA-Z][a-zA-Z0-9]+", query.lower())
    keep = [t for t in tokens if t not in _FTS_STOPWORDS and len(t) > 1]
    if not keep:
        return ""
    return " OR ".join(keep)


def fts_search(query: str, limit: int = 10) -> list[dict]:
    if not query.strip():
        return []
    sanitized = _sanitize_fts(query) or query
    with _conn() as c:
        try:
            rows = c.execute(
                """
                SELECT id, goal, brief, summary,
                       snippet(tasks_fts, -1, '<<', '>>', '...', 12) AS snip,
                       rank
                FROM tasks_fts WHERE tasks_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (sanitized, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            like = f"%{query}%"
            rows = c.execute(
                """
                SELECT t.id, t.goal, t.brief, '' AS summary, '' AS snip, 0 AS rank
                FROM tasks t WHERE t.goal LIKE ? OR t.brief LIKE ? OR t.skill_md LIKE ?
                ORDER BY t.started_at DESC LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
            return [dict(r) for r in rows]


def cleanup_old_workspaces(max_age_secs: float) -> list[tuple[str, str]]:
    cutoff = time.time() - max_age_secs
    with _conn() as c:
        rows = c.execute(
            """
            SELECT id, workspace FROM tasks
            WHERE finished_at IS NOT NULL
              AND finished_at < ?
              AND COALESCE(keep_workspace, 0) = 0
              AND status IN ('done', 'failed', 'abandoned', 'cancelled')
            """,
            (cutoff,),
        ).fetchall()
        return [(r["id"], r["workspace"]) for r in rows if r["workspace"]]


def mark_keep(task_id: str, keep: bool = True) -> None:
    with _conn() as c:
        c.execute("UPDATE tasks SET keep_workspace = ? WHERE id = ?", (int(keep), task_id))


def reindex_fts() -> int:
    """Wipe and rebuild tasks_fts from tasks table. Call after schema change."""
    with _conn() as c:
        c.execute("DELETE FROM tasks_fts")
        rows = c.execute(
            "SELECT id, goal, brief, skill_md, json_extract(result, '$.summary') AS summary FROM tasks"
        ).fetchall()
        for r in rows:
            c.execute(
                "INSERT INTO tasks_fts (id, goal, brief, skill_md, summary) VALUES (?,?,?,?,?)",
                (r["id"], r["goal"] or "", r["brief"] or "", r["skill_md"] or "", r["summary"] or ""),
            )
        return len(rows)


def all_tasks_with_skill_md() -> list[dict]:
    """For ChromaDB indexing: return tasks with non-empty skill_md."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT id, goal, skill_name, skill_description, skill_md,
                   COALESCE(json_extract(result, '$.summary'), '') AS summary,
                   started_at
            FROM tasks WHERE skill_md IS NOT NULL AND skill_md != ''
            """
        ).fetchall()
        return [dict(r) for r in rows]
