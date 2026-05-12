"""Telegram handlers — slash commands + conversation flow + escalation callback.

CONVERSATION FLOW
=================

  user sends task                → receive_task
                                    ├── if active task & no `new:` prefix
                                    │     → POST /task/{id}/note (side-note)
                                    └── else: POST /task/questions
                                          → reply_text(Q1) + state=ASKING
  user answers Q1                 → receive_answer
                                    └── more questions? → reply_text(Q2)
                                        all answered?   → _launch
  _launch                         → POST /task/run
                                    + asyncio.create_task(_send_action_line)
                                    + asyncio.create_task(_poll_task)

SLASH COMMANDS
==============
  /start           cmd_start
  /status          cmd_status      list recent tasks
  /reset           cmd_reset       forget current task, start fresh
  /cancel          cmd_cancel_conv abandon current conversation

ESCALATION
==========
  user taps A/B    handle_escalation_callback
                    → POST /task/{id}/escalation
                    + restart polling
"""
import asyncio

import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes, ConversationHandler

from config import SUPERVISOR_API_BASE_URL

from .auth import ASKING, HTTP_TIMEOUT, _deny, _whitelisted
from .notifications import _poll_task, _send_action_line


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _whitelisted(update):
        await _deny(update)
        return ConversationHandler.END
    await update.message.reply_text(
        "Hi — I'm your chief of staff.\n\n"
        "Send me any task — code, research, writing, analysis.\n"
        "I'll ask 3-5 quick questions, then run it end to end.\n"
        "I'll only ping you when I genuinely need a decision.\n\n"
        "Commands\n"
        "  /status   — list recent tasks\n"
        "  /reset    — forget current task, start fresh\n"
        "  /cancel   — abandon current conversation\n\n"
        "Tip: while a task is running, any text you send gets folded\n"
        "into it as a side note. Use 'new: <task>' to start a fresh one.",
    )
    return ConversationHandler.END


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _whitelisted(update):
        await _deny(update)
        return
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{SUPERVISOR_API_BASE_URL}/tasks", timeout=HTTP_TIMEOUT)
        tasks = r.json()
    if not tasks:
        await update.message.reply_text("No tasks yet.")
        return
    lines = []
    for t in tasks[-10:]:
        emoji = {"done": "✅", "failed": "❌", "escalated": "🔴"}.get(t["status"], "⏳")
        lines.append(f"{emoji} `{t['id']}` — {t['status']} ({t.get('log_size', 0)} log entries)")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.chat_data.clear()
    await update.message.reply_text("Conversation cleared. Send a new task whenever you're ready.")
    return ConversationHandler.END


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _whitelisted(update):
        await _deny(update)
        return ConversationHandler.END
    context.chat_data.pop("task_id", None)
    await update.message.reply_text("Active task forgotten. Next message starts a fresh task.")
    return ConversationHandler.END


async def receive_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _whitelisted(update):
        await _deny(update)
        return ConversationHandler.END

    task = update.message.text.strip()
    if task.startswith("/"):
        return ConversationHandler.END

    if task.lower().startswith("new:") or task.lower().startswith("/new "):
        task = (
            task.split(":", 1)[1].strip()
            if task.lower().startswith("new:")
            else task[5:].strip()
        )
        context.chat_data.pop("task_id", None)
    else:
        active = context.chat_data.get("task_id")
        if active:
            try:
                async with httpx.AsyncClient() as client:
                    s = await client.get(
                        f"{SUPERVISOR_API_BASE_URL}/task/{active}",
                        timeout=HTTP_TIMEOUT,
                    )
                    state = s.json() if s.status_code == 200 else None
            except Exception:
                state = None
            if state and state.get("status") not in ("done", "failed", "abandoned", "cancelled", None):
                # Active task — fold the message in as a side-note instead
                # of starting a new task.
                try:
                    async with httpx.AsyncClient() as client:
                        r = await client.post(
                            f"{SUPERVISOR_API_BASE_URL}/task/{active}/note",
                            json={"note": task},
                            timeout=HTTP_TIMEOUT,
                        )
                        r.raise_for_status()
                    await update.message.reply_text(
                        f"Noted — folding into current task {active} on the next loop. "
                        f"(prefix 'new:' or send /reset to start a fresh task instead)"
                    )
                except Exception as e:
                    await update.message.reply_text(f"Failed to add note: {e}")
                return ConversationHandler.END
            else:
                context.chat_data.pop("task_id", None)

    context.chat_data["task_text"] = task

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    await update.message.reply_text("🤔 Thinking about clarifying questions...")

    # G9 adaptive: ask one Q at a time, condition next on prior answers.
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/questions",
                json={"task": task, "clarifications": {}},
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await update.message.reply_text(f"❌ Error reaching the orchestrator: {e}")
        return ConversationHandler.END

    if data.get("done"):
        await update.message.reply_text("✅ No clarifying questions needed. Starting task...")
        await _launch(update, context, task, {})
        return ConversationHandler.END

    questions = data.get("questions") or []
    if not questions:
        # Defensive: no Q but not marked done — treat as done.
        await update.message.reply_text("✅ Starting task...")
        await _launch(update, context, task, {})
        return ConversationHandler.END

    # Initialize G9 state. Unlike before, we DON'T pre-fetch a question
    # list — each answer triggers the next /task/questions call which
    # decides the next Q (or 'done').
    context.chat_data["answers"] = {}
    context.chat_data["pending_question"] = questions[0]

    await update.message.reply_text(
        f"📋 *Q1:* {questions[0]}",
        parse_mode="Markdown",
    )
    return ASKING


async def receive_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """G9 adaptive: record answer, fetch next question (or done) by
    POSTing all prior answers back to /task/questions. The orchestrator
    decides per-call whether another question is worth asking."""
    if not _whitelisted(update):
        await _deny(update)
        return ConversationHandler.END

    answer = update.message.text.strip()
    pending_q = context.chat_data.get("pending_question")
    answers = context.chat_data.get("answers", {})
    task = context.chat_data.get("task_text", "")

    if not pending_q or not task:
        return ConversationHandler.END

    answers[pending_q] = answer
    context.chat_data["answers"] = answers

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/questions",
                json={"task": task, "clarifications": answers},
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Couldn't fetch next question ({e}). Starting with what I have."
        )
        await _launch(update, context, task, answers)
        return ConversationHandler.END

    if data.get("done"):
        await _launch(update, context, task, answers)
        return ConversationHandler.END

    next_qs = data.get("questions") or []
    if not next_qs:
        await _launch(update, context, task, answers)
        return ConversationHandler.END

    next_q = next_qs[0]
    context.chat_data["pending_question"] = next_q
    qnum = len(answers) + 1
    await update.message.reply_text(
        f"*Q{qnum}:* {next_q}",
        parse_mode="Markdown",
    )
    return ASKING


async def _launch(update: Update, context: ContextTypes.DEFAULT_TYPE, task: str, answers: dict):
    chat_id = update.effective_chat.id
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/run",
                json={"task": task, "clarifications": answers},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await context.bot.send_message(chat_id, f"Failed to start task: {e}")
        return

    task_id = data["task_id"]
    # Critical: stash the active task_id so the next user message gets
    # folded as a note instead of being misinterpreted as a brand-new
    # task. Without this, casual acks like "Sure" / "ok" / "thanks"
    # spawn a fresh /task/questions round-trip — confusing as hell.
    # receive_task() reads chat_data['task_id'] and only kicks off a
    # new task when the active one is in a terminal status (or absent).
    context.chat_data["task_id"] = task_id
    # Clear the ASKING-flow scratch state so it doesn't leak into the
    # next conversation. Includes G9's `pending_question` (replaces the
    # old `questions`/`q_index` pair).
    for k in ("questions", "answers", "q_index", "task_text", "pending_question"):
        context.chat_data.pop(k, None)

    await context.bot.send_message(
        chat_id,
        f"Started task {task_id}. I'll ping you when there's news.\n\n"
        "(reply with 'new: <prompt>' or /reset to start a fresh task — anything "
        "else gets folded into the current task as a note)",
    )

    asyncio.create_task(_send_action_line(context, chat_id, task_id))
    asyncio.create_task(_poll_task(context, chat_id, task_id))


async def handle_escalation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _whitelisted(update):
        await update.callback_query.answer("Not authorized")
        return

    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "esc":
        return
    task_id, answer = parts[1], parts[2]
    chat_id = update.effective_chat.id

    # G7+: the env-escalation surface adds an explicit "abort" button.
    # That maps to a hard cancel, not an escalation answer — calling
    # /escalation with answer='abort' would just store the string and
    # let the runner keep grinding. Route to /cancel instead so the
    # subprocess actually dies.
    is_abort = answer.lower() == "abort"
    endpoint = "cancel" if is_abort else "escalation"
    payload: dict | None = None if is_abort else {"answer": answer}

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/{task_id}/{endpoint}",
                json=payload,
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Failed to submit answer: {e}")
        return

    decision_label = "ABORTED — task cancelled" if is_abort else f"Answered: *{answer.upper()}*"
    await query.edit_message_text(
        (query.message.text or "") + f"\n\n✓ {decision_label}",
        parse_mode="Markdown",
    )

    if is_abort:
        # Bug fix (Phase 3 audit): previously we returned silently after
        # /cancel, so the user never saw a final "task cancelled"
        # message — only the inline edit. If /cancel succeeded but the
        # runner died slowly, there was no second confirmation. Send
        # an explicit follow-up so the user knows the runner is dead.
        try:
            await context.bot.send_message(
                chat_id,
                f"🛑 Task `{task_id}` cancelled. The runner has been stopped.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    asyncio.create_task(_poll_task(context, chat_id, task_id))
