"""Per-task Claude Code hook installer.

Writes <workspace>/.claude/settings.json with PreToolUse hooks that
shell out to permission_hook.py for every Bash / Write / Edit /
MultiEdit / mcp__* call BEFORE Claude executes them. The hook script
enforces workspace-escape protection, snapshots files pre-mutation
(D1-Lite), and logs the decision.

Why per-task and not global: the hook needs to know which workspace
this task is anchored at and where to write the snapshot log. A global
config would either pin one workspace forever or require dynamic
rewriting on every task — both worse than per-task isolation in the
.claude/ directory the runner already creates.
"""
import json
from pathlib import Path

from config import HOOK_SCRIPT


def install_hooks(workspace: Path, hook_log_path: Path) -> dict:
    """Drop a fresh .claude/settings.json into the workspace pointing the
    PreToolUse hook at our permission_hook.py script. Returns the
    settings dict so callers (or tests) can inspect what was written."""
    settings_dir = workspace / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"

    hook_command = (
        f"SUPERVISOR_HOOK_LOG={hook_log_path} "
        f"SUPERVISOR_WORKSPACE={workspace} "
        f"python3 {HOOK_SCRIPT}"
    )
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash|Write|Edit|MultiEdit",
                    "hooks": [{"type": "command", "command": hook_command}],
                },
                {
                    "matcher": "mcp__.*",
                    "hooks": [{"type": "command", "command": hook_command}],
                },
            ]
        }
    }
    settings_path.write_text(json.dumps(settings, indent=2))
    return settings
