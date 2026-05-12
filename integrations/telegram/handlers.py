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
    # Bug fix (Phase 3 audit r2): only clearing `task_id` left G9
    # adaptive-question state (`pending_question`, `answers`,
    # `task_text`, `questions`, `q_index`) in chat_data, so a new task
    # started right after /reset could route the user's first message
    # through receive_answer instead of receive_task. Clear all of
    # them.
    for k in ("task_id", "pending_question", "answers",
              "task_text", "questions", "q_index"):
        context.chat_data.pop(k, None)
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
            # P4 #16: workspace continuity for follow-ups. Last task is
            # terminal but RECENT (< 30 min). Treat the new prompt as a
            # follow-up modification: surface the prior workspace + goal
            # as a clarification so the orchestrator builds a brief that
            # MODIFIES in place rather than scaffolding fresh.
            if state and state.get("status") in ("done", "failed", "abandoned"):
                started = state.get("started_at") or 0
                import time as _time
                if started and (_time.time() - started) < 1800:  # 30 min
                    prev_ws = state.get("workspace")
                    prev_goal = state.get("goal")
                    if prev_ws and prev_goal:
                        context.chat_data["_seed_from_task_id"] = active
                        context.chat_data["_seed_from_workspace"] = prev_ws
                        context.chat_data["_seed_from_goal"] = prev_goal
                        await update.message.reply_text(
                            f"🧷 Continuing from your last task ({active}). "
                            f"I'll modify that workspace in place. "
                            f"(prefix 'new:' to scaffold from scratch instead)"
                        )
                # fall through to fresh task creation, but with seed in chat_data
                context.chat_data.pop("task_id", None)
            else:
                context.chat_data.pop("task_id", None)

    # Bug fix (Phase 3 audit r2): clear any G9 state left over from a
    # prior aborted conversation before recording the new task. Without
    # this, a stale pending_question/answers map can leak into the new
    # /task/questions call's clarifications dict.
    for k in ("pending_question", "answers", "questions", "q_index"):
        context.chat_data.pop(k, None)
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
    # P4 #16: workspace continuity. If the previous task in this chat
    # was recent + done/failed, receive_task stashed the prior workspace
    # path. Pass it to the supervisor as a clarification so the brief
    # generator knows to MODIFY in place rather than scaffold fresh.
    seed_ws = context.chat_data.pop("_seed_from_workspace", None)
    seed_goal = context.chat_data.pop("_seed_from_goal", None)
    seed_task_id = context.chat_data.pop("_seed_from_task_id", None)
    clarifications = dict(answers or {})
    if seed_ws and seed_goal:
        clarifications.setdefault(
            "_continuation_of_task",
            f"This is a follow-up to task {seed_task_id}. Original ask: \"{seed_goal[:200]}\". "
            f"Existing artifacts to MODIFY IN PLACE (not scaffold fresh) live at: {seed_ws}. "
            f"Read the workspace first, identify what's already built, and apply only the "
            f"diff the user is asking for now."
        )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/run",
                json={"task": task, "clarifications": clarifications},
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

    # Live-task UI cleanup (Phase 4 polish): one short started message
    # instead of two verbose paragraphs. The 'new:' / /reset hint moves
    # to /help — users see it once when they /start the bot, not every
    # task. Keeps chat lean.
    await context.bot.send_message(
        chat_id,
        f"🚀 Task `{task_id}` started — I'll ping when done or if I need you.",
        parse_mode="Markdown",
    )

    # Live-task UI cleanup: drop the standalone "What I'll do:" message
    # entirely. The brief and skill name are visible in the dashboard;
    # in chat the user just wants progress + result. Keep _poll_task
    # which now emits a single milestone summary instead of per-step
    # noise.
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
