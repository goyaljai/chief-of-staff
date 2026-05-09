"""V3 #5: SQLite → Postgres+pgvector migration script.

Usage:
  python3 migrations/db_migrate.py \\
    --sqlite=data/chief.db \\
    --postgres="postgresql://user:pass@host:5432/cos"

Reads tasks + log_entries from SQLite, computes embeddings via Chroma's
default model, writes to Postgres. Idempotent on task id.

This script is a stub — full migration runs in the V3.x sprint.
"""
import argparse
import json
import sqlite3
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sqlite", required=True)
    p.add_argument("--postgres", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    print(f"[migrate] reading from {args.sqlite}")
    sc = sqlite3.connect(args.sqlite)
    sc.row_factory = sqlite3.Row
    tasks = sc.execute("SELECT * FROM tasks").fetchall()
    print(f"[migrate] found {len(tasks)} tasks")

    if args.dry_run:
        for r in tasks[:3]:
            print(" sample:", dict(r))
        return

    try:
        import psycopg2
    except ImportError:
        print("Install psycopg2-binary first: pip install psycopg2-binary")
        sys.exit(1)

    pg = psycopg2.connect(args.postgres)
    pg_cur = pg.cursor()
    for r in tasks:
        pg_cur.execute(
            """INSERT INTO tasks (id, goal, clarifications, workspace, status, skill_md,
                                  skill_name, skill_description, brief, result,
                                  started_at, finished_at,
                                  cost_databricks_in, cost_databricks_out, cost_claude_usd, keep_workspace)
               VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO NOTHING""",
            (r["id"], r["goal"], r["clarifications"], r["workspace"], r["status"],
             r["skill_md"], r["skill_name"], r["skill_description"], r["brief"], r["result"],
             r["started_at"], r["finished_at"],
             r["cost_databricks_in"], r["cost_databricks_out"], r["cost_claude_usd"], bool(r["keep_workspace"])),
        )
    pg.commit()
    print(f"[migrate] migrated {len(tasks)} tasks to Postgres")


if __name__ == "__main__":
    main()
