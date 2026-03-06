"""
Local development runner — Telegram polling mode.

Replaces the webhook with long-polling so you can test without ngrok or a
public URL. Telegram pulls for new messages every second.

Usage:
  cd track-anything-agent
  python run_polling.py
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio

from src.tools.firestore_tool import ensure_config_exists
from src.utils.config import settings
from src.utils.logger import get_logger
from src.integrations.telegram import build_application

logger = get_logger("run_polling")


async def _startup():
    await ensure_config_exists()
    logger.info("Firestore config ready.")


if __name__ == "__main__":
    # Run startup (async) first, then hand over to python-telegram-bot's own loop
    asyncio.run(_startup())
    logger.info("Starting bot in polling mode — send a message to your bot now.")
    app = build_application(settings.TELEGRAM_BOT_TOKEN)
    app.run_polling()   # Blocking; manages its own event loop
