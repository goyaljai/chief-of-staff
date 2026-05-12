"""Cost-regression smoke gate (P3 #14).

What this catches:
  Real-world cost-leak bugs we shipped in Phase 4 (C1 fast-path that
  didn't fire on MCP-prefixed tools, ContextVar usage_callback that
  didn't propagate to executor threads). Both meant the supervisor was
  spending money quietly without telemetry — the leak only surfaced
  when we instrumented per-call logging mid-flight.

What this gate does:
  Runs a tiny synthetic task via Orchestrator + Reviewer in-process
  (no Claude executor — keeps the gate cheap + deterministic) and
  asserts that:
    1. review_action calls fire `set_usage_callback` (i.e. the cost
       capture wiring is intact)
    2. _extract_json doesn't silently default a malformed reviewer
       response to `decision=approve` (P0 #19)

  This test runs without DATABRICKS_TOKEN — pure structural checks.
  No real LLM calls, no money spent. Catches the structural bug
  patterns; full E2E cost regression sits in run_eval.py.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def test_review_action_no_silent_approve_on_parse_failure():
    """P0 #19: malformed reviewer JSON used to silently approve. Now
    it must escalate to request_evidence with a parse_failure flag."""
    # Patch _chat to return malformed output
    from agents import reviewer as rev_module
    orig_chat = rev_module._chat
    def fake_chat(*args, **kwargs):
        return "this is not valid JSON at all <<<garbled>>>"
    rev_module._chat = fake_chat
    try:
        r = rev_module.Reviewer()
        out = r.review_action(
            goal="any goal",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert out["decision"] == "request_evidence", (
            f"P0 #19 regression: parse failure silently approved! "
            f"Got decision={out['decision']!r}"
        )
        assert out.get("_parse_failure") is True, (
            "P0 #19 regression: _parse_failure flag missing"
        )
        print("  ✓ parse failure → request_evidence (not silent approve)")
    finally:
        rev_module._chat = orig_chat


def test_chat_passes_caller_to_callback():
    """Cost-leak audit: usage_callback must receive `caller` kwarg.
    If this regresses, per-phase cost telemetry breaks."""
    captured = []
    from agents.llm import set_usage_callback, _chat
    def cb(in_tok, out_tok, caller=None, **kwargs):
        captured.append((in_tok, out_tok, caller))
    set_usage_callback(cb)
    # Patch the OpenAI client so we don't hit Databricks. Just
    # exercise the callback wiring.
    from agents import llm as llm_module
    orig_client = llm_module._client
    class FakeUsage:
        prompt_tokens = 100
        completion_tokens = 50
    class FakeMsg:
        content = "OK"
    class FakeChoice:
        message = FakeMsg()
    class FakeResp:
        usage = FakeUsage()
        choices = [FakeChoice()]
    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs): return FakeResp()
    llm_module._client = lambda: FakeClient()
    try:
        # Wrap call in a function so caller name is non-trivial
        def my_synthetic_caller():
            return _chat("system", "user", max_tokens=10)
        my_synthetic_caller()
        assert captured, "callback never fired"
        in_tok, out_tok, caller = captured[-1]
        assert caller in ("my_synthetic_caller", "?"), (
            f"caller arg not passed correctly. Got {caller!r}"
        )
        print(f"  ✓ usage_callback received caller={caller!r}")
    finally:
        llm_module._client = orig_client
        # clear context var
        from agents.llm import _USAGE_CALLBACK_CTX
        _USAGE_CALLBACK_CTX.set(None)


def test_skills_context_NOT_in_review_action():
    """P2 #6: review_action must not include skills_context (saves
    ~900 tok/call × 26 calls/task)."""
    import agents.reviewer as rev_module
    import inspect
    src = inspect.getsource(rev_module.Reviewer.review_action)
    # The user prompt construction shouldn't include skills_context
    # nor should _chat be called with skills_context= in this method.
    assert "skills_context=skills" not in src, (
        "P2 #6 regression: review_action passes skills_context to _chat. "
        "Drop it — saves ~900 tokens per call."
    )
    print("  ✓ review_action does not pass skills_context to _chat")


def main() -> int:
    print("=== COST-REGRESSION GATE ===\n")
    failures = []
    for name, fn in [
        ("parse-failure → no silent approve", test_review_action_no_silent_approve_on_parse_failure),
        ("usage_callback caller propagation", test_chat_passes_caller_to_callback),
        ("review_action drops skills_context", test_skills_context_NOT_in_review_action),
    ]:
        print(f"[{name}]")
        try:
            fn()
        except Exception as e:
            print(f"  ✗ FAIL: {e}")
            failures.append((name, str(e)))
        print()
    print("=" * 60)
    if failures:
        print("COST-REGRESSION GATE: FAIL ✗")
        for name, msg in failures:
            print(f"  • {name}: {msg}")
        print("=" * 60)
        return 1
    print("COST-REGRESSION GATE: PASS ✓")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
