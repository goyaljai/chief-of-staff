"""Persistence layer — Postgres pool, task/log/embedding/lessons CRUD, in-memory store.

This package replaces the old top-level `db.py` and `task_store.py`. Callers
should now import what they need directly:

    from persistence.pool import _conn, init_db, _close_pool
    from persistence.tasks_repo import upsert_task, append_log_batch, fts_search
    from persistence.embeddings_repo import update_embeddings, vector_search_tasks
    from persistence.lessons_repo import upsert_skill_lesson, list_skill_lessons
    from persistence.store import STORE, TaskState, TaskStore

For convenience, the most commonly-used names are also re-exported here so
single-line imports like `from persistence import STORE, init_db` work.

LAYOUT
======
  pool.py             — ThreadedConnectionPool + _conn context manager + init_db
  tasks_repo.py       — tasks/log_entries CRUD + pg_trgm FTS
  embeddings_repo.py  — pgvector column writes + cosine searches
  lessons_repo.py     — skill_lessons UPSERT/list/count/archive
  store.py            — in-memory TaskStore + log buffer flusher (E5)
"""
from .pool import _close_pool, _conn, _get_dsn, _get_pool, init_db
from .tasks_repo import (
    _sanitize_tokens,
    all_tasks_with_skill_md,
    append_log,
    append_log_batch,
    cleanup_old_workspaces,
    fts_search,
    list_tasks,
    load_task,
    mark_keep,
    reindex_fts,
    upsert_task,
)
from .embeddings_repo import (
    update_embeddings,
    vector_search_skills,
    vector_search_tasks,
)
from .lessons_repo import (
    _normalize_pattern,
    _pattern_hash,
    archive_skill_lesson,
    count_skill_lessons,
    list_skill_lessons,
    upsert_skill_lesson,
)
from .store import (
    STORE,
    TaskState,
    TaskStore,
    _build_dag_progress,
    _build_log_tail,
)


__all__ = [
    # pool
    "init_db", "_conn", "_get_pool", "_close_pool", "_get_dsn",
    # tasks_repo
    "upsert_task", "append_log", "append_log_batch", "load_task", "list_tasks",
    "fts_search", "cleanup_old_workspaces", "mark_keep", "all_tasks_with_skill_md",
    "reindex_fts",
    # embeddings_repo
    "update_embeddings", "vector_search_tasks", "vector_search_skills",
    # lessons_repo
    "upsert_skill_lesson", "list_skill_lessons", "count_skill_lessons",
    "archive_skill_lesson",
    # store
    "STORE", "TaskState", "TaskStore",
    "_build_log_tail", "_build_dag_progress",
]
