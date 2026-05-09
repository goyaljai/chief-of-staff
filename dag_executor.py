"""V3 #7: DAG executor for fan-out parallel Claude execution.

Currently a stub that uses asyncio.gather (NOT LangGraph yet — minimal viable).
LangGraph integration upgrade comes when we hit a real limitation.

Uses Orchestrator.parse_dag to convert a brief into a step graph, then runs
independent steps in parallel via asyncio.gather. Each step runs as its own
Claude Code subprocess in a subdirectory of the task workspace.
"""
import asyncio
from pathlib import Path
from typing import Callable

from claude_runner import ClaudeRunner


async def run_step(step: dict, workspace: Path, log_path: Path) -> dict:
    runner = ClaudeRunner(working_dir=workspace, hook_log_path=log_path)
    result = await runner.run(prompt=step["action"], timeout_secs=600)
    return {
        "step_id": step["id"],
        "success": result.success,
        "output": (result.output or "")[:1000],
        "session_id": result.session_id,
    }


async def execute_dag(steps: list[dict], task_workspace: Path, hook_log: Path) -> dict:
    """Topological execution with parallel fan-out for steps that share no deps."""
    done: dict[str, dict] = {}
    pending = list(steps)
    while pending:
        ready = [s for s in pending if all(d in done for d in s.get("depends_on", []))]
        if not ready:
            return {"error": "deadlock — circular deps?", "done": list(done.keys()), "pending": [s["id"] for s in pending]}
        results = await asyncio.gather(*[
            run_step(s, task_workspace / s["id"], hook_log) for s in ready
        ])
        for s, r in zip(ready, results):
            done[s["id"]] = r
            pending = [p for p in pending if p["id"] != s["id"]]
    return {"results": done, "ok": True}


async def upgrade_to_langgraph_when_ready(steps: list[dict], task_workspace: Path):
    """Placeholder: when we adopt LangGraph (V3.x), this becomes the entrypoint.
    For now, fall through to execute_dag."""
    raise NotImplementedError("LangGraph integration deferred to V3.x")
