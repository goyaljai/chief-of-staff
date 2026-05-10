"""Operations helpers — extracted from main.py during the v2.0 refactor.

Three loosely-related helpers covering ops concerns:
  - Supabase REST ping (visibility in the dashboard's Total Requests widget)
  - Workspace TTL sweep (delete stale task workspaces older than TTL days)
  - The hourly background loop that calls the sweeper

Public exports:
  supabase_rest_ping()                 — fire-and-forget startup ping
  run_workspace_sweep_once() -> int    — single-pass cleanup
  workspace_sweeper_loop()             — async coroutine, runs forever
"""
import asyncio
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import db
from config import WORKSPACE_ROOT, WORKSPACE_TTL_DAYS


def supabase_rest_ping() -> None:
    """One PostgREST GET at startup so the Supabase dashboard sees traffic.

    Direct psycopg2 connections over port 5432 don't register on the
    dashboard's "Total Requests" / "Database Requests" widgets — those
    track PostgREST API hits only. This single call lights up the metrics
    so the user can confirm 'yes, the project is alive' visually.

    Best-effort — if it fails, we log and move on. Everything else still
    works against the direct DB connection."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not url or not key:
        return
    req = urllib.request.Request(
        f"{url.rstrip('/')}/rest/v1/tasks?select=id&limit=1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "chief-of-staff/v2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            print(f"[supabase] REST ping ok ({r.status}) — dashboard will now show 1+ requests")
    except urllib.error.HTTPError as e:
        # Even a 401/403 is a hit on the REST API and registers in metrics.
        print(f"[supabase] REST ping returned {e.code} (still counts as a request)")


def run_workspace_sweep_once() -> int:
    """Single-pass workspace cleanup.

    Reusable from the startup hook (V3.5 C4) AND the hourly background loop
    so the policy lives in one place.

    Two cleanup buckets:
      1. DB-tracked task workspaces older than WORKSPACE_TTL_DAYS — pulled
         via db.cleanup_old_workspaces (which atomically returns rows AND
         removes them from the DB so we don't re-attempt).
      2. _dry_run/* directories older than 1 day. These are NOT in the DB
         (D6 dry-run mode creates them transiently), so we mtime-prune.
         Round-2 audit fix #5 — they used to accumulate forever.

    Returns the count of workspaces actually removed.
    """
    if WORKSPACE_TTL_DAYS <= 0:
        return 0
    swept = 0

    # 1. DB-tracked task workspaces
    max_age = WORKSPACE_TTL_DAYS * 86400
    old = db.cleanup_old_workspaces(max_age)
    for tid, ws in old:
        if ws and Path(ws).exists():
            try:
                shutil.rmtree(ws, ignore_errors=True)
                print(f"[sweeper] removed {ws} (task {tid})")
                swept += 1
            except Exception as e:
                print(f"[sweeper] failed to remove {ws}: {e}")

    # 2. _dry_run scratch directories
    dry_root = WORKSPACE_ROOT / "_dry_run"
    if dry_root.exists():
        cutoff = time.time() - 86400  # 1 day TTL
        for p in dry_root.iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    swept += 1
                    print(f"[sweeper] removed stale dry_run dir {p}")
            except Exception as e:
                print(f"[sweeper] failed to remove {p}: {e}")

    if swept:
        print(f"[sweeper] swept {swept} stale workspaces (TTL={WORKSPACE_TTL_DAYS}d)")
    return swept


async def workspace_sweeper_loop() -> None:
    """Hourly background sweeper. Started as an asyncio task from main's
    startup hook. Runs until cancelled (graceful shutdown handles that)."""
    if WORKSPACE_TTL_DAYS <= 0:
        return
    while True:
        try:
            await asyncio.sleep(3600)
            run_workspace_sweep_once()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[sweeper] loop error: {e}")
