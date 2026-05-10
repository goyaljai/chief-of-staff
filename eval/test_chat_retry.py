"""Regression test for F5 — Databricks 429 / 5xx / connection retry in _chat.

Validates:
  1. RateLimitError → retried, eventually succeeds
  2. APIConnectionError → retried
  3. InternalServerError → retried
  4. After 4 attempts of consistent failure → raises
  5. Non-transient exception (e.g. ValueError) → NOT retried, raises immediately
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401

from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError


def _fake_response(text="ok"):
    msg = MagicMock(); msg.content = text
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    return resp


def _build_429() -> RateLimitError:
    # Constructors vary by openai version; build a minimal stand-in.
    try:
        return RateLimitError("rate limited", response=MagicMock(status_code=429), body=None)
    except Exception:
        return RateLimitError("rate limited")


def _build_5xx():
    try:
        return InternalServerError("server overloaded", response=MagicMock(status_code=503), body=None)
    except Exception:
        return InternalServerError("server overloaded")


def _build_conn():
    try:
        return APIConnectionError(request=MagicMock())
    except Exception:
        return APIConnectionError("connection error")


def main():
    from agents import llm as orchestrator  # _chat + _client live here after the agents/ refactor
    print("=" * 60)
    print("CHAT RETRY REGRESSION TEST (F5)")
    print("=" * 60)

    # Patch _client so we control the .chat.completions.create call.
    fake_client = MagicMock()
    with patch.object(orchestrator, "_client", return_value=fake_client):
        # 1) one 429 then success
        fake_client.chat.completions.create.side_effect = [_build_429(), _fake_response("hello")]
        out = orchestrator._chat("sys", "user", max_tokens=8)
        assert out == "hello"
        assert fake_client.chat.completions.create.call_count == 2
        print("  ✓ 429 → retried, succeeded on attempt 2")

        # 2) two connection errors then success
        fake_client.chat.completions.create.reset_mock()
        fake_client.chat.completions.create.side_effect = [_build_conn(), _build_conn(), _fake_response("ok")]
        out = orchestrator._chat("sys", "user", max_tokens=8)
        assert out == "ok"
        assert fake_client.chat.completions.create.call_count == 3
        print("  ✓ 2× APIConnectionError → retried, succeeded on attempt 3")

        # 3) 5xx then success
        fake_client.chat.completions.create.reset_mock()
        fake_client.chat.completions.create.side_effect = [_build_5xx(), _fake_response("five")]
        out = orchestrator._chat("sys", "user", max_tokens=8)
        assert out == "five"
        assert fake_client.chat.completions.create.call_count == 2
        print("  ✓ 503 InternalServerError → retried, succeeded on attempt 2")

        # 4) 4 consecutive 429 → raises after exhausting attempts
        fake_client.chat.completions.create.reset_mock()
        fake_client.chat.completions.create.side_effect = [_build_429()] * 4
        try:
            orchestrator._chat("sys", "user", max_tokens=8)
            raise AssertionError("expected RateLimitError to propagate after 4 attempts")
        except RateLimitError:
            assert fake_client.chat.completions.create.call_count == 4
            print("  ✓ 4× 429 → raises after 4 attempts (no infinite retry)")

        # 5) Non-transient exception → no retry
        fake_client.chat.completions.create.reset_mock()
        fake_client.chat.completions.create.side_effect = ValueError("malformed JSON")
        try:
            orchestrator._chat("sys", "user", max_tokens=8)
            raise AssertionError("expected ValueError to propagate immediately")
        except ValueError:
            assert fake_client.chat.completions.create.call_count == 1
            print("  ✓ Non-transient ValueError → raised immediately, no retry")

    print()
    print("=" * 60)
    print("CHAT RETRY TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    # Speed: monkey-patch sleep so the test is instant
    import time as _t
    _t.sleep = lambda s: None  # noqa: E731
    main()
