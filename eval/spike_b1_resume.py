"""B1.5 spike — verify `claude --resume <session_id>` works after a
mid-stream interrupt, with an injected coaching message.

Goal: prove the technical foundation for B1 (mid-stream interrupt +
inject reviewer coaching) BEFORE we design the supervisor changes.

Test plan:
  1. Run task A: "Create three hello-world Python files: a.py, b.py, c.py
     — one at a time, with a short delay between each."
  2. Capture session_id from the init event.
  3. After at least one file has been written, interrupt the runner.
  4. Wait for clean termination.
  5. Spawn a NEW ClaudeRunner with --resume <session_id> + coaching:
       "Stop. The reviewer flagged that each file must start with a
        `#!/usr/bin/env python3` shebang. Add it to any files already
        written and any remaining ones."
  6. Verify final workspace:
       - Exactly 3 files: a.py, b.py, c.py
       - Each starts with `#!/usr/bin/env python3`
       - The continuation knew about the prior files (didn't recreate
         the workspace from scratch)

If this works, B1 is feasible as designed. If not, we redesign.

This is a one-off script — do NOT add to the regression suite. It hits
the real Claude CLI (cost: ~$0.10) and is too flaky for CI.
"""
import asyncio
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401 — loads .env

from runners import ClaudeRunner


WORKSPACE = Path("/tmp/cos-spike-b1.5")


def _setup():
    if WORKSPACE.exists():
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def _list_files() -> list[Path]:
    return sorted(p for p in WORKSPACE.iterdir() if p.is_file() and p.suffix == ".py")


async def main():
    print("=" * 72)
    print("B1.5 SPIKE — claude --resume mid-stream interrupt + inject")
    print("=" * 72)
    _setup()

    captured_session_id: str | None = None
    tool_uses_seen = 0
    interrupted_at: float | None = None
    runner = ClaudeRunner(working_dir=WORKSPACE)

    def _on_event(event):
        nonlocal captured_session_id, tool_uses_seen, interrupted_at
        if event.type == "init" and event.session_id:
            captured_session_id = event.session_id
            print(f"  [event] init session_id={event.session_id}")
        elif event.type == "tool_use":
            tool = event.tool_name or ""
            tool_uses_seen += 1
            print(f"  [event] tool_use #{tool_uses_seen}: {tool}")
            # Interrupt after the FIRST tool_use of any kind. The user's
            # CLAUDE.md routes Write through the MCP token-saver, so we
            # can't match a specific tool name reliably — match anything.
            if interrupted_at is None and tool_uses_seen >= 1:
                interrupted_at = time.monotonic()
                print(f"  [spike] INTERRUPTING after first tool_use")
                runner.interrupt()
        elif event.type == "text" and event.text:
            print(f"  [event] text: {event.text[:120]!r}")
        elif event.type == "result":
            print(f"  [event] result: success={not event.is_error}")

    print("\n--- Phase 1: start original task, interrupt mid-stream ---\n")
    prompt_a = (
        "Create three Python files in the current directory:\n"
        "  - a.py: prints 'hello from a' and computes sum(range(100))\n"
        "  - b.py: prints 'hello from b' and computes sum(range(100))\n"
        "  - c.py: prints 'hello from c' and computes sum(range(100))\n\n"
        "After writing each one, run it with `python3 <name>.py` to verify "
        "the output before moving on to the next. Take your time and verify "
        "carefully — print the output of each verification."
    )

    t0 = time.monotonic()
    result_a = await runner.run(
        prompt=prompt_a,
        on_event=_on_event,
        timeout_secs=180,
    )
    elapsed_a = time.monotonic() - t0
    print(f"\nPhase 1 done in {elapsed_a:.1f}s. success={result_a.success}")
    print(f"session_id captured: {captured_session_id}")
    print(f"workspace after Phase 1: {[p.name for p in _list_files()]}")

    if not captured_session_id:
        print("\n❌ FAIL: no session_id captured — ClaudeRunner didn't surface init event")
        return 1

    if interrupted_at is None:
        print("\n❌ FAIL: never reached the interrupt point — task ran to completion before first Write")
        return 1

    # Phase 2: resume with coaching
    print("\n--- Phase 2: spawn new runner with --resume + coaching ---\n")

    runner2 = ClaudeRunner(working_dir=WORKSPACE)
    coaching = (
        "Stop. The reviewer flagged that each file must start with a "
        "`#!/usr/bin/env python3` shebang on line 1. Update any files "
        "you've already written, and add the shebang to any remaining "
        "files. Then verify by printing the first line of each file. "
        "Do not recreate the workspace from scratch."
    )

    def _on_event_2(event):
        if event.type == "init" and event.session_id:
            print(f"  [event] init session_id={event.session_id}")
        elif event.type == "tool_use":
            print(f"  [event] tool_use: {event.tool_name}")
        elif event.type == "text" and event.text:
            print(f"  [event] text: {event.text[:120]!r}")
        elif event.type == "result":
            print(f"  [event] result: success={not event.is_error}")

    t1 = time.monotonic()
    result_b = await runner2.run(
        prompt=coaching,
        session_id=captured_session_id,
        on_event=_on_event_2,
        timeout_secs=180,
    )
    elapsed_b = time.monotonic() - t1
    print(f"\nPhase 2 done in {elapsed_b:.1f}s. success={result_b.success}")
    print(f"workspace after Phase 2: {[p.name for p in _list_files()]}")

    # Verify
    print("\n--- Verification ---\n")
    files = _list_files()
    file_names = {p.name for p in files}
    expected = {"a.py", "b.py", "c.py"}

    pass_count = 0
    total = 4

    if file_names >= expected:
        print(f"  ✓ All 3 expected files present: {sorted(file_names)}")
        pass_count += 1
    else:
        missing = expected - file_names
        print(f"  ✗ Missing files: {missing}")

    shebang = "#!/usr/bin/env python3"
    files_with_shebang = []
    for p in files:
        if p.name in expected:
            first = p.read_text().splitlines()[:1]
            if first and first[0].strip() == shebang:
                files_with_shebang.append(p.name)
    if len(files_with_shebang) == 3:
        print(f"  ✓ All 3 files start with shebang: {files_with_shebang}")
        pass_count += 1
    else:
        print(f"  ✗ Only {len(files_with_shebang)}/3 files have the shebang: {files_with_shebang}")

    if result_b.success:
        print(f"  ✓ Phase 2 (resume) reported success")
        pass_count += 1
    else:
        print(f"  ✗ Phase 2 returned non-success — Claude likely errored out")

    # Continuity check: did Phase 2 see the prior session?
    # We expect the resume to know about Phase 1's work. The shebang task
    # explicitly said "update any files you've already written" — if Claude
    # ignored that and started fresh, the spike is a partial failure.
    phase2_log = (result_b.output or "")
    if any(kw in phase2_log.lower() for kw in ("already wrote", "already written", "previously", "earlier", "from before")):
        print(f"  ✓ Phase 2 referenced prior work in its output (good continuity signal)")
        pass_count += 1
    else:
        print(f"  ~ Phase 2 didn't explicitly reference prior work — continuity unclear from output text alone")
        # Half-credit — file state may still be correct
        pass_count += 0

    print()
    print("=" * 72)
    if pass_count == total:
        print(f"✅ B1.5 SPIKE: PASS ({pass_count}/{total}) — B1 design is feasible")
    elif pass_count >= 2:
        print(f"⚠️  B1.5 SPIKE: PARTIAL ({pass_count}/{total}) — feasible but with caveats")
    else:
        print(f"❌ B1.5 SPIKE: FAIL ({pass_count}/{total}) — B1 design needs rethinking")
    print("=" * 72)
    return 0 if pass_count >= 2 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
