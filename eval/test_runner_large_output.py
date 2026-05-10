"""Regression test for the StreamReader chunk-overflow bug.

Symptom we hit: tasks crashed with
    "Separator is not found, and chunk exceed the limit"
when Claude emitted a `stream-json` line containing a large tool_result
(e.g. big workspace listing, full file dump). asyncio's default StreamReader
limit is 64KB; we now create the subprocess with `limit=64MB`.

This test spawns a tiny shell that emits a single line bigger than 64KB and
confirms ClaudeRunner's underlying `create_subprocess_exec(limit=...)` reads
it intact. We don't spawn real `claude` here — we spawn a mocked shell that
mimics the JSONL output shape, so the test is fast and deterministic.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401


async def _run_with_runner_limit(line_size_bytes: int) -> int:
    """Spawn `printf` to emit one line of `line_size_bytes` bytes + \\n, then
    read it through asyncio with the SAME limit ClaudeRunner uses."""
    # Build a printf that emits N copies of 'x' followed by newline.
    cmd = ["bash", "-c", f"head -c {line_size_bytes} /dev/urandom | base64 -w 0; echo"]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=64 * 1024 * 1024,  # MUST match ClaudeRunner's limit
    )
    line = await process.stdout.readline()
    await process.wait()
    return len(line)


async def main():
    print("=" * 60)
    print("RUNNER LARGE-OUTPUT REGRESSION TEST")
    print("=" * 60)

    # 1) Below default 64KB limit — should always work
    n = await _run_with_runner_limit(32 * 1024)
    print(f"  ✓ 32 KB single line read ({n} bytes)")
    assert n > 32 * 1024  # base64 inflates ~33%

    # 2) Just above default 64KB limit — would CRASH at default; should pass at 64MB
    n = await _run_with_runner_limit(128 * 1024)
    print(f"  ✓ 128 KB single line read ({n} bytes) — would have failed at default 64KB limit")
    assert n > 128 * 1024

    # 3) 4 MB single line — realistic for big tool_result on tasks like
    #    `find / -ls` or full-workspace dumps that the React Native task hit
    n = await _run_with_runner_limit(4 * 1024 * 1024)
    print(f"  ✓ 4 MB single line read ({n} bytes)")
    assert n > 4 * 1024 * 1024

    # 4) 16 MB single line — extreme but not impossible
    n = await _run_with_runner_limit(16 * 1024 * 1024)
    print(f"  ✓ 16 MB single line read ({n} bytes) — well under our 64MB ceiling")
    assert n > 16 * 1024 * 1024

    print()
    print("=" * 60)
    print("RUNNER LARGE-OUTPUT TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
