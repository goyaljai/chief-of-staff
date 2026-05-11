"""B4 regression — conditional reviewer self-check on read-heavy streaks.

Exercises SupervisorLoop._on_event directly (no Claude subprocess, no
inner-loop) and confirms:

  1. A streak of N non-side-effecting tool_uses triggers exactly ONE
     self-check (the streak then resets).
  2. A side-effecting tool resets the streak — even if N reads
     happened just before, the next read should start a fresh count.
  3. Self-check budget caps at MAX_SELF_CHECKS_PER_LOOP per outer loop.
  4. A self-check returning ``decision=correct`` elevates to B1's
     mid-stream coaching path (sets _mid_stream_coaching + interrupts).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401

os.environ.setdefault("DATABRICKS_TOKEN", "test-token-not-real")
os.environ.setdefault("DATABRICKS_BASE_URL", "http://test-base-url.invalid")
os.environ.setdefault("VOYAGE_API_KEY", "test-voyage-key-not-real")
os.environ["COS_ALLOW_DESTRUCTIVE_TESTS"] = "1"

from runners.events import ClaudeEvent  # noqa: E402
from supervisor_loop import (  # noqa: E402
    MAX_MID_STREAM_INTERRUPTS,
    MAX_SELF_CHECKS_PER_LOOP,
    SELF_CHECK_AFTER_N_NON_REVIEWED,
    SupervisorLoop,
)


class _StubReviewer:
    """Records every review_action call and returns scripted decisions."""

    def __init__(self, decision="approve", message="ok"):
        self.calls: list[dict] = []
        self.decision = decision
        self.message = message

    def review_action(self, **kwargs):
        self.calls.append({"method": "review_action", **kwargs})
        return {"decision": self.decision, "message": self.message}

    def drift_check(self, **kwargs):
        self.calls.append({"method": "drift_check", **kwargs})
        return {"decision": self.decision, "message": self.message}


class _StubRunner:
    """Just tracks interrupts."""

    def __init__(self):
        self.interrupt_count = 0

    def interrupt(self):
        self.interrupt_count += 1


def _make_loop(reviewer_decision="approve", reviewer_message="ok") -> SupervisorLoop:
    from persistence import TaskState
    state = TaskState(
        id="b4_test_" + os.urandom(3).hex(),
        goal="read a bunch of files and summarize",
        clarifications={},
        workspace="/tmp/cos-b4-test",
    )
    state.brief = "test brief"
    state.skill_md = "test skill"

    loop = SupervisorLoop.__new__(SupervisorLoop)
    loop.task = state
    loop.workspace = Path(state.workspace)
    loop.workspace.mkdir(parents=True, exist_ok=True)
    loop.reviewer = _StubReviewer(reviewer_decision, reviewer_message)
    loop.runner = _StubRunner()
    loop._action_count = 0
    loop._review_pending = None
    loop._mcp_auth_requested = None
    loop._mid_stream_coaching = None
    loop._mid_stream_count = 0
    loop._streak_non_reviewed = 0
    loop._self_check_count = 0
    return loop


def _read_event(tool: str = "Read") -> ClaudeEvent:
    return ClaudeEvent(type="tool_use", tool_name=tool, tool_input={"file_path": "/tmp/x"})


def _bash_event() -> ClaudeEvent:
    return ClaudeEvent(type="tool_use", tool_name="Bash", tool_input={"command": "ls"})


def test_streak_triggers_one_self_check():
    print("\n--- Test 1: N read-only tool_uses → exactly one self-check ---")
    loop = _make_loop()

    for _ in range(SELF_CHECK_AFTER_N_NON_REVIEWED):
        loop._on_event(_read_event())

    # The Nth read should have triggered the self-check via drift_check (#82).
    assert len(loop.reviewer.calls) == 1, f"expected 1 self-check, got {len(loop.reviewer.calls)}"
    assert loop.reviewer.calls[0]["method"] == "drift_check", \
        "B4 must call Reviewer.drift_check, not review_action"
    assert loop._self_check_count == 1
    assert loop._streak_non_reviewed == 0, "streak must reset after firing"
    print(f"  ✓ self-check fired exactly once after {SELF_CHECK_AFTER_N_NON_REVIEWED} reads")
    print(f"  ✓ streak reset to 0 after firing")


def test_side_effecting_tool_resets_streak():
    print("\n--- Test 2: side-effecting tool resets streak ---")
    loop = _make_loop()

    # 3 reads (just under threshold of 4)
    for _ in range(SELF_CHECK_AFTER_N_NON_REVIEWED - 1):
        loop._on_event(_read_event())
    assert loop._streak_non_reviewed == SELF_CHECK_AFTER_N_NON_REVIEWED - 1

    # One Bash — per-action review fires (1 call), streak resets
    loop._on_event(_bash_event())
    assert loop._streak_non_reviewed == 0
    assert len(loop.reviewer.calls) == 1
    assert loop.reviewer.calls[0]["method"] == "review_action", \
        "Bash should call review_action (per-action gate), not drift_check"
    assert loop.reviewer.calls[0]["tool_name"] == "Bash"

    # Another 3 reads — still not enough; no NEW self-check
    for _ in range(SELF_CHECK_AFTER_N_NON_REVIEWED - 1):
        loop._on_event(_read_event())
    assert len(loop.reviewer.calls) == 1, "streak shouldn't have re-fired"
    assert loop._self_check_count == 0
    print("  ✓ Bash reset streak; 3+3 reads did NOT fire a self-check")


def test_self_check_budget_caps():
    print("\n--- Test 3: self-check count caps at MAX_SELF_CHECKS_PER_LOOP ---")
    loop = _make_loop()

    # Drive (cap + 2) full streaks of N reads each. Only `cap` should fire.
    streaks = MAX_SELF_CHECKS_PER_LOOP + 2
    for _ in range(streaks):
        for _ in range(SELF_CHECK_AFTER_N_NON_REVIEWED):
            loop._on_event(_read_event())

    assert loop._self_check_count == MAX_SELF_CHECKS_PER_LOOP, \
        f"expected cap at {MAX_SELF_CHECKS_PER_LOOP}, got {loop._self_check_count}"
    assert len(loop.reviewer.calls) == MAX_SELF_CHECKS_PER_LOOP
    print(f"  ✓ capped at {MAX_SELF_CHECKS_PER_LOOP} despite {streaks} streaks")


def test_self_check_correct_elevates_to_midstream():
    print("\n--- Test 4: self-check `correct` → B1 mid-stream coaching ---")
    loop = _make_loop(
        reviewer_decision="correct",
        reviewer_message="You've been reading the same file 4 times — try grep instead.",
    )

    for _ in range(SELF_CHECK_AFTER_N_NON_REVIEWED):
        loop._on_event(_read_event())

    assert loop._mid_stream_coaching is not None, \
        "self-check `correct` must set _mid_stream_coaching"
    assert loop._mid_stream_coaching["tool"] == "(self_check)"
    assert "grep" in loop._mid_stream_coaching["message"]
    assert loop.runner.interrupt_count == 1, \
        "self-check `correct` must call runner.interrupt"
    print("  ✓ _mid_stream_coaching populated with self_check tool")
    print("  ✓ runner.interrupt() called once")
    print("  ✓ B1 inner-loop will now spawn a new Claude with coaching prompt")


def test_self_check_correct_capped_falls_through_to_corrections():
    print("\n--- Test 5: self-check `correct` past mid-stream cap → corrections ---")
    loop = _make_loop(
        reviewer_decision="correct",
        reviewer_message="drift again",
    )
    # Pretend B1 already burned its mid-stream budget on prior coaches.
    loop._mid_stream_count = MAX_MID_STREAM_INTERRUPTS

    for _ in range(SELF_CHECK_AFTER_N_NON_REVIEWED):
        loop._on_event(_read_event())

    assert loop._mid_stream_coaching is None, \
        "cap was hit — should NOT set new coaching"
    assert loop.runner.interrupt_count == 0
    assert "drift again" in loop.task.corrections, \
        "post-cap correction should append to task.corrections"
    print("  ✓ capped: no new coaching, no interrupt; correction deferred to next loop")


def main():
    print("=" * 64)
    print("B4 SELF-CHECK — REGRESSION")
    print("=" * 64)
    test_streak_triggers_one_self_check()
    test_side_effecting_tool_resets_streak()
    test_self_check_budget_caps()
    test_self_check_correct_elevates_to_midstream()
    test_self_check_correct_capped_falls_through_to_corrections()
    print()
    print("=" * 64)
    print("B4 SELF-CHECK TEST: PASS ✓")
    print("=" * 64)


if __name__ == "__main__":
    main()
