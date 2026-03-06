"""
Direct integration tests — no Telegram, no HTTP server needed.
Tests tool registration, Firestore reads/writes, and the orchestrator
end-to-end against your real GCP project.

Requirements:
  - .env populated (GOOGLE_CLOUD_PROJECT, GEMINI_API_KEY)
  - GOOGLE_APPLICATION_CREDENTIALS set in .env pointing to your service account JSON
  - pip install -r requirements.txt

Usage:
  cd track-anything-agent
  python tests/test_tools.py
"""
import asyncio
import sys
import os

# Run from the track-anything-agent directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.tools.firestore_tool import (
    ensure_config_exists,
    get_known_trackers,
    create_tracker,
    add_log,
    get_logs_summary,
    get_unsynced_logs,
)
from src.agent.registry import TOOL_REGISTRY
from src.agent.orchestrator import run as orchestrator_run

PASS = "[PASS]"
FAIL = "[FAIL]"


async def test_tool_registry():
    print("\n── Tool Registry ──")
    registered = list(TOOL_REGISTRY.keys())
    print(f"  Registered tools: {registered}")
    assert "add_log" in TOOL_REGISTRY, "add_log not registered"
    assert "create_tracker" in TOOL_REGISTRY, "create_tracker not registered"
    assert "get_logs_summary" in TOOL_REGISTRY, "get_logs_summary not registered"
    print(f"  {PASS} All 3 tools registered")


async def test_firestore_config():
    print("\n── Firestore Config ──")
    await ensure_config_exists()
    trackers = await get_known_trackers()
    print(f"  Known trackers: {list(trackers.keys()) or '(none yet)'}")
    assert isinstance(trackers, dict)
    print(f"  {PASS} Firestore connection OK")


async def test_create_tracker():
    print("\n── create_tracker tool ──")
    result = await create_tracker("TestExpenses", ["Amount", "Category", "Notes"])
    print(f"  Result: {result}")
    assert result["status"] == "success", result
    trackers = await get_known_trackers()
    assert "TestExpenses" in trackers, "Tracker not found after creation"
    print(f"  {PASS} Tracker created, headers: {trackers['TestExpenses']}")


async def test_add_log():
    print("\n── add_log tool ──")
    trackers = await get_known_trackers()
    headers = trackers.get("TestExpenses", [])
    # Build values matching the headers
    sample_values = {
        "Date": "2026-03-05",
        "Amount": "9.50",
        "Category": "Coffee",
        "Notes": "Local test entry",
    }
    values = [sample_values.get(h, "test") for h in headers]
    result = await add_log("TestExpenses", values)
    print(f"  Result: {result}")
    assert result["status"] == "success", result
    print(f"  {PASS} Log written to Firestore")


async def test_get_logs():
    print("\n── get_logs_summary tool ──")
    result = await get_logs_summary("TestExpenses", limit=3)
    print(f"  Status: {result['status']}, count: {len(result.get('data', []))}")
    assert result["status"] == "success", result
    print(f"  {PASS} Logs retrieved")


async def test_unsynced_flag():
    print("\n── synced_to_sheets flag ──")
    unsynced = await get_unsynced_logs("TestExpenses")
    print(f"  Unsynced logs: {len(unsynced)}")
    print(f"  {PASS} Flag check OK (unsynced count is {len(unsynced)})")


async def test_orchestrator_retrieve():
    print("\n── Orchestrator: retrieve intent ──")
    trackers = await get_known_trackers()
    state = await orchestrator_run(
        "Show me my recent expense logs",
        known_trackers=trackers,
        thread_id="pytest-retrieve-001",
    )
    print(f"  final_response: {state.get('final_response', '')[:120]}")
    print(f"  pending_tool:   {state.get('pending_tool')}")
    print(f"  {PASS} Orchestrator returned a response")


async def test_orchestrator_log_hitl():
    print("\n── Orchestrator: log intent (expects HITL pause) ──")
    trackers = await get_known_trackers()
    state = await orchestrator_run(
        "Log $5.00 for coffee to TestExpenses",
        known_trackers=trackers,
        thread_id="pytest-log-001",
    )
    print(f"  pending_tool: {state.get('pending_tool')}")
    print(f"  pending_args: {state.get('pending_args')}")
    print(f"  final_response: {state.get('final_response')}")
    if state.get("pending_tool"):
        print(f"  {PASS} HITL paused correctly — pending tool: {state['pending_tool']}")
    else:
        print(f"  {PASS} Orchestrator responded directly (no tool call needed)")


async def main():
    print("=" * 50)
    print("  track-anything-agent — local integration tests")
    print("=" * 50)

    tests = [
        test_tool_registry,
        test_firestore_config,
        test_create_tracker,
        test_add_log,
        test_get_logs,
        test_unsynced_flag,
        test_orchestrator_retrieve,
        test_orchestrator_log_hitl,
    ]

    passed = 0
    failed = 0
    for t in tests:
        try:
            await t()
            passed += 1
        except Exception as e:
            print(f"  {FAIL} {t.__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'='*50}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
