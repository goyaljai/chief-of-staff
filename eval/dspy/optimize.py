"""DSPy proxy-metric optimization (T3, $5-10 budget).

Two programs are optimized:
  - QuestionGen (orchestrator's clarifying-question step)
  - Reviewer (final_review)

Optimizer: ``BootstrapFewShotWithRandomSearch`` — much cheaper than
MIPROv2, gives a real signal on prompt structure + few-shot picks,
fits comfortably under $5 for both programs combined.

Outputs:
  - eval/dspy/optimized/question_gen.json
  - eval/dspy/optimized/reviewer.json

These artifacts are what prod consumes. The plan in a follow-up
commit is to either:
  (a) drop the mined demonstrations directly into prompts/*.md,
  (b) load the compiled JSON at orchestrator startup and apply the
      bound demos to the system prompt.

Run::

    DATABRICKS_TOKEN=... DATABRICKS_BASE_URL=... \\
      python eval/dspy/optimize.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import dspy
from dotenv import load_dotenv

# Reach the project root so we can import the dataset module by file.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent))

from eval.dspy.dataset import QUESTION_GEN, REVIEWER, split  # noqa: E402
from eval.dspy.signatures import QuestionGenSig, ReviewerSig  # noqa: E402


def _maybe_wire_langsmith() -> None:
    """Route DSPy/litellm calls into LangSmith when the user has tracing
    on. DSPy uses litellm under the hood; litellm exposes a built-in
    LangSmith callback that captures every LLM call. We turn it on
    only when ``LANGSMITH_TRACING=true`` AND ``LANGSMITH_API_KEY`` is
    set so it stays opt-in and silent in CI by default.

    The project name defaults to ``cos-dspy-t3`` so the optimizer's
    runs are easy to filter from the prod orchestrator + reviewer
    traces. Override with ``LANGSMITH_PROJECT_DSPY`` if you'd rather
    co-mingle them.
    """
    if os.environ.get("LANGSMITH_TRACING", "").lower() not in ("true", "1", "yes"):
        return
    if not os.environ.get("LANGSMITH_API_KEY"):
        print("[langsmith] LANGSMITH_TRACING=true but LANGSMITH_API_KEY missing; skipping DSPy trace wiring")
        return
    project = os.environ.get("LANGSMITH_PROJECT_DSPY") or os.environ.get(
        "LANGSMITH_PROJECT", "cos-dspy-t3",
    )
    os.environ.setdefault("LANGSMITH_PROJECT", project)
    try:
        import litellm
        cbs = list(getattr(litellm, "success_callback", None) or [])
        if "langsmith" not in cbs:
            cbs.append("langsmith")
            litellm.success_callback = cbs
        cbs_fail = list(getattr(litellm, "failure_callback", None) or [])
        if "langsmith" not in cbs_fail:
            cbs_fail.append("langsmith")
            litellm.failure_callback = cbs_fail
        print(f"[langsmith] DSPy/litellm traces will land in project '{project}'")
    except Exception as e:
        print(f"[langsmith] failed to enable DSPy trace wiring: {e}")


def _configure_lm() -> dspy.LM:
    """Wire DSPy's LM to the same Databricks AI Gateway prod uses.

    Why: the optimization should target the exact model the
    orchestrator uses in prod (Opus 4.7), not a cheaper proxy. The
    DSPy litellm provider supports OpenAI-compatible bases, which
    is what the gateway exposes.
    """
    load_dotenv()
    token = os.environ.get("DATABRICKS_TOKEN")
    base = os.environ.get("DATABRICKS_BASE_URL")
    if not token or not base:
        raise SystemExit(
            "[T3] DATABRICKS_TOKEN + DATABRICKS_BASE_URL must be set in env."
        )
    _maybe_wire_langsmith()
    lm = dspy.LM(
        model="openai/databricks-claude-opus-4-7",
        api_base=base,
        api_key=token,
        max_tokens=2048,
        temperature=None,
    )
    dspy.configure(lm=lm)
    return lm


def question_gen_metric(example: dspy.Example, pred, trace=None) -> float:
    """1.0 if `done` matches AND when not done the question contains
    at least one of the expected keywords. 0 otherwise."""
    if pred.done != example.expected_done:
        return 0.0
    if example.expected_done:
        return 1.0
    keywords = getattr(example, "keywords_must_appear_in_question", []) or []
    if not keywords:
        return 1.0
    q = (pred.next_question or "").lower()
    return 1.0 if any(k.lower() in q for k in keywords) else 0.0


def reviewer_metric(example: dspy.Example, pred, trace=None) -> float:
    """1.0 if (a) passed matches, (b) deliverables include all
    must-include strings as substrings, (c) deliverables exclude all
    must-exclude strings. 0 otherwise."""
    if pred.passed != example.expected_passed:
        return 0.0
    must_inc = getattr(example, "deliverables_must_include", None) or []
    must_exc = getattr(example, "deliverables_must_exclude", None) or []
    delivs = pred.deliverables or []
    if not isinstance(delivs, list):
        delivs = []
    delivs_str = " ".join(str(d) for d in delivs)
    for sub in must_inc:
        if sub not in delivs_str:
            return 0.0
    for sub in must_exc:
        if sub in delivs_str:
            return 0.0
    return 1.0


def _evaluate(program, dataset, metric) -> tuple[float, int, int]:
    """Tiny eval: run program on each example, return (avg, passed, total).

    DSPy ships a richer ``Evaluate`` class but the surface keeps
    shifting; a hand-rolled loop is forward-compatible."""
    passed = 0
    for ex in dataset:
        try:
            pred = program(**ex.inputs())
            score = metric(ex, pred)
        except Exception as e:
            print(f"  [eval] error on example: {e}")
            score = 0.0
        if score >= 1.0:
            passed += 1
    total = len(dataset)
    return (passed / total if total else 0.0), passed, total


def _compile(name: str, signature, dataset, metric, max_demos: int = 3):
    """Run BootstrapFewShotWithRandomSearch and report before/after.

    Caps:
      - max_bootstrapped_demos = max_demos (default 3) — the optimizer
        won't try to stuff more than 3 examples into the prompt.
      - num_candidate_programs = 4 — only try 4 random demo
        permutations (cheap, still a real gradient).
    """
    print(f"\n=== {name} ===")
    train, dev = split(dataset)
    print(f"  train={len(train)}  dev={len(dev)}")

    program = dspy.Predict(signature)
    pre_avg, pre_p, pre_t = _evaluate(program, dev, metric)
    print(f"  before optimization: dev avg = {pre_avg:.2f}  ({pre_p}/{pre_t})")

    optimizer_cls = None
    try:
        from dspy.teleprompt import BootstrapFewShotWithRandomSearch
        optimizer_cls = BootstrapFewShotWithRandomSearch
    except ImportError:
        try:
            from dspy.teleprompt import BootstrapFewShot
            optimizer_cls = BootstrapFewShot
        except ImportError as e:
            raise SystemExit(f"[T3] DSPy optimizer not importable: {e}")

    optimizer = optimizer_cls(
        metric=metric,
        max_bootstrapped_demos=max_demos,
        max_labeled_demos=max_demos,
        **(
            {"num_candidate_programs": 4}
            if optimizer_cls.__name__ == "BootstrapFewShotWithRandomSearch"
            else {}
        ),
    )
    print(f"  optimizing with {optimizer_cls.__name__}...")
    t0 = time.time()
    compiled = optimizer.compile(program, trainset=train, valset=dev)
    elapsed = time.time() - t0
    print(f"  optimization wall-time: {elapsed:.1f}s")

    post_avg, post_p, post_t = _evaluate(compiled, dev, metric)
    delta_pp = (post_avg - pre_avg) * 100.0
    print(f"  after  optimization: dev avg = {post_avg:.2f}  ({post_p}/{post_t})")
    print(f"  delta: {delta_pp:+.1f} pp")

    out_dir = _HERE / "optimized"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"{name}.json"
    try:
        compiled.save(str(out_path))
    except Exception as e:
        # Some DSPy versions need .json suffix removed; fall back to
        # writing the demos manually so we always have an artifact.
        print(f"  [warn] compiled.save failed ({e}); writing demos manually")
        demos = [d.toDict() for d in getattr(compiled, "demos", [])] if hasattr(compiled, "demos") else []
        with open(out_path, "w") as f:
            json.dump({"demos": demos, "name": name}, f, indent=2)

    print(f"  saved to {out_path}")
    return {
        "name": name,
        "before_avg": pre_avg,
        "after_avg": post_avg,
        "delta_pp": delta_pp,
        "elapsed_s": elapsed,
        "out_path": str(out_path),
    }


def main() -> int:
    _configure_lm()
    print("[T3] DSPy proxy-metric optimization — $5-10 budget cap")

    results = []
    results.append(_compile("question_gen", QuestionGenSig, QUESTION_GEN, question_gen_metric))
    results.append(_compile("reviewer", ReviewerSig, REVIEWER, reviewer_metric))

    summary_path = _HERE / "optimized" / "summary.json"
    summary_path.parent.mkdir(exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n[T3] summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
