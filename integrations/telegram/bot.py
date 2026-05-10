"""Telegram bot entry point — Application factory + main().

Runs as a separate process from the FastAPI server. Talks to it via HTTP
at SUPERVISOR_API_BASE_URL. Long polling against Telegram (no public URL
or webhook needed).

Persistence: the conversation state for the multi-question flow is
pickled to data/telegram_state.pkl so a bot restart mid-conversation
doesn't lose the user's place.

Launch:
  python3 integrations/telegram/bot.py
or, equivalent:
  python3 -m integrations.telegram.bot
"""
import logging
from pathlib import Path

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# Load .env BEFORE any config import so TELEGRAM_BOT_TOKEN etc. are
# present when config.py reads them.
import os as _os
_ENV_FILE = Path(__file__).parent.parent.parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

from config import (  # noqa: E402
    SUPERVISOR_API_BASE_URL,
    TELEGRAM_ALLOWED_USER_IDS,
    TELEGRAM_BOT_TOKEN,
)

from .auth import ASKING  # noqa: E402
from .handlers import (  # noqa: E402
    cmd_cancel_conv,
    cmd_reset,
    cmd_start,
    cmd_status,
    handle_escalation_callback,
    receive_answer,
    receive_task,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def build_app() -> Application:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set in env / .env")

    persist_path = Path(__file__).parent.parent.parent / "data" / "telegram_state.pkl"
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
