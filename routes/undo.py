"""Undo endpoint — extracted from main.py for modularity (v2.0 refactor R1).

D1-Lite: every Write/Edit/MultiEdit a Claude subprocess performs is captured
PRE-mutation by permission_hook.py to <workspace>/.cos_snapshots.jsonl.
This route walks those snapshots in chronological order (deduping per path
to keep the TRUE original) and reverts:

  - originally_existed=True   → write the original content back
  - originally_existed=False  → delete the file (Claude created it)
  - truncated=True            → skip (binary or oversized; we never captured
                                  the bytes, so we can't safely restore)

Why a separate file: D1 is a substantial feature with its own constants,
edge cases (binary detection, dedup-to-true-original, max-files cap), and
audit history. Keeping it co-located makes the contract clearer and the
file diff smaller when iterating.

Mounted in main.py via:
    from undo_route import undo_router
    app.include_router(undo_router)
"""
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from task_store import STORE


# Defensive cap on per-call work — large undos run on the request thread
# and would block uvicorn for too long otherwise. 500 entries covers any
# reasonable single-task footprint; an angry CI run that touched thousands
# of files surfaces the limit explicitly rather than silently slow-rolling.
_MAX_UNDO_FILES = 500


undo_router = APIRouter(tags=["undo"])


@undo_router.post("/task/{task_id}/undo")
def undo_task(task_id: str):
    """V3.5 D1 Lite: restore the workspace to its pre-task state.

    Returns:
        {ok, restored, deleted, skipped, errors[:5]}

    Where:
        restored — file overwrites that were reverted to their original content
        deleted  — files Claude created from scratch that were removed
        skipped  — entries we couldn't safely revert (binary, oversized, or
                   path now points to a directory). NOT silently corrupted.
        errors   — first 5 per-path error messages for diagnostics
    """
    state = STORE.get(task_id)
    if not state:
        raise HTTPException(status_code=404, detail="task not found")

    ws = Path(state.workspace) if state.workspace else None
    if not ws or not ws.exists():
        raise HTTPException(status_code=400, detail="workspace not found on disk")

    snap_file = ws / ".cos_snapshots.jsonl"
    if not snap_file.exists():
        return {
            "ok": True, "restored": 0, "deleted": 0, "skipped": 0,
            "reason": "no snapshots — nothing to undo",
        }

    # Parse all snapshot entries (best-effort — torn lines are skipped)
    entries: list[dict] = []
    for line in snap_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    if len(entries) > _MAX_UNDO_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"too many snapshot entries ({len(entries)}; max {_MAX_UNDO_FILES})",
        )

    # Dedupe by path — only the FIRST snapshot per path matters because that
    # captures the truly-original state before any of Claude's mutations.
    # Later snapshots reflect intermediate (already-mutated) states which we
    # don't want to restore.
    seen: set = set()
    chronological_originals: list[dict] = []
    for entry in entries:
        p = entry.get("path", "")
        if p and p not in seen:
            seen.add(p)
            chronological_originals.append(entry)

    restored = 0
    deleted = 0
    skipped = 0
    errors: list[str] = []
    for entry in chronological_originals:
        path = Path(entry.get("path", ""))
        try:
            # Truncated entries (binary or >256KB) — we never captured the
            # original bytes, so leaving the file alone beats restoring
            # corrupted content.
            if entry.get("truncated"):
                skipped += 1
                continue

            if entry.get("originally_existed"):
                # Restore overwrite: write original content back
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(entry.get("original", ""))
                restored += 1
            else:
                # Originally didn't exist → Claude created it → delete
                if path.exists():
                    if path.is_file():
                        path.unlink()
                        deleted += 1
                    elif path.is_dir():
                        # Edge case: snapshotted as file but is now a dir.
                        # We don't recursively delete dirs from undo — too
                        # destructive. Skip and let the user clean up manually.
                        skipped += 1
        except Exception as e:
            errors.append(f"{path}: {e}")
            skipped += 1

    STORE.append_log(task_id, {
        "kind": "undo",
        "restored": restored, "deleted": deleted, "skipped": skipped,
        "ts": time.time(),
    })
    return {
        "ok": True,
        "restored": restored,
        "deleted": deleted,
        "skipped": skipped,
        "errors": errors[:5],
    }
