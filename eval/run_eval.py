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
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--suite", default=str(Path(__file__).parent / "eval_suite.json"))
    p.add_argument("--filter", default=None, help="run only tasks matching this id substring")
    args = p.parse_args()

    suite = json.loads(Path(args.suite).read_text())
    tasks = suite["tasks"]
    if args.filter:
        tasks = [t for t in tasks if args.filter in t["id"]]
    print(f"running {len(tasks)} eval tasks against {args.base_url}")

    results = []
    with httpx.Client() as client:
        for t in tasks:
            print(f" [{t['id']}] running...")
            try:
                r = run_one(client, args.base_url, t)
            except Exception as e:
                r = {"id": t["id"], "outcome": "ERROR", "error": str(e)}
            print(f"  → {r.get('outcome', '?')} {r.get('checks', {})}")
            results.append(r)

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(json.dumps({"summary": _summarize(results), "results": results}, indent=2))
    print(f"\nresults: {out_file}")
    print(_summarize(results))


def _summarize(results):
    total = len(results)
    passed = sum(1 for r in results if r.get("outcome") == "PASS")
    failed = sum(1 for r in results if r.get("outcome") == "FAIL")
    errored = sum(1 for r in results if r.get("outcome") == "ERROR")
    return {"total": total, "pass": passed, "fail": failed, "error": errored, "pass_rate": round(passed/total*100, 1) if total else 0}


if __name__ == "__main__":
    main()
