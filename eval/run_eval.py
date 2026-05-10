"""V3 #13: Eval harness runner.

Runs each task in eval_suite.json against the local server, captures pass/fail,
duration, cost. Useful for: regression tracking across prompt changes, A/B
comparing orchestrator versions, validating fine-tuned model improvements.

Usage:
  python3 eval/run_eval.py --base-url http://localhost:8000

Output: eval/results/{timestamp}.json
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx


def matches_any(name: str, patterns: list) -> bool:
    return any(re.match(p, name) for p in patterns)


def run_one(client: httpx.Client, base: str, task: dict, timeout: int = 1500) -> dict:
    started = time.time()
    r = client.post(f"{base}/task/run", json={
        "task": task["task"],
        "clarifications": task.get("clarifications", {}),
    }, timeout=60)
    r.raise_for_status()
    task_id = r.json()["task_id"]

    while time.time() - started < timeout:
        try:
            s = client.get(f"{base}/task/{task_id}", timeout=60).json()
        except Exception as e:
            print(f"   [poll error: {e}, retrying]")
            time.sleep(8)
            continue
        if s["status"] in ("done", "failed", "abandoned", "cancelled"):
            break
        time.sleep(8)
    else:
        return {"id": task["id"], "task_id": task_id, "outcome": "TIMEOUT", "duration_secs": time.time() - started}

    expected = task.get("expected", {})
    final = client.get(f"{base}/task/{task_id}", timeout=30).json()
    result = final.get("result") or {}
    artifacts = final.get("artifacts") or []
    artifact_paths = [a["path"] for a in artifacts]

    checks = {}
    if "deliverable_files" in expected:
        checks["deliverable_files"] = all(
            any(matches_any(p, [pat]) for p in artifact_paths)
            for pat in expected["deliverable_files"]
        )
    if "should_pass_review" in expected:
        checks["should_pass_review"] = bool(result.get("success")) == bool(expected["should_pass_review"])
    if "max_loops" in expected:
        checks["max_loops"] = (result.get("loops", 99) <= expected["max_loops"])
    if "next_steps_must_include" in expected:
        ns = result.get("next_steps", "") or ""
        checks["next_steps_must_include"] = all(s in ns for s in expected["next_steps_must_include"])
    if "next_steps_must_include_any_of" in expected:
        ns = result.get("next_steps", "") or ""
        ok = True
        for group in expected["next_steps_must_include_any_of"]:
            if not any(re.search(p, ns) for p in group):
                ok = False
                break
        checks["next_steps_must_include_any_of"] = ok
    if "summary_must_mention" in expected:
        s = result.get("summary", "") or ""
        checks["summary_must_mention"] = all(w.lower() in s.lower() for w in expected["summary_must_mention"])

    all_passed = all(checks.values()) if checks else None

    return {
        "id": task["id"],
        "task_id": task_id,
        "status": final["status"],
        "duration_secs": round(final.get("duration_secs", 0), 1),
        "cost": final.get("cost"),
        "loops": result.get("loops"),
        "outcome": "PASS" if all_passed else "FAIL",
        "checks": checks,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="(single-run) target server")
    p.add_argument("--suite", default=str(Path(__file__).parent / "eval_suite.json"))
    p.add_argument("--filter", default=None, help="run only tasks matching this id substring")
    p.add_argument("--fast-only", action="store_true",
                   help="restrict to suite['fast_ids'] — used by CI")
    p.add_argument("--ab", nargs=2, metavar=("URL_A", "URL_B"),
                   help="A/B mode: run the same suite against two servers and diff pass rates")
    p.add_argument("--label-a", default="A", help="label for the first variant")
    p.add_argument("--label-b", default="B", help="label for the second variant")
    args = p.parse_args()

    suite = json.loads(Path(args.suite).read_text())
    tasks = suite["tasks"]
    if args.fast_only:
        fast_ids = set(suite.get("fast_ids", []))
        tasks = [t for t in tasks if t["id"] in fast_ids]
    if args.filter:
        tasks = [t for t in tasks if args.filter in t["id"]]

    if args.ab:
        return _run_ab(tasks, args.ab[0], args.ab[1], args.label_a, args.label_b)
    return _run_single(tasks, args.base_url)


def _run_single(tasks: list, base_url: str):
    print(f"running {len(tasks)} eval tasks against {base_url}")
    results = []
    with httpx.Client() as client:
        for t in tasks:
            print(f" [{t['id']}] running...")
            try:
                r = run_one(client, base_url, t)
            except Exception as e:
                r = {"id": t["id"], "outcome": "ERROR", "error": str(e)}
            print(f"  → {r.get('outcome', '?')} {r.get('checks', {})}")
            results.append(r)

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    summary = _summarize(results)
    out_file.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(f"\nresults: {out_file}")
    print(summary)
    # Exit code: 0 if every task PASSed; 1 otherwise — needed for CI gating.
    sys.exit(0 if summary.get("pass_rate") == 1.0 else 1)


def _run_side(tasks: list, base_url: str, label: str) -> list:
    out: list = []
    with httpx.Client() as client:
        for t in tasks:
            print(f" [{label}] {t['id']}")
            try:
                r = run_one(client, base_url, t)
            except Exception as e:
                r = {"id": t["id"], "outcome": "ERROR", "error": str(e)}
            out.append(r)
    return out


def _run_ab(tasks: list, url_a: str, url_b: str, label_a: str, label_b: str):
    """C2: run the same eval suite against two servers and diff their results.
    Useful for orchestrator prompt/model A/B testing — baseline vs candidate."""
    print(f"A/B: {label_a}={url_a}  vs  {label_b}={url_b}  ({len(tasks)} tasks each)")
    res_a = _run_side(tasks, url_a, label_a)
    res_b = _run_side(tasks, url_b, label_b)

    by_id_a = {r["id"]: r for r in res_a}
    by_id_b = {r["id"]: r for r in res_b}
    per_task = []
    for t in tasks:
        a = by_id_a.get(t["id"], {"outcome": "MISSING"})
        b = by_id_b.get(t["id"], {"outcome": "MISSING"})
        per_task.append({
            "id": t["id"],
            label_a: a.get("outcome"),
            label_b: b.get("outcome"),
            "regressed": (a.get("outcome") == "PASS" and b.get("outcome") != "PASS"),
            "improved":  (a.get("outcome") != "PASS" and b.get("outcome") == "PASS"),
        })

    sum_a = _summarize(res_a)
    sum_b = _summarize(res_b)
    regressions = [p["id"] for p in per_task if p["regressed"]]
    improvements = [p["id"] for p in per_task if p["improved"]]
    diff_summary = {
        f"{label_a}_pass_rate": sum_a.get("pass_rate"),
        f"{label_b}_pass_rate": sum_b.get("pass_rate"),
        "regressions": regressions,
        "improvements": improvements,
        "delta_pass_rate": (sum_b.get("pass_rate", 0) - sum_a.get("pass_rate", 0)),
    }

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"ab_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps({
        "diff_summary": diff_summary,
        "per_task": per_task,
        f"{label_a}_results": res_a,
        f"{label_b}_results": res_b,
    }, indent=2))
    print(f"\nresults: {out_file}")
    print(json.dumps(diff_summary, indent=2))
    # CI gating: fail if B regressed any task vs A.
    sys.exit(1 if regressions else 0)


def _summarize(results):
    total = len(results)
    passed = sum(1 for r in results if r.get("outcome") == "PASS")
    failed = sum(1 for r in results if r.get("outcome") == "FAIL")
    errored = sum(1 for r in results if r.get("outcome") == "ERROR")
    return {"total": total, "pass": passed, "fail": failed, "error": errored, "pass_rate": round(passed/total*100, 1) if total else 0}


if __name__ == "__main__":
    main()
