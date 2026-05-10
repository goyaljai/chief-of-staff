"""Admin endpoints — extracted from main.py for modularity (v2.0 refactor R1).

These routes mutate global state (skill_lessons table, embeddings index) so
they're gated behind an optional ADMIN_TOKEN header. The token compare uses
hmac.compare_digest for constant-time matching (R7-2 fix).

Mounted in main.py via:
    from admin_routes import admin_router
    app.include_router(admin_router)

Why a separate file:
  - main.py was approaching 800 lines with mixed concerns (lifecycle,
    routes, helpers, shutdown). Routes that mutate global state benefit
    from being co-located with their auth + validation logic.
  - Each /admin endpoint shares the same _check_admin_token gate; keeping
    them together makes the security contract obvious.
  - Easier to add /admin/* endpoints later without bloating main.py.

Public exports:
  admin_router          — FastAPI APIRouter with /admin/* paths
  PromoteLessonRequest  — pydantic body schema for /admin/promote
  _check_admin_token    — gate function (also used by undo_route in tests)
  _MAX_PATTERN_LEN, _MAX_REMEDIATION_LEN — length caps surfaced for tests
"""
import os

import hmac as _hmac
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import persistence as db


# ─── auth gate ────────────────────────────────────────────────────────────

def _check_admin_token(request: Request) -> None:
    """V3.5 D7: require ADMIN_TOKEN header for /admin/* routes if env var set.

    Self-hosted single-user setups can leave it unset (returns immediately,
    open access). Production / shared hosts MUST set it.

    R7-2: hmac.compare_digest is constant-time — defends against the timing
    attack where `==` short-circuits on first mismatched byte and leaks
    byte position via response time.
    """
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        return
    got = (request.headers.get("x-admin-token", "") or "").strip()
    if not _hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Admin-Token")


# ─── request shapes ───────────────────────────────────────────────────────

_MAX_PATTERN_LEN = 1000
_MAX_REMEDIATION_LEN = 2000


class PromoteLessonRequest(BaseModel):
    """Body schema for POST /admin/promote.

    Lessons are stored in the skill_lessons table with frequency counters;
    submitting the same pattern twice bumps frequency rather than creating
    a duplicate row. The pattern_hash dedup is in db.upsert_skill_lesson.
    """
    pattern: str
    remediation: str | None = None
    domains: list[str] = []
    origin_task_id: str | None = None


# ─── router ───────────────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/admin", tags=["admin"])


@admin_router.post("/reindex")
def reindex(request: Request):
    """Re-embed every task in the DB.

    Heavy operation: every task's summary + skill_md text is fed through
    Databricks embeddings (v2.0 T2) and written back to tasks.summary_embedding
    and tasks.skill_embedding. Batches via rag.reindex_all_from_db (R5-4).

    Use cases:
      - After a schema migration that changed embedding dimensions
        (e.g. v1.0 384-dim → v2.0 1024-dim — see migrations/postgres_v2_0_t2_embed_1024.sql)
      - When swapping the embedding model (T2 → some future T2 successor)
      - After mass data import to ensure all rows are searchable

    Gated by ADMIN_TOKEN if env var is set (round-2 audit fix #6 — without
    this gate, anyone on the network could trigger a multi-minute, token-
    burning re-embedding).
    """
    _check_admin_token(request)
    import rag
    fts = db.reindex_fts() if hasattr(db, "reindex_fts") else 0
    rag_result = rag.reindex_all_from_db()
    return {"fts_rows": fts, "chroma": rag_result}


@admin_router.post("/promote")
def admin_promote_lesson(req: PromoteLessonRequest, request: Request):
    """V3.5 D7: seed/curate skill_lessons directly without running a task.

    UPSERTs the lesson — duplicates increment frequency rather than creating
    new rows. Re-renders skills/global.md from the table after every write
    so the in-prompt skill list stays sorted by frequency desc.

    Validation:
      - pattern: required, 1-1000 chars
      - remediation: optional, 0-2000 chars
      - domains: optional, max 8 entries (filters non-string + empty entries)

    Gated by ADMIN_TOKEN if env var set.
    """
    _check_admin_token(request)
    from orchestrator import append_to_global

    pattern = (req.pattern or "").strip()
    if not pattern:
        raise HTTPException(status_code=400, detail="pattern is required")
    if len(pattern) > _MAX_PATTERN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"pattern too long ({len(pattern)} chars; max {_MAX_PATTERN_LEN})",
        )
    remediation = (req.remediation or "").strip() or None
    if remediation and len(remediation) > _MAX_REMEDIATION_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"remediation too long ({len(remediation)} chars; max {_MAX_REMEDIATION_LEN})",
        )

    entry: dict = {"pattern": pattern}
    if remediation:
        entry["remediation"] = remediation
    if req.domains:
        entry["domains"] = [
            d for d in req.domains if isinstance(d, str) and d.strip()
        ][:8]

    added = append_to_global([entry], origin_task_id=req.origin_task_id or "admin")
    total = db.count_skill_lessons()
    return {"ok": True, "added_new": added, "total_lessons": total}
