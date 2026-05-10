"""Outbound notifications: action-line preview, status polling, escalation
prompts, and completion summaries.

These run as fire-and-forget asyncio tasks scheduled by the conversation
handlers. The contract is "given a chat_id and a task_id, ping the user
when something interesting happens" — escalations, completion, status
transitions worth surfacing.

Why polling and not server-push: the Telegram bot runs as a separate
process and talks to the FastAPI server over HTTP. Long polling against
GET /task/{id} is simpler than wiring SSE through python-telegram-bot's
event loop, and the polling interval is cheap (5s) for the few in-flight
tasks a single user has at once.
"""
import asyncio
import logging

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import SUPERVISOR_API_BASE_URL

from .auth import HTTP_TIMEOUT, POLLING_INTERVAL_SECS


log = logging.getLogger(__name__)


async def _send_action_line(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str):
    """Once the orchestrator has generated the per-task SKILL.md, send
    ONE line to the user telling them what's about to happen. Polls
    GET /task/{id} up to 60 times (≈2 minutes) waiting for skill_md to
    land; gives up silently if the task finishes before the skill is
    materialized."""
    for _ in range(60):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{SUPERVISOR_API_BASE_URL}/task/{task_id}",
                    timeout=HTTP_TIMEOUT,
                )
                r.raise_for_status()
                state = r.json()
        except Exception:
            await asyncio.sleep(2)
            continue
        skill_md = state.get("skill_md", "")
        if skill_md:
            line = _extract_action_line(skill_md)
            if line:
                await context.bot.send_message(chat_id, f"What I'll do: {line}")
            return
        if state.get("status") in ("done", "failed", "abandoned"):
            return
        await asyncio.sleep(2)


def _extract_action_line(skill_md: str) -> str:
    """Pull a single one-sentence action statement from the SKILL.md
    Objective section. Returns empty string when the section is missing
    or empty — caller treats that as 'no preview, skip the message'."""
    if not skill_md:
        return ""
    lines = skill_md.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("## objective"):
            for follow in lines[i + 1:i + 8]:
                s = follow.strip()
                if not s or s.startswith("#"):
                    continue
                first_sentence = s.split(". ")[0].rstrip(".") + "."
                first_sentence = first_sentence.replace("**", "")
                return first_sentence[:300]
    return ""


async def _poll_task(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str):
    """Long-poll the supervisor's /task/{id} endpoint until terminal.
    Surfaces interesting status transitions (correction loops, reviews)
    and dispatches to _send_escalation / _send_completion when those
    events fire."""
    last_status: str | None = None
    while True:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{SUPERVISOR_API_BASE_URL}/task/{task_id}",
                    timeout=HTTP_TIMEOUT,
                )
                r.raise_for_status()
                state = r.json()
        except Exception as e:
            log.warning("poll failed: %s", e)
            await asyncio.sleep(POLLING_INTERVAL_SECS)
            continue

        status = state.get("status", "")

        if status != last_status:
            last_status = status
            if status.startswith("reviewing_"):
                await context.bot.send_message(
                    chat_id,
                    f"🔍 Reviewing {status.replace('reviewing_', '')}...",
                )
            elif status.startswith("executing_loop_") and status != "executing_loop_1":
                loop_n = status.split("_")[-1]
                await context.bot.send_message(
                    chat_id,
                    f"🔄 Starting correction loop {loop_n}...",
                )

        if status == "escalated":
            esc = state.get("escalation") or {}
            await _send_escalation(context, chat_id, task_id, esc)
            return

        if status in ("done", "failed", "abandoned"):
            await _send_completion(context, chat_id, task_id, state)
            return

        await asyncio.sleep(POLLING_INTERVAL_SECS)


async def _send_escalation(context, chat_id: int, task_id: str, esc: dict):
    """Render an A/B inline keyboard for an escalation. The callback_data
    encodes the task_id + answer letter so the callback handler can
    submit it back to /task/{id}/escalation without keeping per-chat
    state."""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("A", callback_data=f"esc:{task_id}:a"),
        InlineKeyboardButton("B", callback_data=f"esc:{task_id}:b"),
    ]])
    text = (
        f"🔴 *Need your decision* (`{task_id}`)\n\n"
        f"{esc.get('question', '(no question)')}\n\n"
        f"*A)* {esc.get('option_a', '')}\n"
        f"*B)* {esc.get('option_b', '')}"
    )
    await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")


async def _send_completion(context, chat_id: int, task_id: str, state: dict):
    """Final summary message at task end. Includes the reviewer's
    next_steps when present so the user knows EXACTLY how to use the
    deliverable, without having to ssh into the workspace and figure it
    out themselves."""
    status = state["status"]
    result = state.get("result") or {}
    duration = round(state.get("duration_secs", 0), 1)
    workspace = state.get("workspace", "?")
    emoji = {"done": "✅", "failed": "❌", "abandoned": "⚠️", "cancelled": "⚪"}.get(status, "❓")
    summary = result.get("summary") or "(no summary)"
    next_steps = result.get("next_steps") or ""

    text = f"{emoji} Task {task_id} — {status} in {duration:.0f}s\n\n{summary[:1500]}"

    if next_steps:
        text += f"\n\n— How to use it —\n{next_steps[:2500]}"

    if not result.get("success") and result.get("issues"):
        issues_text = "\n".join(f"• {i[:200]}" for i in (result.get("issues") or [])[:3])
        text += f"\n\nIssues:\n{issues_text}"

    text += f"\n\nWorkspace: {workspace}"

    await context.bot.send_message(chat_id, text)
