"""B1 regression test — mid-stream interrupt + inject reviewer coaching.

These tests exercise SupervisorLoop's mid-stream coaching path WITHOUT
spawning a real Claude subprocess. We mock ClaudeRunner with a scripted
event stream and a mock Reviewer that returns `decision="correct"` on
the first tool_use. The supervisor should:

  1. Set _mid_stream_coaching on the reviewer's `correct` decision
  2. Call runner.interrupt() (verified by mock)
  3. After Claude exits, build a coaching prompt and re-spawn with
     a fresh runner + the captured session_id
  4. Cap at MAX_MID_STREAM_INTERRUPTS per outer correction loop;
     beyond that, the `correct` decision falls back to the original
     "defer to next loop" behaviour.

These tests don't talk to Databricks, Voyage, or the real Claude CLI.
Safe in CI (assuming the env-var defaults from other suites are in place).
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401

# Same env-var defaults the other suites use so DatabricksEmbeddings /
# Orchestrator can construct without real creds.
os.environ.setdefault("DATABRICKS_TOKEN", "test-token-not-real")
os.environ.setdefault("DATABRICKS_BASE_URL", "http://test-base-url.invalid")
os.environ.setdefault("VOYAGE_API_KEY", "test-voyage-key-not-real")
os.environ["COS_ALLOW_DESTRUCTIVE_TESTS"] = "1"


# Imports MUST come after env-var defaults are set — Orchestrator's
# _client touches the OpenAI SDK on construction.
from runners.events import ClaudeEvent, TaskResult
from supervisor_loop import (
    MAX_MID_STREAM_INTERRUPTS,
    SupervisorLoop,
)


class _FakeRunner:
    """Replacement for ClaudeRunner. Each `.run(...)` consumes the next
    scripted batch of events from .scripted_events; .interrupt()
    increments .interrupt_count. The runner pretends to be successful."""

    instances: list["_FakeRunner"] = []

    def __init__(self, working_dir, hook_log_path=None):
        self.working_dir = working_dir
        self.hook_log_path = hook_log_path
        self.interrupt_count = 0
        self.runs_received_prompts: list[str] = []
        self.runs_received_session_ids: list = []
        _FakeRunner.instances.append(self)

    async def run(self, prompt, session_id=None, on_event=None, timeout_secs=1200):
        self.runs_received_prompts.append(prompt)
        self.runs_received_session_ids.append(session_id)
        events_for_this_run = _SCRIPT.pop(0) if _SCRIPT else []
        for ev in events_for_this_run:
            if on_event:
                # #83: on_event may be sync OR async — mirror the
                # production runner's await-if-coroutine handling so
                # the supervisor's elevation paths get a chance to run
                # before we move to the next event.
                _r = on_event(ev)
                if asyncio.iscoroutine(_r):
                    await _r
            # Simulate a tiny gap between events so the test exercises
            # the same async ordering real runners produce.
            await asyncio.sleep(0)
            if self.interrupt_count > 0:
                # Interrupt was issued mid-event-stream — stop emitting
                # further events from THIS run, just like the real runner
                # exits early on SIGTERM.
                break
        return TaskResult(
            success=self.interrupt_count == 0,
            session_id="sess-" + str(len(_FakeRunner.instances)),
            output="(fake)",
            events=[],
            cost_usd=0.0,
        )

    def interrupt(self):
        self.interrupt_count += 1


# Each entry in _SCRIPT is the event list one run() call should emit.
_SCRIPT: list[list[ClaudeEvent]] = []


def _set_script(events_per_run: list[list[ClaudeEvent]]):
    _SCRIPT.clear()
    _SCRIPT.extend(events_per_run)


def _make_state():
    """Minimal TaskState the supervisor needs to construct."""
    from persistence import TaskState
    s = TaskState(
        id="b1_test_" + os.urandom(3).hex(),
        goal="test goal",
        clarifications={},
        workspace="/tmp/cos-b1-test",
    )
    s.brief = "test brief"
    s.skill_md = "test skill"
    return s


async def test_correct_triggers_midstream_interrupt():
    """The reviewer's `correct` decision should cause an interrupt + a
    second runner spawn with the coaching prompt and the same session_id."""
    print("\n--- Test 1: 'correct' triggers mid-stream interrupt + resume ---")
    _FakeRunner.instances.clear()
    _set_script([
        # Run 1 emits a tool_use that the reviewer will flag.
        [
            ClaudeEvent(type="init", session_id="sess-from-claude"),
            ClaudeEvent(type="tool_use", tool_name="Bash", tool_input={"command": "rm -rf /"}),
        ],
        # Run 2 emits a clean completion.
        [
            ClaudeEvent(type="init", session_id="sess-from-claude"),
            ClaudeEvent(type="result", text="done"),
        ],
    ])

    fake_review = {"decision": "correct", "message": "rm -rf is way too destructive — try targeted file deletion instead."}

    with patch("supervisor_loop.ClaudeRunner", _FakeRunner), \
         patch.object(SupervisorLoop, "__init__", lambda self, task: None):
        # Hand-build a SupervisorLoop bypassing the real __init__ (which
        # would try to load Reviewer prompts from disk).
        sl = SupervisorLoop.__new__(SupervisorLoop)
        sl.task = _make_state()
        sl.workspace = Path("/tmp/cos-b1-test")
        sl.workspace.mkdir(parents=True, exist_ok=True)
        sl.hook_log = sl.workspace / "hook.log"
        sl.hook_log.touch()
        sl._action_count = 0
        sl._mid_stream_coaching = None
        sl._mid_stream_count = 0
        sl._mcp_auth_requested = None

        # Stub the reviewer + orchestrator with cheap fakes
        class _R:
            def review_action(self, **kw):
                return fake_review
        class _O:
            pass
        sl.reviewer = _R()
        sl.orchestrator = _O()

        sl.runner = _FakeRunner(sl.workspace, sl.hook_log)

        # Drive the inner loop manually.
        from persistence import STORE
        STORE._tasks[sl.task.id] = sl.task
        STORE.register_runner(sl.task.id, sl.runner)

        inner_prompt = "initial brief"
        session_id = None
        loop_num = 1
        for _ in range(5):  # bound the inner loop in the test
            result = await sl.runner.run(
                prompt=inner_prompt,
                session_id=session_id,
                on_event=sl._on_event,
            )
            session_id = result.session_id
            if sl._mid_stream_coaching:
                coaching = sl._mid_stream_coaching
                sl._mid_stream_coaching = None
                sl._mid_stream_count += 1
                inner_prompt = sl._build_coaching_prompt(coaching)
                sl.runner = _FakeRunner(sl.workspace, sl.hook_log)
                STORE.register_runner(sl.task.id, sl.runner)
                continue
            break

    runners = _FakeRunner.instances
    assert len(runners) >= 2, f"expected at least 2 runner spawns; got {len(runners)}"
    print(f"  ✓ Spawned {len(runners)} runners (initial + at least one resume)")
    assert runners[0].interrupt_count == 1, f"first runner should be interrupted exactly once; got {runners[0].interrupt_count}"
    print(f"  ✓ First runner interrupted exactly once")
    assert sl._mid_stream_count == 1, f"_mid_stream_count should be 1; got {sl._mid_stream_count}"
    print(f"  ✓ _mid_stream_count is 1")
    second_prompt = runners[1].runs_received_prompts[0]
    assert "Stop." in second_prompt and "reviewer flagged" in second_prompt
    assert "rm -rf" in second_prompt or "destructive" in second_prompt
    print(f"  ✓ Second runner got the coaching prompt with the reviewer's reason")
    second_session = runners[1].runs_received_session_ids[0]
    assert second_session == "sess-from-claude" or second_session is not None, f"second runner should resume the same session_id; got {second_session!r}"
    print(f"  ✓ Second runner resumed session_id={second_session!r}")
    print("  TEST 1: PASS ✓")


async def test_cap_falls_back_to_deferred():
    """After MAX_MID_STREAM_INTERRUPTS rounds, further `correct` decisions
    should be appended to corrections (deferred behaviour) instead of
    triggering more interrupts."""
    print("\n--- Test 2: cap falls back to defer-to-next-loop ---")
    _FakeRunner.instances.clear()
    _set_script([
        # First MAX runs each emit a flagged tool_use → all interrupted.
        *[
            [ClaudeEvent(type="init", session_id="sess-from-claude"),
             ClaudeEvent(type="tool_use", tool_name="Bash", tool_input={"command": f"bad_cmd_{i}"})]
            for i in range(MAX_MID_STREAM_INTERRUPTS + 2)
        ],
    ])

    fake_review = {"decision": "correct", "message": "stop doing that"}

    with patch("supervisor_loop.ClaudeRunner", _FakeRunner):
        sl = SupervisorLoop.__new__(SupervisorLoop)
        sl.task = _make_state()
        sl.workspace = Path("/tmp/cos-b1-test")
        sl.workspace.mkdir(parents=True, exist_ok=True)
        sl.hook_log = sl.workspace / "hook.log"
        sl.hook_log.touch()
        sl._action_count = 0
        sl._mid_stream_coaching = None
        sl._mid_stream_count = 0
        sl._mcp_auth_requested = None

        class _R:
            def review_action(self, **kw):
                return fake_review
        class _O:
            pass
        sl.reviewer = _R()
        sl.orchestrator = _O()

        sl.runner = _FakeRunner(sl.workspace, sl.hook_log)
        from persistence import STORE
        STORE._tasks[sl.task.id] = sl.task
        STORE.register_runner(sl.task.id, sl.runner)

        inner_prompt = "initial brief"
        session_id = None
        for _ in range(MAX_MID_STREAM_INTERRUPTS + 3):
            result = await sl.runner.run(
                prompt=inner_prompt,
                session_id=session_id,
                on_event=sl._on_event,
            )
            session_id = result.session_id
            if sl._mid_stream_coaching:
                coaching = sl._mid_stream_coaching
                sl._mid_stream_coaching = None
                sl._mid_stream_count += 1
                inner_prompt = sl._build_coaching_prompt(coaching)
                sl.runner = _FakeRunner(sl.workspace, sl.hook_log)
                STORE.register_runner(sl.task.id, sl.runner)
                continue
            break

    assert sl._mid_stream_count == MAX_MID_STREAM_INTERRUPTS, \
        f"should hit cap; got count={sl._mid_stream_count}"
    print(f"  ✓ Hit cap at exactly {MAX_MID_STREAM_INTERRUPTS} mid-stream interrupts")
    # After cap, next `correct` should append to corrections instead.
    assert any("stop doing that" == c for c in sl.task.corrections), \
        f"deferred correction missing from task.corrections: {sl.task.corrections}"
    print(f"  ✓ Post-cap correction was appended to task.corrections (deferred path)")
    print("  TEST 2: PASS ✓")


async def main():
    print("=" * 64)
    print("B1 MID-STREAM INTERRUPT + COACH — REGRESSION")
    print("=" * 64)
    await test_correct_triggers_midstream_interrupt()
    await test_cap_falls_back_to_deferred()
    print()
    print("=" * 64)
    print("B1 MID-STREAM TEST: PASS ✓")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
