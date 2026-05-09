"""ChromaDB layer for chief-of-staff V2.5.

Two collections:
  task_summaries     — embeds (goal + summary + skill_md preview) for /ask retrieval
  skill_descriptions — embeds SKILL.md description fields for skill library reuse
"""
import threading
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from config import CHROMA_PATH

_LOCK = threading.RLock()
_CLIENT: chromadb.api.ClientAPI | None = None
_TASK_COLL = None
_SKILL_COLL = None
_EMBED_READY = False


def warm_embedding_fn(timeout_secs: int = 120) -> bool:
    """Eager-trigger Chroma's default embedding model download. Returns True on success."""
    global _EMBED_READY
    if _EMBED_READY:
        return True
    try:
        coll = _get_task_coll()
        coll.upsert(
            ids=["__warmup__"],
            documents=["warmup ping for embedding model download"],
            metadatas=[{"warmup": "true"}],
        )
        try:
            coll.delete(ids=["__warmup__"])
        except Exception:
            pass
        _EMBED_READY = True
        print(f"[rag] embedding model warmed up; collection has {coll.count()} entries")
        return True
    except Exception as e:
        print(f"[rag] embedding warmup failed: {e}")
        return False


def is_ready() -> bool:
    return _EMBED_READY


def _get_client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _LOCK:
        if _CLIENT is not None:
            return _CLIENT
        CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        _CLIENT = chromadb.PersistentClient(
            path=str(CHROMA_PATH),
            settings=Settings(anonymized_telemetry=False),
        )
        return _CLIENT


def _get_task_coll():
    global _TASK_COLL
    if _TASK_COLL is None:
        _TASK_COLL = _get_client().get_or_create_collection(
            name="task_summaries",
            metadata={"hnsw:space": "cosine"},
        )
    return _TASK_COLL


def _get_skill_coll():
    global _SKILL_COLL
    if _SKILL_COLL is None:
        _SKILL_COLL = _get_client().get_or_create_collection(
            name="skill_descriptions",
            metadata={"hnsw:space": "cosine"},
        )
    return _SKILL_COLL


def index_task(task_id: str, goal: str, summary: str, skill_md: str = "") -> None:
    if not goal:
        return
    try:
        coll = _get_task_coll()
        text = f"GOAL: {goal}\n\nSUMMARY: {summary}\n\nSKILL: {skill_md[:1500]}"
        coll.upsert(
            ids=[task_id],
            documents=[text],
            metadatas=[{"task_id": task_id, "goal": goal[:500]}],
        )
        print(f"[rag] indexed task {task_id} ({coll.count()} total)")
    except Exception as e:
        import traceback
        print(f"[rag] index_task failed: {e}")
        traceback.print_exc()


def index_skill(task_id: str, name: str, description: str, skill_md: str) -> None:
    if not description:
        return
    try:
        coll = _get_skill_coll()
        coll.upsert(
            ids=[task_id],
            documents=[description],
            metadatas=[{
                "task_id": task_id,
                "name": name or "",
                "description": description[:500],
                "skill_md_preview": skill_md[:500],
            }],
        )
        print(f"[rag] indexed skill {task_id} ({coll.count()} total)")
    except Exception as e:
        import traceback
        print(f"[rag] index_skill failed: {e}")
        traceback.print_exc()


def reindex_all_from_db() -> dict:
    """Rebuild Chroma collections from the SQLite tasks table."""
    import db as _db
    rows = _db.all_tasks_with_skill_md()
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


def search_tasks(query: str, top_k: int = 5) -> list[dict]:
    if not query.strip():
        return []
    try:
        coll = _get_task_coll()
        if coll.count() == 0:
            return []
        result = coll.query(query_texts=[query], n_results=min(top_k, coll.count()))
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        out = []
        for i, doc, dist, meta in zip(ids, docs, dists, metas):
            out.append({"task_id": i, "doc": doc, "distance": dist, "meta": meta})
        return out
    except Exception as e:
        print(f"[rag] search_tasks failed: {e}")
        return []


def find_matching_skill(description_or_task: str, top_k: int = 1, distance_max: float = 0.30) -> dict | None:
    """Return the top matching past skill if cosine distance is within threshold (closer = better).
    distance_max: 0.0 = identical, 1.0 = unrelated. Default 0.30 ≈ very similar.
    """
    if not description_or_task.strip():
        return None
    try:
        coll = _get_skill_coll()
        if coll.count() == 0:
            return None
        result = coll.query(query_texts=[description_or_task], n_results=min(top_k, coll.count()))
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        if not ids:
            return None
        if dists[0] > distance_max:
            return None
        return {
            "task_id": ids[0],
            "description": docs[0],
            "distance": dists[0],
            "meta": metas[0],
        }
    except Exception as e:
        print(f"[rag] find_matching_skill failed: {e}")
        return None
