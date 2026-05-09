import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from config import ALLOWED_TOOLS, HOOK_SCRIPT


@dataclass
class ClaudeEvent:
    type: str
    tool_name: str | None = None
    tool_input: dict = field(default_factory=dict)
    tool_output: str | None = None
    text: str | None = None
    session_id: str | None = None
    is_error: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class TaskResult:
    success: bool
    session_id: str | None
    output: str
    events: list[ClaudeEvent]
    cost_usd: float | None = None


def install_hooks(workspace: Path, hook_log_path: Path) -> dict:
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


class ClaudeRunner:
    """Headless Claude Code via the `claude` CLI. Streams events.
    Supports interrupt() and --resume."""

    def __init__(self, working_dir: Path, hook_log_path: Path | None = None):
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)
        self.hook_log_path = hook_log_path or (self.working_dir / "hook_log.jsonl")
        install_hooks(self.working_dir, self.hook_log_path)
        self._process: asyncio.subprocess.Process | None = None

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
        on_event: Callable[[ClaudeEvent], None] | None = None,
        timeout_secs: int = 1200,
    ) -> TaskResult:
        cmd = self._build_command(prompt, session_id)
        events: list[ClaudeEvent] = []
        output_lines: list[str] = []
        final_session_id = session_id
        cost_usd = None

        env = os.environ.copy()
        env["SUPERVISOR_HOOK_LOG"] = str(self.hook_log_path)
        env["SUPERVISOR_WORKSPACE"] = str(self.working_dir)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.working_dir),
            env=env,
        )
        self._process = process

        deadline = asyncio.get_event_loop().time() + timeout_secs

        while True:
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=max(1.0, deadline - asyncio.get_event_loop().time()),
                )
            except asyncio.TimeoutError:
                print(f"[runner] timeout after {timeout_secs}s, terminating Claude")
                self.interrupt()
                break
            if not line:
                break
            raw_line = line.decode(errors="replace").strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            event = self._parse_event(data)
            if event is None:
                continue
            if event.session_id:
                final_session_id = event.session_id
            events.append(event)
            if event.type == "result":
                output_lines.append(event.text or "")
                if "cost_usd" in data:
                    cost_usd = data["cost_usd"]
            if on_event:
                try:
                    on_event(event)
                except Exception as e:
                    print(f"[runner] on_event raised: {e}")

        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.interrupt()
            await process.wait()
        success = process.returncode == 0

        return TaskResult(
            success=success,
            session_id=final_session_id,
            output="\n".join(output_lines),
            events=events,
            cost_usd=cost_usd,
        )

    def interrupt(self):
        if self._process and self._process.returncode is None:
            self._process.terminate()

    def _build_command(self, prompt: str, session_id: str | None) -> list[str]:
        tools_str = ",".join(ALLOWED_TOOLS)
        cmd = [
            "claude",
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", tools_str,
            "--permission-mode", "acceptEdits",
            "-p", prompt,
        ]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def _parse_event(self, data: dict) -> ClaudeEvent | None:
        event_type = data.get("type", "")

        if event_type == "system" and data.get("subtype") == "init":
            return ClaudeEvent(type="init", session_id=data.get("session_id"), raw=data)

        if event_type == "assistant":
            message = data.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "tool_use":
                    return ClaudeEvent(
                        type="tool_use",
                        tool_name=block.get("name"),
                        tool_input=block.get("input", {}),
                        session_id=data.get("session_id"),
                        raw=data,
                    )
                if block.get("type") == "text":
                    return ClaudeEvent(
                        type="text",
                        text=block.get("text"),
                        session_id=data.get("session_id"),
                        raw=data,
                    )

        if event_type == "user":
            message = data.get("message", {})
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return ClaudeEvent(
                        type="tool_result",
                        tool_output=str(block.get("content", "")),
                        is_error=block.get("is_error", False),
                        session_id=data.get("session_id"),
                        raw=data,
                    )

        if event_type == "result":
            return ClaudeEvent(
                type="result",
                text=data.get("result", ""),
                is_error=data.get("subtype") == "error",
                session_id=data.get("session_id"),
                raw=data,
            )

        return None
