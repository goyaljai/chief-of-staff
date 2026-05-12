"""ClaudeRunner — headless Claude Code subprocess wrapper.

Spawns `claude --output-format stream-json` against the per-task
workspace, parses every line into a ClaudeEvent, optionally forwards
each event to a caller-supplied callback (used by the supervisor to
stream into SSE / STORE), and returns a TaskResult at the end.

Why headless: the supervisor needs to OBSERVE every action Claude
takes (so it can review them, log them, surface them to the user),
which the interactive Claude CLI doesn't make easy. The stream-json
output format is structured enough to drive everything we need.

Notable internals:
  • 64MB StreamReader limit — `--output-format stream-json` lines can
    contain large tool_result chunks (workspace listings, big stdout).
    The default 64KB limit silently kills the run with "Separator not
    found" mid-task. See run().
  • interrupt() does SIGTERM + 5s SIGKILL escalation (round-3 fix #11)
    — prevents zombie Claude subprocesses when the CLI is stuck in a
    heavy build / hung syscall.
  • `--resume <session_id>` is supported; pass `session_id` to run()
    to continue a previous Claude conversation instead of starting fresh.
"""
import asyncio
import json
import os
from pathlib import Path
from typing import Awaitable, Callable

from config import ALLOWED_TOOLS

from .events import ClaudeEvent, TaskResult
from .hooks import install_hooks


# Claude Opus 4.7 list pricing as of 2026-05. Update if model rates
# change. Cache-read is heavily discounted vs. fresh input. The
# computed value is an estimate; the authoritative source remains
# `total_cost_usd` when the CLI emits it.
_USD_PER_MTOK_INPUT = 5.00
_USD_PER_MTOK_OUTPUT = 25.00
_USD_PER_MTOK_CACHE_WRITE = 6.25  # 1.25x input
_USD_PER_MTOK_CACHE_READ = 0.50   # 0.1x input


def _compute_usd_from_usage(usage: dict) -> float | None:
    """Estimate Claude API spend from a usage block (Claude Code 2.x
    final result event). Returns None on missing/malformed input so the
    caller can leave cost_usd unset rather than logging $0.

    Why we need this: older Claude CLIs emitted `cost_usd` directly;
    newer ones (2.x) only emit token counts in a `usage` dict. Without
    this conversion, tasks.cost_claude_usd stays $0 and the executor
    side of cost is invisible — exactly what surfaced post-Phase-4.
    """
    if not isinstance(usage, dict):
        return None
    try:
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        cache_w = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_r = int(usage.get("cache_read_input_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    if in_tok + out_tok + cache_w + cache_r == 0:
        return None
    usd = (
        (in_tok / 1_000_000) * _USD_PER_MTOK_INPUT
        + (out_tok / 1_000_000) * _USD_PER_MTOK_OUTPUT
        + (cache_w / 1_000_000) * _USD_PER_MTOK_CACHE_WRITE
        + (cache_r / 1_000_000) * _USD_PER_MTOK_CACHE_READ
    )
    return round(usd, 4)


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
        on_event: Callable[[ClaudeEvent], "None | Awaitable[None]"] | None = None,
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

        # V3.5 fix: default asyncio StreamReader limit is 64KB, which fails
        # on `--output-format stream-json` lines that contain large
        # tool_result chunks (e.g. workspace listings, big stdout). Bump to
        # 64MB so single JSONL events of any reasonable size are read
        # intact. Without this, we got "Separator is not found, and chunk
        # exceed the limit" mid-task and the run died.
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.working_dir),
            env=env,
            limit=64 * 1024 * 1024,
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
                # Bug fix (post-Phase-4 audit): older Claude CLIs emit
                # `cost_usd` directly; Claude Code 2.x emits
                # `total_cost_usd` and/or a `usage` block with token
                # counts. We were ONLY checking the legacy field, so
                # production capture was always None and tasks.
                # cost_claude_usd stayed $0 — making executor cost
                # invisible in observability. Try fields in order:
                # total_cost_usd → cost_usd → compute from usage tokens.
                if "total_cost_usd" in data and data["total_cost_usd"] is not None:
                    cost_usd = float(data["total_cost_usd"])
                elif "cost_usd" in data and data["cost_usd"] is not None:
                    cost_usd = float(data["cost_usd"])
                else:
                    usage = data.get("usage") or {}
                    if isinstance(usage, dict) and usage:
                        cost_usd = _compute_usd_from_usage(usage)
            if on_event:
                try:
                    # #83: support async on_event handlers. The supervisor's
                    # _on_event hops reviewer LLM calls onto a thread via
                    # run_in_executor so this drain loop isn't blocked
                    # waiting for Databricks (which makes Claude's stdout
                    # OS pipe fill ~64KB and Claude pauses writing).
                    result = on_event(event)
                    if asyncio.iscoroutine(result):
                        await result
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
        """Round-3 fix #11: SIGTERM, then escalate to SIGKILL in 5s if the
        process still hasn't exited. Prevents zombie Claude subprocesses
        when the CLI is stuck in a heavy build / hung syscall and ignores
        SIGTERM. Best-effort — works whether or not we're inside an
        event loop."""
        if not self._process or self._process.returncode is not None:
            return
        try:
            self._process.terminate()
        except ProcessLookupError:
            return
        # Schedule the escalation. If we're inside a running event loop,
        # fire an async timer; otherwise rely on the caller's `wait()` +
        # 10s timeout in run() that already escalates via interrupt()
        # recursion (idempotent).
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(5.0, self._escalate_kill)
        except RuntimeError:
            pass  # no loop running — sync context, deferred to run()'s wait

    def _escalate_kill(self):
        if self._process and self._process.returncode is None:
            try:
                print(f"[runner] SIGTERM ignored after 5s — sending SIGKILL")
                self._process.kill()
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[runner] SIGKILL failed: {e}")

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
