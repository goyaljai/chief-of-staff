"""G5 — DSPy-mined skill templates per task family.

Reads recent completed tasks from Postgres, groups them by inferred
family (code/research/writing/data/ops), and asks DSPy to extract the
common shape (objective, deliverable, acceptance, gotchas) that
repeated across that family. The output augments the hand-written
``skills/templates/{family}.md`` scaffolds.

Why this exists:
  The hand-written templates are a reasonable v0, but real prod has
  surfaced patterns we couldn't predict: which deliverables get
  rejected most often, what acceptance phrasing the reviewer
  actually trusts, which gotchas keep recurring. This script reads
  the actual prod history and lets DSPy extract those patterns into
  a structured "common gotchas" list per family.

Run::

    DATABRICKS_TOKEN=... DATABRICKS_BASE_URL=... DATABASE_URL=... \\
        python eval/dspy/mine_templates.py

Output:
  - eval/dspy/optimized/templates/{family}.json — DSPy-mined
    shape for each family, keyed by the gotchas/acceptance the
    history actually validated.

The mined JSON is NOT auto-committed to skills/templates/. A human
reviews the diff and merges promising additions into the markdown
scaffolds — that keeps a human in the loop on what becomes
authoritative guidance.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))


_FAMILY_KEYWORDS = {
    "code": ["build", "implement", "code", "script", "app", "cli", "library",
             "function", "endpoint", "compile", "kotlin", "python", "android",
             "react", "node", "java", "typescript", "fix bug", "refactor"],
    "research": ["research", "find ", "summari", "compare", "what are", "explain",
                 "top ", " best ", "pros and cons", "investigate"],
    "writing": ["blog", "essay", "post", "memo", "press release", "announcement",
                "tweet", "newsletter"],
    "data": ["csv", "dataset", "etl", "pipeline", "schema", "transform", "join",
             "aggregate", "analytics", "analysis"],
    "ops": ["deploy", "kubectl", "kubernetes", "terraform", "ci ", "github actions",
            "cron", "infra", "monitoring", "runbook", "config", "dockerfile"],
}


def _classify(task: str) -> str:
    t = (task or "").lower()
    for fam, kws in _FAMILY_KEYWORDS.items():
        if any(k in t for k in kws):
            return fam
    return "other"


def _fetch_history() -> list[dict]:
    """Pull completed tasks + their final reviews from Postgres.

    Returns a list of ``{task_id, goal, family, summary, deliverables,
    issues, passed}`` rows. Empty list when DATABASE_URL is unset (tests
    can stub this in)."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("[g5] DATABASE_URL unset — skipping prod history mining")
        return []
    try:
        import psycopg2
    except ImportError:
        print("[g5] psycopg2 missing — skipping prod history mining")
        return []
    rows: list[dict] = []
    conn = psycopg2.connect(db_url)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.id, t.goal,
                   l.payload->>'summary'         AS summary,
                   l.payload->>'issues'          AS issues_json,
                   l.payload->>'passed'          AS passed,
                   l.payload->'deliverables'     AS deliverables_json
            FROM tasks t
            JOIN log_entries l ON l.task_id = t.id
            WHERE l.kind = 'final_review' AND t.status = 'done'
            ORDER BY t.started_at DESC
            LIMIT 100
        """)
        for r in cur.fetchall():
            tid, goal, summary, issues_json, passed_str, delivs_json = r
            try:
                issues = json.loads(issues_json) if issues_json else []
            except Exception:
                issues = []
            try:
                deliverables = list(delivs_json) if delivs_json else []
            except Exception:
                deliverables = []
            rows.append({
                "task_id": tid,
                "goal": goal or "",
                "family": _classify(goal or ""),
                "summary": summary or "",
                "issues": issues,
                "passed": (passed_str == "true"),
                "deliverables": deliverables,
            })
    finally:
        conn.close()
    return rows


def _group(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["family"]].append(r)
    return dict(out)


def _mine_family(family: str, rows: list[dict]) -> dict:
    """For a family, ask DSPy to extract the recurring shape. Cheap
    enough to call once per family per run."""
    if not rows:
        return {"family": family, "n": 0, "common_acceptance": [], "common_gotchas": []}
    import dspy

    class FamilyShape(dspy.Signature):
        """Given several completed tasks in the same family, extract the
        recurring shape: which acceptance phrasings actually validated,
        and which gotchas appeared in the reviewer's issues lists."""
        family: str = dspy.InputField()
        sample_tasks: str = dspy.InputField(desc="numbered list of task summaries")
        common_acceptance: list[str] = dspy.OutputField(
            desc="3-7 short acceptance criteria phrasings the reviewer trusted",
        )
        common_gotchas: list[str] = dspy.OutputField(
            desc="3-7 short gotcha phrases the reviewer flagged on failed tasks",
        )

    sample = []
    for i, r in enumerate(rows[:20], 1):
        passed = "PASS" if r["passed"] else "FAIL"
        issues = r["issues"][:3] if r["issues"] else []
        sample.append(
            f"{i}. [{passed}] task={r['goal'][:140]}\n   summary={r['summary'][:140]}\n"
            f"   issues={issues}\n   deliverables={r['deliverables'][:3]}"
        )
    prog = dspy.Predict(FamilyShape)
    pred = prog(family=family, sample_tasks="\n".join(sample))
    return {
        "family": family,
        "n": len(rows),
        "common_acceptance": list(pred.common_acceptance or []),
        "common_gotchas": list(pred.common_gotchas or []),
    }


def main() -> int:
    load_dotenv()
    token = os.environ.get("DATABRICKS_TOKEN")
    base = os.environ.get("DATABRICKS_BASE_URL")
    if not token or not base:
        print("[g5] DATABRICKS_TOKEN + DATABRICKS_BASE_URL must be set")
        return 1
    import dspy
    # Reuse the same LM-config helper from optimize.py for consistency.
    from eval.dspy.optimize import _maybe_wire_langsmith
    _maybe_wire_langsmith()
    lm = dspy.LM(
        model="openai/databricks-claude-opus-4-7",
        api_base=base,
        api_key=token,
        max_tokens=2048,
        temperature=None,
    )
    dspy.configure(lm=lm)

    rows = _fetch_history()
    if not rows:
        print("[g5] no completed tasks in history; nothing to mine")
        return 0
    grouped = _group(rows)

    out_dir = _HERE / "optimized" / "templates"
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for family, frows in sorted(grouped.items()):
        if family == "other" or not frows:
            continue
        print(f"[g5] mining family={family} (n={len(frows)})")
        result = _mine_family(family, frows)
        out_path = out_dir / f"{family}.json"
        with open(out_path, "w") as f:
            json.dump(result, f, indent=2)
        summary.append(result)
        print(f"  → {out_path}")

    summary_path = out_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n[g5] summary written to {summary_path}")
    print(
        "[g5] review the per-family JSONs and merge the high-signal\n"
        "     entries into skills/templates/{family}.md by hand."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
