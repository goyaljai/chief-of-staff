#!/usr/bin/env python3
"""T5 — Promptfoo pass-rate gate.

Reads the JSON output of `promptfoo eval --output results.json`, computes
pass-rate, and exits non-zero if the rate dropped more than 5 percentage
points relative to ``eval/promptfoo/baseline.json``.

CI flow::

    promptfoo eval -c eval/promptfoo/promptfooconfig.yaml \
                   --output eval/promptfoo/run.json --no-write
    python eval/promptfoo/check_pass_rate.py \
        --baseline eval/promptfoo/baseline.json \
        --run eval/promptfoo/run.json \
        --tolerance 5

If ``baseline.json`` doesn't exist, the script writes the current run as
the baseline and exits 0 — the first PR adds the baseline, subsequent PRs
get gated against it. Reset the baseline by deleting the file and
re-running the script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _pass_rate(run_data: dict) -> tuple[float, int, int]:
    """Extract (pass_rate_pct, passed, total) from a Promptfoo run JSON.

    Promptfoo's JSON shape has changed across versions; we try the two
    most common keys and fall back to walking ``results.results[]`` for
    a per-row pass tally."""
    results = run_data.get("results") or {}
    stats = results.get("stats") or run_data.get("stats") or {}
    successes = stats.get("successes")
    failures = stats.get("failures")
    if isinstance(successes, int) and isinstance(failures, int):
        total = successes + failures
        if total == 0:
            return 0.0, 0, 0
        return (successes / total) * 100.0, successes, total
    rows = results.get("results") or run_data.get("results", []) or []
    if not isinstance(rows, list):
        rows = []
    passed = sum(1 for r in rows if r.get("success") is True)
    total = len(rows)
    if total == 0:
        return 0.0, 0, 0
    return (passed / total) * 100.0, passed, total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, help="baseline.json path")
    parser.add_argument("--run", required=True, action="append",
                        help="run.json path. Pass multiple times to combine across configs.")
    parser.add_argument("--tolerance", type=float, default=5.0,
                        help="max allowed drop in percentage points (default: 5)")
    parser.add_argument("--init-baseline", action="store_true",
                        help="explicitly write the current run as the baseline. "
                             "Required when baseline.json is missing — without "
                             "this flag, a missing baseline is a hard failure "
                             "(prevents silent regressions if the file is "
                             "deleted or lost).")
    args = parser.parse_args()

    run_passed = 0
    run_total = 0
    for path in args.run:
        if not os.path.isfile(path):
            print(f"[gate] FAIL: run file not found: {path}", file=sys.stderr)
            return 2
        with open(path, "r") as f:
            data = json.load(f)
        _, p, t = _pass_rate(data)
        run_passed += p
        run_total += t

    if run_total == 0:
        print("[gate] FAIL: no test results across all run files", file=sys.stderr)
        return 2
    run_pct = (run_passed / run_total) * 100.0
    print(f"[gate] this run: {run_passed}/{run_total} pass ({run_pct:.1f}%) "
          f"across {len(args.run)} config(s)")

    if not os.path.isfile(args.baseline):
        if not args.init_baseline:
            # Bug fix (Phase 3 audit): silently writing a fresh
            # baseline when the file is missing was a footgun — anyone
            # accidentally deleting baseline.json would mask the next
            # PR's regression. Now require an explicit flag.
            print(
                f"[gate] FAIL: no baseline at {args.baseline} and --init-baseline not set.\n"
                "       Pass --init-baseline ONCE to write the current run as the\n"
                "       new baseline (e.g. after a deliberate prompt change).",
                file=sys.stderr,
            )
            return 2
        print(f"[gate] writing current run as new baseline at {args.baseline}")
        with open(args.baseline, "w") as f:
            json.dump({"pass_rate_pct": run_pct, "passed": run_passed, "total": run_total}, f, indent=2)
        return 0

    with open(args.baseline, "r") as f:
        base = json.load(f)
    base_pct = float(base.get("pass_rate_pct", 0.0))
    base_passed = base.get("passed", "?")
    base_total = base.get("total", "?")
    drop = base_pct - run_pct
    print(f"[gate] baseline: {base_passed}/{base_total} pass ({base_pct:.1f}%)  →  drop {drop:+.1f} pp")

    if drop > args.tolerance:
        print(f"[gate] FAIL: drop {drop:.1f} pp exceeds tolerance {args.tolerance:.1f} pp", file=sys.stderr)
        return 1

    print(f"[gate] PASS: drop {drop:.1f} pp within tolerance {args.tolerance:.1f} pp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
