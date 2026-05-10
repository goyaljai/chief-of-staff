"""Indexing — write task and skill rows into BOTH the denormalized
embedding columns and the LangChain PGVector collection (atomic dual-write).

Why two writes:
  • `tasks.summary_embedding` / `tasks.skill_embedding` are denormalized
    columns indexed with ivfflat. They drive the hot-path
    `db.vector_search_tasks` query — fast, single-table.
  • `langchain_pg_embedding` (managed by PGVector) carries richer metadata
    (goal, name, skill_md preview) and powers the broader retrieval calls
    in `retrieve.py`.

R6-4 fix (silent retrieval skew):
    Previously we wrote (1) and (2) sequentially with no atomicity. If (2)
    succeeded but (1) failed, search_tasks would find the doc but
    vector_search_tasks would miss it — different query paths returned
    different result sets. Now: embed once, write (1), write (2). On (2)'s
    failure we roll back (1) by NULLing the column, leaving the row in a
    clean "not indexed" state rather than a half-indexed one.

NAMESPACING: row id prefixed with `task::` / `skill::` because
langchain_postgres uses a single langchain_pg_embedding table for ALL
collections; sharing raw task_id between index_task and index_skill caused
cross-collection overwrites (caught in tests).
"""
import os

import psycopg2

import db

from .embeddings import _get_embed
from ._stores import _get_task_store, _get_skill_store


def index_task(task_id: str, goal: str, summary: str, skill_md: str = "") -> None:
    """Index a completed task in the task_summaries store. Writes both the
    denormalized column AND the PGVector collection; rolls back the column
    on PGVector-write failure (R6-4)."""
    if not goal:
        return
    try:
        text = f"GOAL: {goal}\n\nSUMMARY: {summary}\n\nSKILL: {skill_md[:1500]}"
        emb = _get_embed().embed_query(text)

        # Write 1: denormalized column (cheap single UPDATE)
        db.update_embeddings(task_id, summary_embedding=emb)

        # Write 2: langchain_postgres collection
        try:
            store = _get_task_store()
            store.add_texts(
                texts=[text],
                metadatas=[{"task_id": task_id, "goal": goal[:500]}],
                ids=[f"task::{task_id}"],
            )
        except Exception:
            # Roll back write 1 so we don't leave a half-indexed task.
            # db.update_embeddings(...None) skips None, so we issue raw SQL.
            try:
                with psycopg2.connect(os.environ["DATABASE_URL"]) as _c:
                    with _c.cursor() as _cur:
                        _cur.execute(
                            "UPDATE tasks SET summary_embedding = NULL WHERE id = %s",
                            (task_id,),
                        )
            except Exception:
                pass
            raise

        print(f"[rag] indexed task {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_task failed: {e}")
        traceback.print_exc()


def index_skill(task_id: str, name: str, description: str, skill_md: str) -> None:
    """Index a skill description for future find_matching_skill lookups.
    Same atomic dual-write pattern as index_task — embed once, write
    denormalized column first, write PGVector collection second, roll back
    column on collection-write failure."""
    if not description:
        return
    try:
        emb = _get_embed().embed_query(description)
        db.update_embeddings(task_id, skill_embedding=emb)
        try:
            store = _get_skill_store()
            store.add_texts(
                texts=[description],
                metadatas=[{
                    "task_id": task_id,
                    "name": name or "",
                    "description": description[:500],
                    "skill_md_preview": skill_md[:500],
                }],
                ids=[f"skill::{task_id}"],
            )
        except Exception:
            try:
                with psycopg2.connect(os.environ["DATABASE_URL"]) as _c:
                    with _c.cursor() as _cur:
                        _cur.execute(
                            "UPDATE tasks SET skill_embedding = NULL WHERE id = %s",
                            (task_id,),
                        )
            except Exception:
                pass
            raise
        print(f"[rag] indexed skill {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_skill failed: {e}")
        traceback.print_exc()
