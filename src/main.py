"""
FastAPI entry point for track-anything-agent.

Key improvements over legacy main.py:
  - lifespan context manager instead of @app.on_event("startup")
  - Fixed CORS: no allow_credentials=True with wildcard origins
  - /webhook endpoint for Telegram (primary interface)
  - Background task: sync unsynced Firestore logs to Google Sheets
"""
from dotenv import load_dotenv
load_dotenv()

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update

from src.integrations.telegram import build_application
from src.tools.firestore_tool import ensure_config_exists, get_known_trackers, get_unsynced_logs, mark_synced
from src.tools.sheets_tool import sync_tracker
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Build Telegram application once at startup
telegram_app = build_application(settings.TELEGRAM_BOT_TOKEN)


async def run_sheets_sync():
    """Background task: push unsynced Firestore logs to Google Sheets."""
    try:
        known = await get_known_trackers()
        for tracker_name, headers in known.items():
            unsynced = await get_unsynced_logs(tracker_name)
            if unsynced:
                count = await sync_tracker(tracker_name, headers, unsynced, mark_synced)
                if count:
                    logger.info(f"[Sync] Pushed {count} rows to Sheets for '{tracker_name}'")
    except Exception as e:
        logger.error(f"[Sync] Background sync failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting track-anything-agent...")
    await ensure_config_exists()
    await telegram_app.initialize()
    yield
    # Shutdown
    logger.info("Shutting down...")
    await telegram_app.shutdown()


app = FastAPI(title="track-anything-agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
    # Note: allow_credentials must NOT be True when allow_origins=["*"]
)


@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook."""
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)

    # Fire-and-forget Sheets sync after each message
    asyncio.create_task(run_sheets_sync())

    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
