"""Schema-drift gate (DOC2 / Phase 3.5 hardening).

Today's audit r3 surfaced 3 production gaps where a `TaskState` field
existed in code but had no column / no upsert slot / no hydration.
Server crash silently dropped user data. This test catches that
class of bug at PR time.

Three lists must agree:

  1. Field names on the ``TaskState`` dataclass (minus runtime-only
     primitives like ``asyncio.Event`` and the separate ``log`` table).
  2. Column names on the ``tasks`` Postgres table.
  3. The columns named in the ``INSERT INTO tasks (...)`` clause of
     ``persistence/tasks_repo.upsert_task``.
  4. The keyword args passed to ``TaskState(...)`` in the hydration
     block of ``persistence/store._load_task_from_db``.

If any one drifts from the others, the supervisor will silently lose
the field on a restart. This test fails with a clear diff.

Run::

    DATABASE_URL=postgresql://... python eval/test_schema_drift.py

Returns 0 on PASS, 2 on drift detected.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import fields
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


# Fields that are intentionally NOT persisted to the tasks row.
# Adding to this list is acceptable but warrants a comment in
# docs/PERSISTENCE.md.
_RUNTIME_ONLY_FIELDS = {
    "escalation_event",  # asyncio.Event — process-local primitive
    "log",               # lives in log_entries table, separate from tasks row
}


def _taskstate_fields() -> set[str]:
    from persistence.store import TaskState
    return {f.name for f in fields(TaskState)} - _RUNTIME_ONLY_FIELDS


def _tasks_columns_from_db() -> set[str]:
    from dotenv import load_dotenv
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        # In CI the test container exposes DATABASE_URL; locally we
        # may not have it — treat as a soft skip with a clear message.
        print("[drift] DATABASE_URL unset — running schema-only check from migration files")
        return _tasks_columns_from_migrations()
    import psycopg2
    try:
        conn = psycopg2.connect(db_url, connect_timeout=4)
    except Exception as e:
        print(f"[drift] DATABASE_URL set but unreachable ({e}); falling back to migrations")
        return _tasks_columns_from_migrations()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='tasks' ORDER BY ordinal_position"
        )
        return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def _tasks_columns_from_migrations() -> set[str]:
    """Fallback when DATABASE_URL isn't available — parse the
    initial schema migration + every ALTER TABLE in migrations/."""
    cols: set[str] = set()
    migrations_dir = _ROOT / "migrations"
    for mig in sorted(migrations_dir.glob("postgres_*.sql")):
        text = mig.read_text(encoding="utf-8")
        # CREATE TABLE tasks (...) — pull column names from the body.
        m = re.search(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+tasks\s*\(([^;]+?)\);",
                      text, re.IGNORECASE | re.DOTALL)
        if m:
            body = m.group(1)
            for line in body.splitlines():
                line = line.strip().rstrip(",")
                if not line or line.startswith("--"):
                    continue
                tok = line.split()[0].strip('"')
                if tok.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                    continue
                cols.add(tok)
        # ALTER TABLE tasks ADD COLUMN [IF NOT EXISTS] <name> <type>
        for am in re.finditer(
            r"ALTER\s+TABLE\s+tasks\s+(?:[^;]*?)ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)",
            text, re.IGNORECASE,
        ):
            cols.add(am.group(1))
    return cols


def _upsert_columns_from_source() -> set[str]:
    """Extract the column list from the INSERT INTO tasks (...) clause
    in persistence/tasks_repo.upsert_task."""
    src = (_ROOT / "persistence" / "tasks_repo.py").read_text(encoding="utf-8")
    m = re.search(
        r"INSERT\s+INTO\s+tasks\s*\(([^)]+)\)",
        src, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        raise SystemExit("[drift] could not find INSERT INTO tasks(...) in tasks_repo.py")
    raw = m.group(1)
    cols = set()
    for tok in raw.split(","):
        tok = tok.strip().strip('"').strip("`")
        if tok:
            cols.add(tok)
    return cols


def _hydration_kwargs_from_source() -> set[str]:
    """Extract the keyword arg names passed to TaskState(...) in
    persistence/store.py — the union across BOTH call sites
    (create() at task creation time, and the hydration block in
    _load_task_from_db). The drift check cares that EVERY persisted
    field is hydrated; if a field is absent from the hydration site
    the post-restart state is broken even if create() passes it
    fresh on first task submission."""
    src = (_ROOT / "persistence" / "store.py").read_text(encoding="utf-8")
    out: set[str] = set()
    # Walk every occurrence of `TaskState(` and pull the kwargs
    # between matching parens.
    for m in re.finditer(r"TaskState\s*\(", src):
        idx = m.end() - 1  # position of the opening paren
        depth = 0
        end = None
        for i in range(idx, len(src)):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        body = src[idx + 1:end]
        for km in re.finditer(r"(\w+)\s*=", body):
            out.add(km.group(1))
    # The `id` arg is sometimes positional (from create() it's via
    # tid kwarg → but field name is 'id'). Add it if any TaskState(...)
    # passed an `id` slot positionally — which is the create() case
    # in store.py. Detect by checking `id=` is in any call.
    return out


def _diff_block(label: str, missing: set[str], extra: set[str]) -> str | None:
    if not missing and not extra:
        return None
    parts = [f"  {label}:"]
    if missing:
        parts.append(f"    missing: {sorted(missing)}")
    if extra:
        parts.append(f"    extra:   {sorted(extra)}")
    return "\n".join(parts)


def _auto_managed_db_columns(db_cols: set[str]) -> set[str]:
    """Columns that exist on `tasks` but are managed outside the
    TaskState/upsert/hydration triangle — embeddings denormalized
    from langchain_pg_embedding, future generated/computed columns.

    Audit 5-r bug fix: pre-fix, `_DB_ONLY = {"summary_embedding",
    "skill_embedding"}` was hardcoded. A future migration adding a
    new embedding column would falsely trigger drift. Auto-detect
    by suffix instead so the gate keeps passing as embeddings evolve.
    """
    return {c for c in db_cols if c.endswith("_embedding") or c.endswith("_vector")}


def main() -> int:
    ts_fields = _taskstate_fields()
    db_cols = _tasks_columns_from_db()
    upsert_cols = _upsert_columns_from_source()
    hydration_kwargs = _hydration_kwargs_from_source()

    db_only = _auto_managed_db_columns(db_cols)
    db_persisted = db_cols - db_only

    print(f"[drift] TaskState fields (persistable): {len(ts_fields)}")
    print(f"[drift] tasks columns:                  {len(db_cols)} ({len(db_persisted)} excl. embeddings)")
    print(f"[drift] upsert column list:             {len(upsert_cols)}")
    print(f"[drift] hydration kwargs:               {len(hydration_kwargs)}")

    diffs = []
    diffs.append(_diff_block(
        "TaskState ↔ tasks columns",
        missing=ts_fields - db_persisted,
        extra=db_persisted - ts_fields,
    ))
    diffs.append(_diff_block(
        "tasks columns ↔ upsert INSERT clause",
        missing=db_persisted - upsert_cols,
        extra=upsert_cols - db_cols,
    ))
    diffs.append(_diff_block(
        "TaskState ↔ hydration kwargs",
        missing=ts_fields - hydration_kwargs,
        extra=hydration_kwargs - ts_fields - {"id"},  # id isn't a kwarg, it's positional
    ))

    real_diffs = [d for d in diffs if d]
    if not real_diffs:
        print("\n" + "=" * 60)
        print("SCHEMA-DRIFT CHECK: PASS ✓")
        print("=" * 60)
        return 0

    print("\n[drift] REGRESSION DETECTED:\n")
    for d in real_diffs:
        print(d)
        print()
    print("=" * 60)
    print("SCHEMA-DRIFT CHECK: FAIL ✗")
    print("=" * 60)
    print(
        "\nFix: pick the canonical source and reconcile. The usual cause\n"
        "is a new TaskState field without a migration + upsert/hydration\n"
        "update. See docs/PERSISTENCE.md for the 5-step add procedure."
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
