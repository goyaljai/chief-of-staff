"""P3 #13 — `/admin/cost` minimal cost-accountability endpoint.

Was Phase-5 candidate (full UI is days of work). Ships a JSON-only
version tonight — analyses the per-call llm_call telemetry that
landed earlier today and surfaces:

  - per-task cost rollup (total in/out tokens, $ estimate, claude
    executor $ when measured)
  - per-caller breakdown (which orchestrator/reviewer phase costs the
    most)
  - aggregate windows (last 24h, last 7d) with avg-per-task

Auth: gated behind ADMIN_TOKEN like the other /admin endpoints. No
write paths. Read-only.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Query, Request

from persistence import store as STORE_module
from persistence.pool import _conn

admin_cost_router = APIRouter()

_OPUS_USD_IN = 5.0
_OPUS_USD_OUT = 25.0


def _check_admin(request: Request) -> None:
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected:
        return  # not configured = open (matches other /admin endpoints)
    got = request.headers.get("x-admin-token") or request.query_params.get("admin_token")
    if not got or not _const_eq(got, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


def _const_eq(a: str, b: str) -> bool:
    """Constant-time string compare to avoid timing-attack leak on
    the admin token (the same pattern other /admin endpoints use)."""
    if len(a) != len(b):
        return False
    out = 0
    for x, y in zip(a, b):
        out |= ord(x) ^ ord(y)
    return out == 0


@admin_cost_router.get("/admin/cost/{task_id}")
def cost_for_task(task_id: str, request: Request):
    """Per-task cost detail. Pulls llm_call events + the task row's
    aggregated counters and surfaces:
      - total_databricks_in/out tokens + USD estimate
      - cost_claude_usd (executor)
      - per-caller breakdown
      - turns + wall_time
    """
    _check_admin(request)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT goal, status, cost_databricks_in, cost_databricks_out, "
            "cost_claude_usd, claude_turn_count, wall_time_secs "
            "FROM tasks WHERE id=%s",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="task not found")
        goal, status, dbx_in, dbx_out, claude_usd, turns, wall = row
        cur.execute(
            "SELECT payload->>'caller' AS caller, "
            "       COUNT(*) AS calls, "
            "       COALESCE(SUM((payload->>'in_tokens')::int), 0)  AS in_tok, "
            "       COALESCE(SUM((payload->>'out_tokens')::int), 0) AS out_tok, "
            "       COALESCE(SUM((payload->>'usd_estimate')::float), 0) AS usd "
            "FROM log_entries WHERE task_id=%s AND kind='llm_call' "
            "GROUP BY caller ORDER BY usd DESC",
            (task_id,),
        )
        callers = [
            {
                "caller": c,
                "calls": calls,
                "in_tokens": int(in_t or 0),
                "out_tokens": int(out_t or 0),
                "usd_estimate": round(float(usd or 0), 4),
            }
            for c, calls, in_t, out_t, usd in cur.fetchall()
        ]
    dbx_usd = round((dbx_in or 0) / 1e6 * _OPUS_USD_IN + (dbx_out or 0) / 1e6 * _OPUS_USD_OUT, 4)
    return {
        "task_id": task_id,
        "goal": (goal or "")[:200],
        "status": status,
        "wall_time_secs": wall,
        "claude_turn_count": turns,
        "cost_databricks": {
            "in_tokens": dbx_in or 0,
            "out_tokens": dbx_out or 0,
            "usd_estimate": dbx_usd,
        },
        "cost_claude_executor_usd": float(claude_usd or 0),
        "estimated_total_usd": round(dbx_usd + float(claude_usd or 0), 4),
        "callers": callers,
    }


@admin_cost_router.get("/admin/cost")
def cost_summary(request: Request, hours: int = Query(24, ge=1, le=720)):
    """Aggregate cost over the last N hours (default 24).
    Returns total + per-caller + avg-per-task."""
    _check_admin(request)
    cutoff = time.time() - hours * 3600
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT COUNT(DISTINCT id) AS n_tasks, "
            "       COALESCE(SUM(cost_databricks_in), 0)  AS dbx_in, "
            "       COALESCE(SUM(cost_databricks_out), 0) AS dbx_out, "
            "       COALESCE(SUM(cost_claude_usd), 0)     AS claude_usd, "
            "       COALESCE(AVG(claude_turn_count), 0)   AS avg_turns, "
            "       COALESCE(AVG(wall_time_secs), 0)      AS avg_wall "
            "FROM tasks WHERE started_at > %s",
            (cutoff,),
        )
        n_tasks, dbx_in, dbx_out, claude_usd, avg_turns, avg_wall = cur.fetchone()
        cur.execute(
            "SELECT payload->>'caller' AS caller, "
            "       COUNT(*) AS calls, "
            "       COALESCE(SUM((payload->>'in_tokens')::int), 0) AS in_tok, "
            "       COALESCE(SUM((payload->>'out_tokens')::int), 0) AS out_tok, "
            "       COALESCE(SUM((payload->>'usd_estimate')::float), 0) AS usd "
            "FROM log_entries WHERE kind='llm_call' AND ts > %s "
            "GROUP BY caller ORDER BY usd DESC",
            (cutoff,),
        )
        callers = [
            {
                "caller": c,
                "calls": calls,
                "in_tokens": int(in_t or 0),
                "out_tokens": int(out_t or 0),
                "usd_estimate": round(float(usd or 0), 4),
            }
            for c, calls, in_t, out_t, usd in cur.fetchall()
        ]
    dbx_usd = round((dbx_in or 0) / 1e6 * _OPUS_USD_IN + (dbx_out or 0) / 1e6 * _OPUS_USD_OUT, 4)
    total_usd = dbx_usd + float(claude_usd or 0)
    return {
        "window_hours": hours,
        "n_tasks": n_tasks or 0,
        "totals": {
            "databricks_in_tokens": dbx_in or 0,
            "databricks_out_tokens": dbx_out or 0,
            "databricks_usd": dbx_usd,
            "claude_executor_usd": float(claude_usd or 0),
            "total_usd": round(total_usd, 4),
        },
        "averages": {
            "usd_per_task": round(total_usd / max(1, n_tasks or 1), 4),
            "turns_per_task": round(float(avg_turns or 0), 1),
            "wall_secs_per_task": round(float(avg_wall or 0), 1),
        },
        "callers": callers,
    }
