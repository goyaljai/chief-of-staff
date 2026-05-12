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
    # Bug #4 fix: surface DAG step transitions so the user isn't staring
    # at a silent chat for 5+ minutes during a multi-step build (Android
    # gradle, RN expo export, etc.). We announce when each step starts
    # and finishes, but only once — no spam on every poll.
    announced_started: set[str] = set()
    announced_done: set[str] = set()
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

        # Live-task UI cleanup: a real Telegram task showed 15+ messages
        # (per-step started + completed pairs, "🔍 Reviewing...", "🛠️
        # Running parallel build steps...", per-loop status pings). User
        # only needs: started (sent at task creation), maybe ONE
        # mid-flight "applying corrections" if a fresh loop is needed,
        # escalation if any, and the completion message. Everything
        # else is noise. Drop the per-step + per-status spam.
        if status != last_status:
            last_status = status
            # Only emit a status message for correction loops past the
            # first — that's a meaningful "I noticed something wrong
            # and I'm fixing it" signal. Reviewing / executing_dag /
            # executing_loop_1 transitions are silent.
            if status.startswith("executing_loop_") and status != "executing_loop_1":
                loop_n = status.split("_")[-1]
                await context.bot.send_message(
                    chat_id,
                    f"🔄 Applying corrections (loop {loop_n})…",
                )
        # Track step ids without emitting per-step messages — kept so
        # a future "live progress bar" feature has the seed.
        for step_id, p in (state.get("dag_progress") or {}).items():
            if p.get("count", 0) > 0:
                announced_started.add(step_id)
        for e in (state.get("log_tail") or []):
            if (e.get("kind") == "dag_step_event"
                    and e.get("event_type") == "result"
                    and e.get("step_id")):
                announced_done.add(e["step_id"])

        if status == "escalated":
            esc = state.get("escalation") or {}
            await _send_escalation(context, chat_id, task_id, esc)
            return

        if status in ("done", "failed", "abandoned"):
            await _send_completion(context, chat_id, task_id, state)
            return

        await asyncio.sleep(POLLING_INTERVAL_SECS)


async def _send_escalation(context, chat_id: int, task_id: str, esc: dict):
    """Render an inline keyboard for an escalation. callback_data
    encodes task_id + answer slug so the callback handler can submit
    it back to /task/{id}/escalation without keeping per-chat state.

    G7+: when esc.kind == 'environment', renders three buttons
    (install/fix · accept fallback · abort) and surfaces the
    structured summary/why fields the executor emitted.
    """
    if esc.get("kind") == "environment":
        summary = (esc.get("summary") or "").strip()
        why = (esc.get("why") or "").strip()
        opt_a = esc.get("option_a", "")
        opt_b = esc.get("option_b", "")
        opt_abort = esc.get("option_abort") or "Cancel the task"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("A — install / fix", callback_data=f"esc:{task_id}:a")],
            [InlineKeyboardButton("B — accept fallback", callback_data=f"esc:{task_id}:b")],
            [InlineKeyboardButton("✖ Abort", callback_data=f"esc:{task_id}:abort")],
        ])
        text = (
            f"⚠️ *Environment wall* (`{task_id}`)\n\n"
            + (f"*{summary}*\n\n" if summary else "")
            + (f"_Why:_ {why}\n\n" if why else "")
            + f"*A)* {opt_a}\n*B)* {opt_b}\n*Abort)* {opt_abort}"
        )
        await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
        return

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

    # P4 #15: failed-with-best_effort still has a partial deliverable.
    # User got "❌ task failed" but the workspace had the working APK
    # (just without orange/EditText). Reframe the message: ⚠️ partial
    # + clear "couldn't apply X" callout, AND we still send the APK.
    is_partial = (
        not result.get("success")
        and result.get("best_effort")
        and result.get("deliverables")
    )
    if is_partial:
        header = f"⚠️ Task {task_id} — partial result in {duration:.0f}s"
    else:
        header = f"{emoji} Task {task_id} — {status} in {duration:.0f}s"
    text = f"{header}\n\n{summary[:1500]}"

    if next_steps:
        text += f"\n\n— How to use it —\n{next_steps[:2500]}"

    if not result.get("success") and result.get("issues"):
        issues_text = "\n".join(f"• {i[:200]}" for i in (result.get("issues") or [])[:3])
        if is_partial:
            text += (
                "\n\n⚠️ I couldn't apply these (the file below is best-"
                "effort without them):\n" + issues_text
            )
        else:
            text += f"\n\nIssues:\n{issues_text}"

    text += f"\n\nWorkspace: {workspace}"

    await context.bot.send_message(chat_id, text)

    # P4 #15: _send_artifacts ships any declared deliverables — even
    # on failed status — so the user gets the partial APK with a
    # clear "best-effort" caveat from the message above.
    await _send_artifacts(context, chat_id, state)


_TG_DOC_MAX_BYTES = 49 * 1024 * 1024  # 1MB headroom under TG's 50MB cap


async def _send_artifacts(context, chat_id: int, state: dict):
    """Send EXACTLY the files the reviewer declared as deliverables — nothing else.

    G10 (2026-05-12): the reviewer's final_review now returns a
    `deliverables: list[str]` of workspace-relative paths. Those are the
    only files we hand back to the user. If the list is empty, send
    nothing — that's a valid outcome (research / Q&A tasks have no file
    deliverable). The reviewer is the only thing that knows what the
    user actually asked for; previously we tried to infer it via summary
    parsing + a hardcoded scaffolding blocklist (gradlew / package-lock
    / etc.), which was the rule-encoding anti-pattern called out by
    the user 2026-05-11.

    Cross-checks we still do (cheap, defensive):
      • file exists at the declared path
      • file size > 0 and ≤ 49MB (Telegram per-document cap)
      • path doesn't escape the workspace (no '../', no leading '/')
      • dedupe by basename so a reviewer accidentally listing the same
        file twice via different paths only sends once
    """
    import os
    workspace = state.get("workspace") or ""
    result = state.get("result") or {}
    deliverables = result.get("deliverables") or []
    if not workspace or not deliverables:
        return

    seen_basenames: set[str] = set()
    sent = 0
    skipped_missing: list[str] = []
    skipped_empty: list[str] = []
    skipped_oversize: list[str] = []

    # Bug fix (Phase 3 audit r2): canonicalize the workspace root once
    # so every deliverable can be checked for containment via prefix.
    # Symlinks inside the workspace pointing OUTSIDE it would otherwise
    # let us send arbitrary files (e.g. a `link.txt` deliverable that
    # symlinks to /etc/passwd). os.path.isfile follows symlinks so the
    # naive check passed it through.
    workspace_root = os.path.realpath(workspace)

    for rel_path in deliverables:
        if not isinstance(rel_path, str) or not rel_path.strip():
            continue
        rel_path = rel_path.strip().lstrip("/")
        # Reject obvious traversal — stay strict, the reviewer is an LLM.
        if ".." in rel_path.split("/"):
            log.warning("skipping deliverable with '..' segment: %s", rel_path)
            continue

        abs_path = os.path.join(workspace, rel_path)
        # Resolve symlinks then verify the resolved path is still
        # under the workspace. Rejects symlink-based escapes.
        try:
            real_abs = os.path.realpath(abs_path)
        except Exception:
            skipped_missing.append(rel_path)
            continue
        if not (real_abs == workspace_root or real_abs.startswith(workspace_root + os.sep)):
            log.warning("rejecting deliverable that escapes workspace: %s -> %s", rel_path, real_abs)
            skipped_missing.append(rel_path)
            continue
        abs_path = real_abs
        if not os.path.isfile(abs_path):
            skipped_missing.append(rel_path)
            continue

        size = os.path.getsize(abs_path)
        if size == 0:
            # Bug fix (Phase 3 audit): empty files are NOT missing —
            # the file exists but is zero bytes. Track separately so
            # the user message is accurate.
            skipped_empty.append(rel_path)
            continue
        if size > _TG_DOC_MAX_BYTES:
            skipped_oversize.append(f"{rel_path} ({size // (1024*1024)}MB)")
            continue

        basename = rel_path.rsplit("/", 1)[-1].lower()
        if basename in seen_basenames:
            continue
        seen_basenames.add(basename)

        try:
            with open(abs_path, "rb") as fh:
                await context.bot.send_document(
                    chat_id,
                    document=fh,
                    filename=rel_path.rsplit("/", 1)[-1],
                    caption=f"📎 {rel_path}",
                )
            sent += 1
        except Exception as e:
            log.warning("send_document(%s) failed: %s", abs_path, e)

    notes: list[str] = []
    if skipped_missing:
        notes.append(f"⚠️ {len(skipped_missing)} declared deliverable(s) missing in workspace: "
                     + ", ".join(skipped_missing[:3]))
    if skipped_empty:
        notes.append(f"⚠️ {len(skipped_empty)} declared deliverable(s) exist but are empty: "
                     + ", ".join(skipped_empty[:3]))
    if skipped_oversize:
        notes.append(f"⚠️ {len(skipped_oversize)} deliverable(s) > 50MB (Telegram limit): "
                     + ", ".join(skipped_oversize[:3]))
    if notes:
        await context.bot.send_message(chat_id, "\n".join(notes))
