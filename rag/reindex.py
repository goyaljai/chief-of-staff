"""Bulk re-embed every task in the DB.

Used after a schema migration (like the v1.0→v2.0 swap from 384-dim to
1024-dim) to repopulate embeddings against the new model. Also gated
behind the /admin/reindex endpoint for ad-hoc rebuilds.

R5-4 fix: previously this called index_task / index_skill in a loop, which
meant one Databricks embeddings round-trip per task — for 100 tasks that
was 100 round-trips (~30s+ on a free Databricks tier). Now we batch the
embedding calls: build all texts first, embed in batches of 96 (the
Databricks gateway limit), then write rows.

Side effect: PGVector's `add_texts` is also called batch-wise so we're
not paying the connection-per-row cost there either.
"""
import db

from .embeddings import _get_embed
from ._stores import _get_task_store, _get_skill_store


def reindex_all_from_db() -> dict:
    rows = db.all_tasks_with_skill_md()
    if not rows:
        return {"tasks_indexed": 0, "skills_indexed": 0}

    emb = _get_embed()

    # ── tasks ─────────────────────────────────────────────────────────────
    task_records = [r for r in rows if r.get("goal")]
    task_texts = [
        f"GOAL: {r['goal']}\n\nSUMMARY: {r.get('summary') or ''}\n\nSKILL: {(r.get('skill_md') or '')[:1500]}"
        for r in task_records
    ]
    task_ids_ns = [f"task::{r['id']}" for r in task_records]
    task_metas = [{"task_id": r["id"], "goal": r["goal"][:500]} for r in task_records]

    n_tasks = 0
    if task_records:
        try:
            # Single batched embed call — replaces N round-trips.
            task_embeddings = emb.embed_documents(task_texts)
            store = _get_task_store()
            store.add_texts(texts=task_texts, metadatas=task_metas, ids=task_ids_ns)
            for r, vec in zip(task_records, task_embeddings):
                try:
                    db.update_embeddings(r["id"], summary_embedding=vec)
                    n_tasks += 1
                except Exception as e:
                    print(f"[rag] reindex update_embeddings({r['id']}) failed: {e}")
            print(f"[rag] reindexed {n_tasks} tasks in 1 batch")
        except Exception as e:
            import traceback
            print(f"[rag] reindex tasks batch failed: {e}")
            traceback.print_exc()

    # ── skills ────────────────────────────────────────────────────────────
    skill_records = [r for r in rows if r.get("skill_description")]
    n_skills = 0
    if skill_records:
        try:
            skill_texts = [r["skill_description"] for r in skill_records]
            skill_ids_ns = [f"skill::{r['id']}" for r in skill_records]
            skill_metas = [
                {
                    "task_id": r["id"],
                    "name": r.get("skill_name") or "",
                    "description": r["skill_description"][:500],
                    "skill_md_preview": (r.get("skill_md") or "")[:500],
                }
                for r in skill_records
            ]
            skill_embeddings = emb.embed_documents(skill_texts)
            store = _get_skill_store()
            store.add_texts(texts=skill_texts, metadatas=skill_metas, ids=skill_ids_ns)
            for r, vec in zip(skill_records, skill_embeddings):
                try:
                    db.update_embeddings(r["id"], skill_embedding=vec)
                    n_skills += 1
                except Exception as e:
                    print(f"[rag] reindex update_embeddings({r['id']}) failed: {e}")
            print(f"[rag] reindexed {n_skills} skills in 1 batch")
        except Exception as e:
            import traceback
            print(f"[rag] reindex skills batch failed: {e}")
            traceback.print_exc()

    return {"tasks_indexed": n_tasks, "skills_indexed": n_skills}
