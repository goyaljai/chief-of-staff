"""Postgres connection pool + schema verification.

V3.5 E5: ThreadedConnectionPool replaces per-call psycopg2.connect. The
prior design serialized every DB call through a global RLock — under
concurrent DAG step events that produced 6.27s wall time on parallel
workloads. The pool eliminates that hot-spot.

Public API:
  init_db()          — verify Postgres extensions + tables on startup
  _conn()            — context manager yielding a pooled connection
  _get_pool()        — singleton accessor (used by the shutdown drain)
  _close_pool()      — closeall() + reset; called during F1 shutdown
  _get_dsn()         — read DATABASE_URL at call time (config.py loads
                       .env later, so we can't snapshot at import time)
"""
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg2


# Kept for legacy direct callers; new code goes through the pool below.
_LOCK = threading.RLock()
_POOL: "psycopg2.pool.ThreadedConnectionPool | None" = None


def _get_pool():
    global _POOL
    if _POOL is None:
        from psycopg2 import pool as _pool
        _POOL = _pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=8,
            dsn=_get_dsn(),
            connect_timeout=10,
        )
    return _POOL


def _close_pool():
    global _POOL
    if _POOL is not None:
        try:
            _POOL.closeall()
        except Exception:
            pass
        _POOL = None


def _get_dsn() -> str:
    """Read DATABASE_URL at call time, not module load time (config.py
    loads .env later)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set. V3.5 requires Postgres + pgvector.")
    return url


def init_db() -> None:
    """Verify Postgres connection + extensions. Schema must be applied via
    migrations/postgres_v3.sql (and v3_5_b2_skills + v2_0_t2_embed_1024)."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname IN ('vector','pg_trgm');")
        exts = {r[0] for r in cur.fetchall()}
        missing = {"vector", "pg_trgm"} - exts
        if missing:
            raise RuntimeError(
                f"Postgres missing extensions: {missing}. "
                "Apply migrations/postgres_v3.sql."
            )
        cur.execute("SELECT to_regclass('public.tasks');")
        if cur.fetchone()[0] is None:
            raise RuntimeError("Tables missing. Apply migrations/postgres_v3.sql.")
        cur.execute("SELECT to_regclass('public.skill_lessons');")
        if cur.fetchone()[0] is None:
            raise RuntimeError(
                "skill_lessons table missing. "
                "Apply migrations/postgres_v3_5_b2_skills.sql."
            )


@contextmanager
def _conn() -> Iterator[psycopg2.extensions.connection]:
    """V3.5 E5: pool-backed connection. Pool reuse removes the global-lock
    serialization that was the root cause of the parallel-DAG hot-spot.
    Bounded retry stays — Supabase free tier still drops idle conns
    occasionally."""
    delays = [0.0, 0.5, 1.5]
    pool = _get_pool()
    last_exc: Exception | None = None
    c: psycopg2.extensions.connection | None = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            c = pool.getconn()
            # Sanity check — if the pool handed us a closed/broken conn
            # (Supabase idle-disconnect), discard and retry.
            if c.closed != 0:
                pool.putconn(c, close=True)
                c = None
                raise psycopg2.OperationalError("pool returned closed conn")
            last_exc = None
            break
        except (psycopg2.OperationalError, Exception) as e:
            # PoolError("connection pool exhausted") is also transient
            # under load.
            last_exc = e
            print(f"[db] pool getconn transient error (will retry): {e}")
            c = None
    if c is None:
        assert last_exc is not None
        raise last_exc
    try:
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            pool.putconn(c)
        except Exception as e:
            print(f"[db] pool putconn failed: {e}")
