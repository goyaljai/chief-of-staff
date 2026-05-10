"""RAG package — Retrieval-augmented generation layer for chief-of-staff.

PIPELINE
========

    query
      │
      ▼ Stage 1 — embed (Databricks gte-large-en, 1024-dim)
      │           See rag/embeddings.py
      ▼
    Stage 2 — pgvector cosine top-N (langchain-postgres PGVector)
              See rag/_stores.py
      │
      ▼ N candidates
    Stage 3 — Voyage rerank-2 cross-encoder → top_k
              See rag/rerank.py
      │
      ▼

KEYS USED
=========
  DATABRICKS_TOKEN, DATABRICKS_BASE_URL — Stage 1 (embeddings)
  VOYAGE_API_KEY                        — Stage 3 (rerank, optional)
  DATABASE_URL                          — Stage 2 (pgvector storage)

PUBLIC API
==========
This package re-exports the public-facing names so callers can keep
writing `import rag; rag.search_tasks(...)` and `rag.DatabricksEmbeddings()`
exactly as they did when rag was a single .py file.

  index_task(task_id, goal, summary, skill_md="")
  index_skill(task_id, name, description, skill_md)
  search_tasks(query, top_k=5)            → reranked task summaries
  find_matching_skill(query, top_k=1, ...)→ reranked + filtered
  reindex_all_from_db()                   → bulk re-embed
  warm_embedding_fn() / is_ready()        → optional warmup helper
  DatabricksEmbeddings                    → embedding client class

INTERNAL LAYOUT
===============
  rag/embeddings.py — DatabricksEmbeddings + _get_embed singleton
  rag/rerank.py     — Voyage client + _rerank
  rag/_stores.py    — PGVector wrappers (_get_task_store, _get_skill_store)
  rag/index.py      — atomic dual-write index_task / index_skill
  rag/retrieve.py   — search_tasks + find_matching_skill
  rag/reindex.py    — reindex_all_from_db (admin endpoint)
"""
from .embeddings import DatabricksEmbeddings, warm_embedding_fn, is_ready
from .index import index_task, index_skill
from .reindex import reindex_all_from_db
from .retrieve import find_matching_skill, search_tasks


__all__ = [
    "DatabricksEmbeddings",
    "find_matching_skill",
    "index_skill",
    "index_task",
    "is_ready",
    "reindex_all_from_db",
    "search_tasks",
    "warm_embedding_fn",
]
