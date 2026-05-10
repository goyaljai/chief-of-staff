"""D1 Lite regression test — file snapshot + /undo per task.

Validates:
  1. permission_hook._maybe_snapshot_for_undo records existing-file content.
  2. permission_hook records new-file with originally_existed=False.
  3. /undo restores overwrites and deletes created-from-scratch files.
  4. Truncated snapshots (oversized) are skipped, not corrupted.
  5. Files outside workspace are NOT snapshotted (security boundary).
  6. Multiple writes to the same path are deduped — only the FIRST snapshot
     (true original) is used at undo time.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config  # noqa: F401


def main():
    print("=" * 60)
    print("D1 LITE — UNDO REGRESSION TEST")
    print("=" * 60)

    from permission_hook import _maybe_snapshot_for_undo

    # 1. Set up a sandbox workspace
    ws = Path(tempfile.mkdtemp(prefix="d1_test_"))
    snap_file = ws / ".cos_snapshots.jsonl"

    # File that exists pre-task; Claude is about to overwrite it
    existing = ws / "config.json"
    existing.write_text('{"version": "v1"}')

    # File that does NOT exist; Claude is about to create it
    new_file = ws / "new_module.py"

    # 2. Simulate hook firing for two writes
    _maybe_snapshot_for_undo("Write", {"file_path": str(existing)}, str(ws))
    _maybe_snapshot_for_undo("Write", {"file_path": str(new_file)}, str(ws))

    # Now Claude actually performs the writes
    existing.write_text('{"version": "v2_BAD"}')
    new_file.write_text("def hallucinated(): pass")
    (ws / "subdir").mkdir(parents=True, exist_ok=True)
    nested_new = ws / "subdir" / "deep.txt"
    _maybe_snapshot_for_undo("Edit", {"file_path": str(nested_new)}, str(ws))
    nested_new.write_text("nested content")

    assert snap_file.exists(), "snapshots file must be created"
    lines = [json.loads(l) for l in snap_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 3, f"expected 3 snapshot entries; got {len(lines)}"
    print(f"  ✓ Hook recorded 3 snapshot entries (1 overwrite, 2 creates)")

    # 3. Simulate the /undo endpoint logic inline
    from main import undo_task as _undo_handler
    from task_store import STORE, TaskState

    tid = f"d1_test_{uuid.uuid4().hex[:6]}"
    state = TaskState(id=tid, goal="d1 test", clarifications={}, workspace=str(ws))
    STORE._tasks[tid] = state

    result = _undo_handler(tid)
    print(f"  ✓ /undo result: {result}")

    # 4. Verify state restored
    assert existing.read_text() == '{"version": "v1"}', \
        f"overwrite not restored: got {existing.read_text()!r}"
    assert not new_file.exists(), "newly-created file should have been deleted"
    assert not nested_new.exists(), "newly-created nested file should have been deleted"
    print("  ✓ Existing file restored to original; created files deleted")

    # 5. Boundary check — files outside workspace are NOT snapshotted
    snap_file.unlink()
    outside_path = "/tmp/_d1_outside_test.txt"
    Path(outside_path).write_text("untouched")
    _maybe_snapshot_for_undo("Write", {"file_path": outside_path}, str(ws))
    if snap_file.exists():
        recorded_paths = [json.loads(l).get("path") for l in snap_file.read_text().splitlines() if l.strip()]
        assert outside_path not in recorded_paths, \
            f"out-of-workspace path was snapshotted: {recorded_paths}"
    print("  ✓ Out-of-workspace path is rejected by snapshot guard")

    # 6. Truncated entries skipped, not corrupted
    snap_file.unlink(missing_ok=True)
    big_file = ws / "big.bin"
    big_file.write_bytes(b"x" * (300 * 1024))  # > 256KB cap
    _maybe_snapshot_for_undo("Write", {"file_path": str(big_file)}, str(ws))
    big_file.write_bytes(b"y" * 1024)  # Claude shrinks it
    entries = [json.loads(l) for l in snap_file.read_text().splitlines() if l.strip()]
    assert entries[0]["truncated"], "oversized file should be marked truncated"
    # Run undo — truncated entry should be skipped, not corrupt
    result = _undo_handler(tid)
    assert result["skipped"] >= 1
    assert big_file.read_bytes() == b"y" * 1024, "truncated file should be left alone"
    print("  ✓ Truncated snapshot skipped (file not corrupted by undo)")

    # 7. Dedupe: multiple snapshots for same path → only first applied
    snap_file.unlink(missing_ok=True)
    multi = ws / "multi.txt"
    multi.write_text("ORIGINAL")
    _maybe_snapshot_for_undo("Write", {"file_path": str(multi)}, str(ws))  # snapshot 1: ORIGINAL
    multi.write_text("INTERMEDIATE")
    _maybe_snapshot_for_undo("Edit", {"file_path": str(multi)}, str(ws))   # snapshot 2: INTERMEDIATE
    multi.write_text("FINAL_BAD")
    _undo_handler(tid)
    assert multi.read_text() == "ORIGINAL", \
        f"undo should restore the FIRST captured original, got {multi.read_text()!r}"
    print("  ✓ Repeated writes to same path deduped — true ORIGINAL restored")

    # cleanup
    shutil.rmtree(ws, ignore_errors=True)
    Path(outside_path).unlink(missing_ok=True)
    STORE._tasks.pop(tid, None)

    print()
    print("=" * 60)
    print("D1 LITE UNDO TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
