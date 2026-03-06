import asyncio
from functools import partial
from googleapiclient.discovery import build
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


def _get_sheets_service():
    # Uses GOOGLE_APPLICATION_CREDENTIALS env var automatically
    return build("sheets", "v4")


async def create_tracker_sheet(tracker_name: str, headers: list):
    """Create a new tab in the spreadsheet for a tracker and write the header row."""
    service = _get_sheets_service()
    spreadsheet_id = settings.SPREADSHEET_ID

    def _add_tab():
        body = {"requests": [{"addSheet": {"properties": {"title": tracker_name}}}]}
        return service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body=body
        ).execute()

    try:
        await _run_sync(_add_tab)
    except Exception as e:
        if "already exists" in str(e).lower():
            logger.warning(f"Sheet tab '{tracker_name}' already exists, skipping creation")
        else:
            logger.error(f"Failed to create sheet tab '{tracker_name}': {e}")
            raise

    def _write_headers():
        return service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tracker_name}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": [headers]},
        ).execute()

    try:
        await _run_sync(_write_headers)
        logger.info(f"Created sheet tab '{tracker_name}' with headers: {headers}")
    except Exception as e:
        logger.error(f"Failed to write headers to sheet '{tracker_name}': {e}")
        raise

async def append_row(tracker_name: str, values: list):
    """Append a single row to the Google Sheet for a specific tracker."""
    service = _get_sheets_service()
    spreadsheet_id = settings.SPREADSHEET_ID
    
    # We assume each tracker has a tab named exactly like the tracker_name
    range_name = f"{tracker_name}!A1"
    
    body = {
        "values": [values]
    }

    def _execute():
        return service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body
        ).execute()

    try:
        await _run_sync(_execute)
        logger.info(f"Successfully appended row to Sheet: {tracker_name}")
    except Exception as e:
        logger.error(f"Failed to append to Google Sheets: {e}")
        raise e


async def sync_tracker(tracker_name: str, headers: list, unsynced: list, mark_synced_fn) -> int:
    """Push unsynced Firestore logs to Google Sheets. Returns count of rows pushed."""
    count = 0
    for doc_id, data in unsynced:
        try:
            row = []
            for h in headers:
                val = data.get(h, "")
                if hasattr(val, "isoformat"):
                    val = val.isoformat()
                row.append(str(val))
            await append_row(tracker_name, row)
            await mark_synced_fn(tracker_name, doc_id)
            count += 1
        except Exception as e:
            logger.error(f"Failed to sync log {doc_id} for '{tracker_name}': {e}")
    return count