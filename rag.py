"""V3.5: LangChain-backed RAG using Postgres + pgvector.

Replaces V3's raw chromadb client with langchain-postgres PGVector vectorstore.
LangSmith automatically traces every retrieval (LANGCHAIN_TRACING_V2=true).

Public API kept compatible with V3 callers:
  - index_task(task_id, goal, summary, skill_md)
  - index_skill(task_id, name, description, skill_md)
  - search_tasks(query, top_k) -> list[dict]
  - find_matching_skill(query, top_k, distance_max) -> dict | None
  - reindex_all_from_db()
  - warm_embedding_fn() / is_ready()  (kept for backwards compat)
"""
import os
import threading
from typing import Any

try:
    from langchain_huggingface import HuggingFaceEmbeddings
except ImportError:
    from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_postgres import PGVector

import db

_LOCK = threading.RLock()
_EMBED = None
_TASK_STORE: PGVector | None = None
_SKILL_STORE: PGVector | None = None
_READY = False


def _get_embed():
    """Use the same default embedding model Chroma was using
    (sentence-transformers/all-MiniLM-L6-v2 → 384 dim, matches our pgvector schema)."""
    global _EMBED
    if _EMBED is not None:
        return _EMBED
    with _LOCK:
        if _EMBED is None:
            _EMBED = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        return _EMBED


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return url


def _get_task_store() -> PGVector:
    global _TASK_STORE
    if _TASK_STORE is not None:
        return _TASK_STORE
    with _LOCK:
        if _TASK_STORE is None:
            _TASK_STORE = PGVector(
                collection_name="task_summaries",
                connection=_dsn(),
                embeddings=_get_embed(),
                use_jsonb=True,
            )
        return _TASK_STORE


def _get_skill_store() -> PGVector:
    global _SKILL_STORE
    if _SKILL_STORE is not None:
        return _SKILL_STORE
    with _LOCK:
        if _SKILL_STORE is None:
            _SKILL_STORE = PGVector(
                collection_name="skill_descriptions",
                connection=_dsn(),
                embeddings=_get_embed(),
                use_jsonb=True,
            )
        return _SKILL_STORE


def warm_embedding_fn(timeout_secs: int = 120) -> bool:
    """V2.5 compat: pre-load the HuggingFace embedding model."""
    global _READY
    if _READY:
        return True
    try:
        emb = _get_embed()
        emb.embed_query("warmup")
        _READY = True
        print("[rag] HuggingFace embedding warmed up (all-MiniLM-L6-v2, 384-dim)")
        return True
    except Exception as e:
        print(f"[rag] warmup failed: {e}")
        return False


def is_ready() -> bool:
    return _READY


def index_task(task_id: str, goal: str, summary: str, skill_md: str = "") -> None:
    """Store the task summary embedding both in PGVector AND on the tasks row (so we can
    do hybrid queries later)."""
    if not goal:
        return
    try:
        text = f"GOAL: {goal}\n\nSUMMARY: {summary}\n\nSKILL: {skill_md[:1500]}"
        store = _get_task_store()
        store.add_texts(
            texts=[text],
            metadatas=[{"task_id": task_id, "goal": goal[:500]}],
            ids=[task_id],
        )
        # Also store on tasks row for fast Postgres-only queries
        emb = _get_embed().embed_query(text)
        db.update_embeddings(task_id, summary_embedding=emb)
        print(f"[rag] indexed task {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_task failed: {e}")
        traceback.print_exc()


def index_skill(task_id: str, name: str, description: str, skill_md: str) -> None:
    if not description:
        return
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
            ids=[task_id],
        )
        emb = _get_embed().embed_query(description)
        db.update_embeddings(task_id, skill_embedding=emb)
        print(f"[rag] indexed skill {task_id}")
    except Exception as e:
        import traceback
        print(f"[rag] index_skill failed: {e}")
        traceback.print_exc()


def search_tasks(query: str, top_k: int = 5) -> list[dict]:
    if not query.strip():
        return []
    try:
        store = _get_task_store()
        results = store.similarity_search_with_score(query, k=top_k)
        out = []
        for doc, distance in results:
            out.append({
                "task_id": doc.metadata.get("task_id"),
                "doc": doc.page_content,
                "distance": float(distance),
                "meta": doc.metadata,
            })
        return out
    except Exception as e:
        print(f"[rag] search_tasks failed: {e}")
        return []


def find_matching_skill(description_or_task: str, top_k: int = 1, distance_max: float = 0.40) -> dict | None:
    if not description_or_task.strip():
        return None
    try:
        store = _get_skill_store()
        results = store.similarity_search_with_score(description_or_task, k=top_k)
        if not results:
            return None
        doc, distance = results[0]
        if float(distance) > distance_max:
            return None
        return {
            "task_id": doc.metadata.get("task_id"),
            "description": doc.page_content,
            "distance": float(distance),
            "meta": doc.metadata,
        }
    except Exception as e:
        print(f"[rag] find_matching_skill failed: {e}")
        return None


def reindex_all_from_db() -> dict:
    """Rebuild PGVector collections from the tasks table."""
    rows = db.all_tasks_with_skill_md()
    n_tasks = 0
    n_skills = 0
    for r in rows:
        try:
            index_task(r["id"], r["goal"] or "", r.get("summary") or "", r.get("skill_md") or "")
            n_tasks += 1
        except Exception:
            pass
        if r.get("skill_description"):
            try:
                index_skill(r["id"], r.get("skill_name") or "", r["skill_description"], r.get("skill_md") or "")
                n_skills += 1
            except Exception:
                pass
    return {"tasks_indexed": n_tasks, "skills_indexed": n_skills}
