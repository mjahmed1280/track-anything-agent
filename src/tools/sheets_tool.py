"""
Sheets Tool — Mirror/display layer. Firestore is the source of truth.

All Google Sheets API calls are synchronous (google-api-python-client),
so they run inside asyncio's thread executor to avoid blocking FastAPI's event loop.

The main entry point is sync_tracker(), called by the background task in main.py.
"""
import asyncio
from functools import partial
from typing import Any

import google.auth
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

_service = None


def _get_service():
    global _service
    if _service is None:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        _service = build("sheets", "v4", credentials=credentials)
    return _service


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


# ── Raw Sheets operations ────────────────────────────────────────────────────

def _create_tab(title: str):
    svc = _get_service()
    body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
    svc.spreadsheets().batchUpdate(
        spreadsheetId=settings.SPREADSHEET_ID, body=body
    ).execute()
    logger.info(f"[Sheets] Created tab: {title}")


def _write_headers(tab_name: str, headers: list[str]):
    svc = _get_service()
    svc.spreadsheets().values().update(
        spreadsheetId=settings.SPREADSHEET_ID,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [headers]},
    ).execute()


def _append_rows(tab_name: str, rows: list[list[str]]):
    svc = _get_service()
    svc.spreadsheets().values().append(
        spreadsheetId=settings.SPREADSHEET_ID,
        range=f"'{tab_name}'!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def _get_rows(tab_name: str) -> list[list[str]]:
    svc = _get_service()
    result = svc.spreadsheets().values().get(
        spreadsheetId=settings.SPREADSHEET_ID,
        range=f"'{tab_name}'",
    ).execute()
    return result.get("values", [])


# ── Public async API ─────────────────────────────────────────────────────────

async def ensure_tab_exists(tab_name: str, headers: list[str]):
    """Create the Sheet tab with headers if it doesn't already exist."""
    try:
        rows = await _run_sync(_get_rows, tab_name)
        if not rows:
            await _run_sync(_write_headers, tab_name, headers)
    except HttpError as e:
        if "Unable to parse range" in str(e):
            await _run_sync(_create_tab, tab_name)
            await _run_sync(_write_headers, tab_name, headers)
        else:
            raise


async def append_log_row(tab_name: str, headers: list[str], values: list[str]) -> dict[str, Any]:
    """Append a single log row to the Sheet, creating the tab if needed."""
    try:
        await ensure_tab_exists(tab_name, headers)
        await _run_sync(_append_rows, tab_name, [values])
        logger.info(f"[Sheets] Appended row to '{tab_name}': {values}")
        return {"status": "success", "message": f"Row appended to '{tab_name}'."}
    except HttpError as e:
        logger.error(f"[Sheets] Error: {e}")
        return {"status": "error", "message": str(e)}


async def sync_tracker(
    tracker_name: str,
    headers: list[str],
    unsynced_logs: list[tuple[str, dict]],
    mark_synced_fn,
) -> int:
    """
    Push all unsynced Firestore logs to Google Sheets.
    Calls mark_synced_fn(tracker_name, doc_id) after each successful write.
    Returns the number of rows successfully synced.
    """
    synced_count = 0
    for doc_id, log_data in unsynced_logs:
        row = [str(log_data.get(h, "")) for h in headers]
        result = await append_log_row(tracker_name, headers, row)
        if result["status"] == "success":
            await mark_synced_fn(tracker_name, doc_id)
            synced_count += 1
        else:
            logger.warning(f"[Sheets] Failed to sync doc {doc_id}: {result['message']}")
    return synced_count
