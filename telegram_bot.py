"""Telegram bot — the V2 user interface.

Runs as a separate process. Talks to the FastAPI server at SUPERVISOR_API_BASE_URL.
Single-user mode: only Telegram user_ids in TELEGRAM_ALLOWED_USER_IDS are allowed.

Conversation flow:
  user sends task     →  bot calls /task/questions, sends Qs one by one
  user answers each   →  bot collects, calls /task/run
  bot polls /task/{id}→  posts updates on escalation, done, failed
  user taps A or B    →  bot calls /task/{id}/escalation
"""
import asyncio
import logging
from typing import Any

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

from config import (
    SUPERVISOR_API_BASE_URL,
    TELEGRAM_ALLOWED_USER_IDS,
    TELEGRAM_BOT_TOKEN,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

ASKING = 1
POLLING_INTERVAL_SECS = 5
HTTP_TIMEOUT = 60


def _whitelisted(update: Update) -> bool:
    return str(update.effective_user.id) in TELEGRAM_ALLOWED_USER_IDS


async def _deny(update: Update):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Access denied.\n\n"
        f"Your Telegram user_id is: {uid}\n\n"
        f"Add this number to TELEGRAM_ALLOWED_USER_IDS in the .env file and restart the bot."
    )


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
        task = task.split(":", 1)[1].strip() if task.lower().startswith("new:") else task[5:].strip()
        context.chat_data.pop("task_id", None)
    else:
        active = context.chat_data.get("task_id")
        if active:
            try:
                async with httpx.AsyncClient() as client:
                    s = await client.get(f"{SUPERVISOR_API_BASE_URL}/task/{active}", timeout=HTTP_TIMEOUT)
                    state = s.json() if s.status_code == 200 else None
            except Exception:
                state = None
            if state and state.get("status") not in ("done", "failed", "abandoned", "cancelled", None):
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

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/questions",
                json={"task": task},
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await update.message.reply_text(f"❌ Error reaching the orchestrator: {e}")
        return ConversationHandler.END

    questions = data.get("questions") or []
    if not questions:
        await update.message.reply_text("✅ No clarifying questions needed. Starting task...")
        await _launch(update, context, task, {})
        return ConversationHandler.END

    context.chat_data["questions"] = questions
    context.chat_data["answers"] = {}
    context.chat_data["q_index"] = 0

    await update.message.reply_text(
        f"📋 I have {len(questions)} question(s).\n\n*Q1:* {questions[0]}",
        parse_mode="Markdown",
    )
    return ASKING


async def receive_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _whitelisted(update):
        await _deny(update)
        return ConversationHandler.END

    answer = update.message.text.strip()
    questions = context.chat_data.get("questions", [])
    answers = context.chat_data.get("answers", {})
    idx = context.chat_data.get("q_index", 0)

    if idx >= len(questions):
        return ConversationHandler.END

    answers[questions[idx]] = answer
    idx += 1

    if idx >= len(questions):
        await _launch(update, context, context.chat_data["task_text"], answers)
        return ConversationHandler.END

    context.chat_data["answers"] = answers
    context.chat_data["q_index"] = idx
    await update.message.reply_text(
        f"*Q{idx + 1}:* {questions[idx]}",
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
    await context.bot.send_message(chat_id, f"Started task {task_id}. I'll ping you when there's news.")

    asyncio.create_task(_send_action_line(context, chat_id, task_id))
    asyncio.create_task(_poll_task(context, chat_id, task_id))


async def _send_action_line(context: ContextTypes.DEFAULT_TYPE, chat_id: int, task_id: str):
    """Once the orchestrator has generated Skill.md, send ONE line: what I'm going to do."""
    for _ in range(60):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{SUPERVISOR_API_BASE_URL}/task/{task_id}", timeout=HTTP_TIMEOUT)
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
    """Pull a single one-sentence action statement from the Skill.md Objective section."""
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
    last_status: str | None = None
    while True:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{SUPERVISOR_API_BASE_URL}/task/{task_id}", timeout=HTTP_TIMEOUT)
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
                await context.bot.send_message(chat_id, f"🔍 Reviewing {status.replace('reviewing_', '')}...")
            elif status.startswith("executing_loop_") and status != "executing_loop_1":
                loop_n = status.split("_")[-1]
                await context.bot.send_message(chat_id, f"🔄 Starting correction loop {loop_n}...")

        if status == "escalated":
            esc = state.get("escalation") or {}
            await _send_escalation(context, chat_id, task_id, esc)
            return

        if status in ("done", "failed", "abandoned"):
            await _send_completion(context, chat_id, task_id, state)
            return

        await asyncio.sleep(POLLING_INTERVAL_SECS)


async def _send_escalation(context, chat_id: int, task_id: str, esc: dict):
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

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{SUPERVISOR_API_BASE_URL}/task/{task_id}/escalation",
                json={"answer": answer},
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ Failed to submit answer: {e}")
        return

    await query.edit_message_text(
        (query.message.text or "") + f"\n\n✓ Answered: *{answer.upper()}*",
        parse_mode="Markdown",
    )

    asyncio.create_task(_poll_task(context, chat_id, task_id))


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in env / .env")

    from pathlib import Path as _P
    persist_path = _P(__file__).parent / "data" / "telegram_state.pkl"
    persist_path.parent.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=str(persist_path))
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_task),
        ],
        states={
            ASKING: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_answer)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel_conv)],
        allow_reentry=False,
        name="cos_main",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(handle_escalation_callback))

    return app


def main():
    app = build_app()
    if not TELEGRAM_ALLOWED_USER_IDS:
        log.warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty — bot will reply to anyone with their user_id "
            "but won't run tasks. DM the bot once, copy the user_id it returns, add to .env, restart."
        )
    log.info("Bot starting; talking to API at %s", SUPERVISOR_API_BASE_URL)
    app.run_polling()


if __name__ == "__main__":
    main()
