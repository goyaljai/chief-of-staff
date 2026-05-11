"""B1.6 regression — agnostic tool-review classifier.

Validates services.tool_review.is_side_effecting works for:
  - native Claude tools (Bash, Write, MultiEdit, Read, Grep, …)
  - the developer's token-saver MCP (write_file, edit_file, bash_compressed, …)
  - third-party MCPs we've never seen
  - explicit non-reviewable carve-out (TodoWrite)
  - REVIEW_EXTRA_VERBS env override
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    print("=" * 60)
    print("B1.6 TOOL-REVIEW CLASSIFIER TEST")
    print("=" * 60)

    from services.tool_review import (
        is_side_effecting, tokenize, classify_announced_tools,
    )

    # 1. Tokenizer correctness — covers the three boundary types
    cases = {
        "Bash": ["bash"],
        "MultiEdit": ["multi", "edit"],
        "WebFetch": ["web", "fetch"],
        "mcp__glance-token-saver__write_file":
            ["mcp", "glance", "token", "saver", "write", "file"],
        "mcp__plugin_exa_exa__authenticate":
            ["mcp", "plugin", "exa", "exa", "authenticate"],
        "snake_case_thing": ["snake", "case", "thing"],
    }
    for name, expected in cases.items():
        got = tokenize(name)
        assert got == expected, f"{name}: expected {expected}, got {got}"
    print(f"  ✓ tokenize() correct on {len(cases)} cases")

    # 2. Native side-effecting tools must be reviewed
    for t in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert is_side_effecting(t), f"{t} should be reviewed"
    print("  ✓ native side-effecting tools reviewed (Bash/Write/Edit/MultiEdit/NotebookEdit)")

    # 3. Native read-only tools must NOT be reviewed
    for t in ("Read", "Grep", "Glob", "WebFetch", "WebSearch", "Task"):
        assert not is_side_effecting(t), f"{t} should NOT be reviewed"
    print("  ✓ native read-only tools skipped (Read/Grep/Glob/WebFetch/WebSearch/Task)")

    # 4. Developer's token-saver MCP — the case that motivated this whole change
    saver_review = [
        "mcp__glance-token-saver__write_file",
        "mcp__glance-token-saver__edit_file",
        "mcp__glance-token-saver__bash_compressed",
    ]
    saver_skip = [
        "mcp__glance-token-saver__read_compressed",
        "mcp__glance-token-saver__grep_compressed",
    ]
    for t in saver_review:
        assert is_side_effecting(t), f"{t} should be reviewed"
    for t in saver_skip:
        assert not is_side_effecting(t), f"{t} should NOT be reviewed"
    print("  ✓ token-saver MCP: write_file/edit_file/bash_compressed reviewed; read/grep skipped")

    # 5. Third-party MCPs we've never seen — common verb patterns
    third_party_review = [
        "mcp__slack__post_message",
        "mcp__github__create_pull_request",
        "mcp__k8s__apply_manifest",  # via REVIEW_EXTRA_VERBS in test 7
        "mcp__db__truncate_table",
        "mcp__deploy__publish_artifact",
        "mcp__shell__run_command",
    ]
    third_party_skip = [
        "mcp__readonly__list_pods",
        "mcp__readonly__get_status",
        "mcp__exa__search",
    ]
    for t in third_party_review:
        if "apply" in t:
            continue  # exercised in test 7
        assert is_side_effecting(t), f"{t} should be reviewed"
    for t in third_party_skip:
        assert not is_side_effecting(t), f"{t} should NOT be reviewed"
    print("  ✓ third-party MCPs classified by verb tokens (Slack/GitHub/db/deploy/shell)")

    # 6. TodoWrite explicit carve-out — would match "write" but is plan tracking
    assert not is_side_effecting("TodoWrite"), "TodoWrite must be in NON_REVIEWABLE"
    assert is_side_effecting("Write"), "Write must still be reviewed"
    print("  ✓ TodoWrite carve-out: not reviewed; bare Write still reviewed")

    # 7. REVIEW_EXTRA_VERBS env override — adds 'apply' to verb set on reload
    import importlib

    import services.tool_review as tr_mod
    os.environ["REVIEW_EXTRA_VERBS"] = "apply,promote"
    importlib.reload(tr_mod)
    assert tr_mod.is_side_effecting("mcp__k8s__apply_manifest"), \
        "REVIEW_EXTRA_VERBS=apply should make k8s apply reviewable"
    assert tr_mod.is_side_effecting("mcp__release__promote_build"), \
        "REVIEW_EXTRA_VERBS=promote should make promote_build reviewable"
    # Non-verb tools still skipped
    assert not tr_mod.is_side_effecting("mcp__readonly__list_pods")
    print("  ✓ REVIEW_EXTRA_VERBS env override: apply/promote added to verb set")

    # cleanup
    del os.environ["REVIEW_EXTRA_VERBS"]
    importlib.reload(tr_mod)

    # 8. classify_announced_tools partitions a real init.tools payload
    real_tools = [
        "Bash", "Edit", "Read", "Grep", "Glob", "Write", "WebFetch", "WebSearch",
        "TodoWrite", "Task",
        "mcp__glance-token-saver__write_file",
        "mcp__glance-token-saver__read_compressed",
        "mcp__glance-token-saver__bash_compressed",
        "mcp__plugin_exa_exa__authenticate",
    ]
    review, skip = tr_mod.classify_announced_tools(real_tools)
    assert "Bash" in review and "Write" in review and "Edit" in review
    assert "mcp__glance-token-saver__write_file" in review
    assert "mcp__glance-token-saver__bash_compressed" in review
    assert "Read" in skip and "Grep" in skip and "Glob" in skip
    assert "mcp__glance-token-saver__read_compressed" in skip
    assert "TodoWrite" in skip
    assert "mcp__plugin_exa_exa__authenticate" in skip
    print(f"  ✓ classify_announced_tools: {len(review)} review, {len(skip)} skip on real init payload")

    # 9. Empty / None input must not crash
    assert is_side_effecting(None) is False
    assert is_side_effecting("") is False
    assert tokenize("") == []
    print("  ✓ None/empty input handled")

    print()
    print("=" * 60)
    print("B1.6 TOOL-REVIEW CLASSIFIER TEST: PASS ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
