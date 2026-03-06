"""
Firestore Tool — Source of Truth for all log data.

Every log document includes:
  synced_to_sheets: bool  (False on write, True after successful Sheet sync)

Firestore SDK is synchronous; writes run in a thread executor to avoid
blocking FastAPI's event loop.
"""
import asyncio
from functools import partial
from typing import Any

from google.cloud import firestore

from src.agent.registry import register_tool
from src.tools import sheets_tool
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

db = firestore.Client(project=settings.GOOGLE_CLOUD_PROJECT)


async def _run_sync(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


# ── Tracker config ──────────────────────────────────────────────────────────

async def get_known_trackers() -> dict:
    """Return {tracker_name: [headers]} from Firestore system/config."""
    def _get():
        doc = db.collection("system").document("config").get()
        return doc.to_dict().get("trackers", {}) if doc.exists else {}
    return await _run_sync(_get)


async def get_tracker_descriptions() -> dict:
    """Return {tracker_name: description} from Firestore system/config."""
    def _get():
        doc = db.collection("system").document("config").get()
        return doc.to_dict().get("tracker_descriptions", {}) if doc.exists else {}
    return await _run_sync(_get)


async def ensure_config_exists():
    def _check():
        ref = db.collection("system").document("config")
        if not ref.get().exists:
            ref.set({"trackers": {}, "tracker_descriptions": {}, "initialized": True})
            logger.info("Initialized system/config in Firestore")
    await _run_sync(_check)


# ── Registered Tools ────────────────────────────────────────────────────────

async def _push_log_to_sheets(tracker_name: str, doc_id: str, log_data: dict, headers: list):
    """Push a single log row to Sheets and mark it synced. Fire-and-forget background task."""
    try:
        row = []
        for h in headers:
            val = log_data.get(h, "")
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            row.append(str(val))
        await sheets_tool.append_row(tracker_name, row)
        await mark_synced(tracker_name, doc_id)
        logger.info(f"Immediately synced log {doc_id} to Sheets for '{tracker_name}'")
    except Exception as e:
        logger.error(f"Immediate Sheets sync failed for '{tracker_name}/{doc_id}': {e} — will retry via background sync")


@register_tool("add_log")
async def add_log(tracker_name: str, values: list[str]) -> dict[str, Any]:
    """Write a log entry to Firestore with synced_to_sheets=False."""
    known = await get_known_trackers()
    if tracker_name not in known:
        return {"status": "error", "message": f"Tracker '{tracker_name}' not found."}

    headers = known[tracker_name]
    log_data = dict(zip(headers, values))
    log_data["timestamp"] = firestore.SERVER_TIMESTAMP
    log_data["synced_to_sheets"] = False

    doc_id = None

    def _write():
        nonlocal doc_id
        _, doc_ref = db.collection("trackers").document(tracker_name).collection("logs").add(log_data)
        db.collection("stats").document(tracker_name).set(
            {"logs_count": firestore.Increment(1)}, merge=True
        )
        doc_id = doc_ref.id

    await _run_sync(_write)
    logger.info(f"Logged to Firestore [{tracker_name}]: {log_data}")

    # Push to Sheets immediately; falls back to background sync in main.py on failure
    asyncio.create_task(_push_log_to_sheets(tracker_name, doc_id, log_data, headers))

    return {"status": "success", "message": f"Logged to '{tracker_name}'."}


add_log.__tool_schema__ = {
    "name": "add_log",
    "description": "Log an entry into an existing tracker. Values must match the tracker headers in order.",
    "parameters": {
        "type": "object",
        "properties": {
            "tracker_name": {"type": "string", "description": "Name of the existing tracker"},
            "values": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Values mapping to the tracker headers in order",
            },
        },
        "required": ["tracker_name", "values"],
    },
}


@register_tool("create_tracker")
async def create_tracker(tracker_name: str, headers: list[str], description: str = "") -> dict[str, Any]:
    """Register a new tracker in Firestore system/config."""
    if "Date" not in headers:
        headers = ["Date"] + headers

    def _write():
        db.collection("system").document("config").update({
            f"trackers.{tracker_name}": headers,
            f"tracker_descriptions.{tracker_name}": description,
        })

    await _run_sync(_write)
    logger.info(f"Registered tracker '{tracker_name}' with headers {headers}, description: '{description}'")

    # Create the sheet tab with headers immediately
    asyncio.create_task(sheets_tool.create_tracker_sheet(tracker_name, headers))

    return {"status": "success", "message": f"Tracker '{tracker_name}' created with headers: {headers}."}


create_tracker.__tool_schema__ = {
    "name": "create_tracker",
    "description": "Create a new tracker. 'Date' is always prepended as the first column.",
    "parameters": {
        "type": "object",
        "properties": {
            "tracker_name": {"type": "string", "description": "Name for the new tracker"},
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Column headers e.g. ['Amount', 'Category', 'Notes']",
            },
            "description": {
                "type": "string",
                "description": "Short description of what this tracker is for (e.g. 'Track daily food expenses')",
            },
        },
        "required": ["tracker_name", "headers"],
    },
}


@register_tool("get_logs_summary")
async def get_logs_summary(tracker_name: str, limit: int = 10) -> dict[str, Any]:
    """Fetch the most recent N logs from Firestore for a tracker."""
    def _fetch():
        docs = (
            db.collection("trackers")
            .document(tracker_name)
            .collection("logs")
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [d.to_dict() for d in docs]

    logs = await _run_sync(_fetch)
    if not logs:
        return {"status": "success", "message": f"No logs found for '{tracker_name}'.", "data": []}
    return {"status": "success", "message": f"Last {len(logs)} logs retrieved.", "data": logs}


get_logs_summary.__tool_schema__ = {
    "name": "get_logs_summary",
    "description": "Retrieve recent log entries from a tracker for summarization.",
    "parameters": {
        "type": "object",
        "properties": {
            "tracker_name": {"type": "string", "description": "Name of the tracker to query"},
            "limit": {"type": "integer", "description": "Max entries to fetch (default 10)"},
        },
        "required": ["tracker_name"],
    },
}


# ── Background sync helpers ──────────────────────────────────────────────────

async def get_unsynced_logs(tracker_name: str) -> list[tuple[str, dict]]:
    """Return (doc_id, data) for logs where synced_to_sheets=False."""
    def _fetch():
        docs = (
            db.collection("trackers")
            .document(tracker_name)
            .collection("logs")
            .where("synced_to_sheets", "==", False)
            .stream()
        )
        return [(d.id, d.to_dict()) for d in docs]
    return await _run_sync(_fetch)


async def mark_synced(tracker_name: str, doc_id: str):
    def _update():
        (
            db.collection("trackers")
            .document(tracker_name)
            .collection("logs")
            .document(doc_id)
            .update({"synced_to_sheets": True})
        )
    await _run_sync(_update)

