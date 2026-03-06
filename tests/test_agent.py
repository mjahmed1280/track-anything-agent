"""
Automated agent test runner.

Runs a predefined suite of scenarios against the orchestrator directly
(no Telegram, no human input). Auto-confirms create_tracker HITL prompts.
Writes a full timestamped log to tests/user-logs.txt and prints a pass/fail
analysis at the end.

Usage (from project root):
    python tests/test_agent.py
"""
import os
import sys
import asyncio
import warnings
import time
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# Force UTF-8 output on Windows terminals
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Credentials must be set BEFORE any Google Cloud imports ─────────────────
from dotenv import load_dotenv
load_dotenv()

# .env sets a Vertex AI SA (LLM only). Override with full-access SA
# that has Firestore permissions -- same approach as clear_data.py.
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"C:\Users\Jakaria.Ahmed\.gemini\antigravity\agent-gcp-sa-full-access.json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import orchestrator
from src.tools.firestore_tool import ensure_config_exists, get_known_trackers
from src.utils.logger import get_logger

logger = get_logger(__name__)

LOG_FILE = Path(__file__).parent / "user-logs.txt"
THREAD_ID = "auto-test-session"


# ── Test case definition ─────────────────────────────────────────────────────

@dataclass
class TestCase:
    id: str
    description: str
    user_input: str
    expect_tool: str | None = None
    expect_tracker: str | None = None
    expect_in_response: list[str] = field(default_factory=list)
    confirm_hitl: bool = True


TEST_SUITE: list[TestCase] = [
    TestCase(
        id="T01",
        description="Query trackers when database is empty",
        user_input="what trackers do we have?",
        expect_in_response=[],
    ),
    TestCase(
        id="T02",
        description="Create an Expenses tracker (HITL expected)",
        user_input="create an Expenses tracker with columns Item, Amount, Category, and Notes. Use it to track daily spending.",
        expect_tool="create_tracker",
        expect_tracker="Expenses",
        confirm_hitl=True,
    ),
    TestCase(
        id="T03",
        description="Log an expense without specifying tracker (auto-detect)",
        user_input="I spent 350 on groceries at DMart yesterday",
        expect_tool="add_log",
        expect_tracker="Expenses",
    ),
    TestCase(
        id="T04",
        description="Context memory: show logs without naming tracker",
        user_input="show me recent logs",
        expect_tool="get_logs_summary",
        expect_tracker="Expenses",
        expect_in_response=["350", "groceries", "dmart"],
    ),
    TestCase(
        id="T05",
        description="Log another expense with natural language date",
        user_input="also add coffee 80 rupees this morning",
        expect_tool="add_log",
        expect_tracker="Expenses",
    ),
    TestCase(
        id="T06",
        description="Create a second tracker — Food Intake (HITL expected)",
        user_input="create a Food Intake tracker with columns Meal, Calories, Time. Track what I eat each day.",
        expect_tool="create_tracker",
        expect_tracker="Food Intake",
        confirm_hitl=True,
    ),
    TestCase(
        id="T07",
        description="Log to second tracker — auto-detect from context",
        user_input="had dal rice for lunch today around 1pm, about 450 calories",
        expect_tool="add_log",
        expect_tracker="Food Intake",
    ),
    TestCase(
        id="T08",
        description="Context switch: query Expenses after Food Intake turn",
        user_input="how much have I spent total?",
        expect_tool="get_logs_summary",
        expect_tracker="Expenses",
    ),
    TestCase(
        id="T09",
        description="Ambiguous input — agent should ask or infer sensibly",
        user_input="add 200",
    ),
    TestCase(
        id="T10",
        description="Decline tracker creation (cancel HITL)",
        user_input="create a Sleep tracker with columns Hours, Quality",
        expect_tool="create_tracker",
        confirm_hitl=False,
        expect_in_response=["cancel"],
    ),
]


# ── Result tracking ──────────────────────────────────────────────────────────

@dataclass
class TestResult:
    case: TestCase
    elapsed: float
    tool_called: str | None
    tracker_used: str | None
    response: str
    hitl_fired: bool
    hitl_confirmed: bool | None
    passed: bool
    failures: list[str]
    error: str | None = None
    started_at: str = ""
    finished_at: str = ""


# ── Helpers ──────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def ts_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_tracker(state: dict) -> str | None:
    args = state.get("pending_args") or {}
    return args.get("tracker_name")


def check_result(case: TestCase, tool_called: str | None, tracker_used: str | None,
                 response: str, hitl_fired: bool, hitl_confirmed: bool | None) -> tuple[bool, list[str]]:
    failures = []

    if case.expect_tool and tool_called != case.expect_tool:
        failures.append(f"expect_tool={case.expect_tool!r}, got={tool_called!r}")

    if case.expect_tracker and tracker_used:
        if case.expect_tracker.lower() not in tracker_used.lower():
            failures.append(f"expect_tracker={case.expect_tracker!r}, got={tracker_used!r}")
    elif case.expect_tracker and not tracker_used:
        failures.append(f"expect_tracker={case.expect_tracker!r}, but no tracker detected")

    resp_lower = response.lower()
    for substring in case.expect_in_response:
        if substring.lower() not in resp_lower:
            failures.append(f"response missing {substring!r}")

    return len(failures) == 0, failures


# ── Core test runner ─────────────────────────────────────────────────────────

async def run_test(
    case: TestCase, conversation_history: list[dict], last_active_tracker: str | None
) -> tuple[TestResult, list[dict], str | None]:
    tool_called = None
    tracker_used = None
    hitl_fired = False
    hitl_confirmed = None
    error = None
    elapsed = 0.0
    response = ""
    updated_history = conversation_history
    updated_tracker = last_active_tracker

    started_at = ts_full()
    try:
        known_trackers = await get_known_trackers()
        t0 = time.monotonic()
        state = await orchestrator.run(
            case.user_input, known_trackers, THREAD_ID, conversation_history, last_active_tracker
        )

        if state.get("pending_tool") and state.get("confirmed") is None:
            # HITL checkpoint hit (expected only for create_tracker)
            hitl_fired = True
            tool_called = state["pending_tool"]
            tracker_used = extract_tracker(state)
            hitl_confirmed = case.confirm_hitl
            state = await orchestrator.resume(THREAD_ID, case.confirm_hitl)
        else:
            # Auto-executed tool or direct LLM response
            # pending_tool/pending_args are cleared after execution — read the preserved fields
            tool_called = state.get("last_tool_called")
            tracker_used = state.get("last_active_tracker")

        elapsed = time.monotonic() - t0
        response = state.get("final_response") or ""
        updated_history = state.get("conversation_history", conversation_history)
        updated_tracker = state.get("last_active_tracker", last_active_tracker)

    except Exception as e:
        elapsed = time.monotonic() - (t0 if 't0' in dir() else time.monotonic())
        error = str(e)

    finished_at = ts_full()
    passed, failures = check_result(case, tool_called, tracker_used, response, hitl_fired, hitl_confirmed)
    if error:
        passed = False
        failures.append(f"Exception: {error}")

    result = TestResult(
        case=case,
        elapsed=elapsed,
        tool_called=tool_called,
        tracker_used=tracker_used,
        response=response,
        hitl_fired=hitl_fired,
        hitl_confirmed=hitl_confirmed,
        passed=passed,
        failures=failures,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
    )
    return result, updated_history, updated_tracker


# ── Log formatting ───────────────────────────────────────────────────────────

def format_result_block(result: TestResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    hitl_info = ""
    if result.hitl_fired:
        hitl_info = f" → {'confirmed [OK]' if result.hitl_confirmed else 'cancelled [!!]'}"
    lines = [
        f"{'='*60}",
        f"[{status}] {result.case.id}: {result.case.description}",
        f"{'='*60}",
        f"  Started    : {result.started_at}",
        f"  Finished   : {result.finished_at}",
        f"  Input      : {result.case.user_input}",
        f"  Tool called: {result.tool_called or '(none — direct LLM response)'}",
        f"  Tracker    : {result.tracker_used or '(n/a)'}",
        f"  HITL fired : {result.hitl_fired}{hitl_info}",
        f"  Elapsed    : {result.elapsed:.1f}s",
        f"  Response:",
        f"    {result.response}",
    ]
    if result.failures:
        lines.append("  FAILURES:")
        for f in result.failures:
            lines.append(f"    [!!] {f}")
    if result.error:
        lines.append(f"  ERROR: {result.error}")
    lines.append("")
    return "\n".join(lines)


def format_summary(results: list[TestResult]) -> str:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    avg_elapsed = sum(r.elapsed for r in results) / total if total else 0

    lines = [
        "",
        "=" * 60,
        "  TEST SUMMARY",
        "=" * 60,
        f"  Total      : {total}",
        f"  Passed     : {passed} [OK]",
        f"  Failed     : {failed}",
        f"  Avg latency: {avg_elapsed:.1f}s",
        "",
        "  Results by test:",
    ]
    for r in results:
        icon = "[OK]" if r.passed else "[!!]"
        note = f"  [{r.failures[0]}]" if r.failures else ""
        lines.append(f"    {icon} {r.case.id}: {r.case.description[:45]:<45} ({r.elapsed:.1f}s){note}")

    lines += ["", "  ANALYSIS:"]

    hitl_cases = [r for r in results if r.hitl_fired]
    tool_cases = [r for r in results if r.tool_called]
    direct_cases = [r for r in results if not r.tool_called and not r.error]
    error_cases = [r for r in results if r.error]

    lines.append(f"    • Tool calls      : {len(tool_cases)}/{total}")
    lines.append(f"    • HITL prompts    : {len(hitl_cases)} (should all be create_tracker)")
    lines.append(f"    • Direct replies  : {len(direct_cases)}")
    lines.append(f"    • Errors          : {len(error_cases)}")

    wrong_hitl = [r for r in hitl_cases if r.tool_called != "create_tracker"]
    if wrong_hitl:
        lines.append(f"    [WW] HITL for non-create_tracker: {[r.tool_called for r in wrong_hitl]}")
    else:
        lines.append("    [OK] HITL only fired for create_tracker")

    slow = [r for r in results if r.elapsed > 15]
    if slow:
        lines.append(f"    [WW] Slow (>15s): {[r.case.id for r in slow]}")
    else:
        lines.append("    [OK] All responses under 15s")

    context_tests = [r for r in results if r.case.id in ("T04", "T07", "T08")]
    context_pass = [r for r in context_tests if r.passed]
    lines.append(f"    • Context-aware tests: {len(context_pass)}/{len(context_tests)} passed (T04, T07, T08)")

    lines.append("=" * 60)
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    header = (
        f"\n{'='*60}\n"
        f"  Track Anything Agent — Automated Test Suite\n"
        f"  Run at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  Thread : {THREAD_ID}\n"
        f"  Cases  : {len(TEST_SUITE)}\n"
        f"{'='*60}\n"
    )

    print(header)
    log_lines = [header]

    await ensure_config_exists()
    conversation_history: list[dict] = []
    last_active_tracker: str | None = None
    results: list[TestResult] = []

    for i, case in enumerate(TEST_SUITE, 1):
        print(f"[{ts()}] Running {case.id} ({i}/{len(TEST_SUITE)}): {case.description}")
        result, conversation_history, last_active_tracker = await run_test(
            case, conversation_history, last_active_tracker
        )
        results.append(result)

        block = format_result_block(result)
        print(block)
        log_lines.append(block)

        # Brief pause between tests to ease rate limits
        if i < len(TEST_SUITE):
            await asyncio.sleep(2)

    summary = format_summary(results)
    print(summary)
    log_lines.append(summary)

    full_log = "\n".join(log_lines)
    LOG_FILE.write_text(full_log, encoding="utf-8")
    print(f"\n  Full log saved to: {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
