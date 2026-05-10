"""Telegram whitelist + shared timing constants.

Single-user mode by default: only Telegram user_ids in
TELEGRAM_ALLOWED_USER_IDS are allowed to start tasks. The first DM the
bot receives prints the user's ID so the user knows what to add to .env.

ASKING is the ConversationHandler state for the multi-question
question→answer flow. POLLING_INTERVAL_SECS controls how often the bot
re-fetches /task/{id} during a run. HTTP_TIMEOUT bounds every outbound
call to the supervisor API.
"""
from telegram import Update

from config import TELEGRAM_ALLOWED_USER_IDS


ASKING = 1
POLLING_INTERVAL_SECS = 5
HTTP_TIMEOUT = 60


def _whitelisted(update: Update) -> bool:
    return str(update.effective_user.id) in TELEGRAM_ALLOWED_USER_IDS


async def _deny(update: Update):
    """Tell an un-allowed user their ID so they can self-onboard by adding
    it to the .env file. The same message tells them what to do next —
    no separate help command needed."""
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Access denied.\n\n"
        f"Your Telegram user_id is: {uid}\n\n"
        f"Add this number to TELEGRAM_ALLOWED_USER_IDS in the .env file and restart the bot."
    )
