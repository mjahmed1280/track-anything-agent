"""
Automated agent test runner.

Runs a predefined suite of scenarios against the orchestrator directly
(no Telegram, no human input). Auto-confirms create_tracker HITL prompts.
Writes a full timestamped log to tests/user-logs.txt and a machine-readable
JSON report to tests/test-report.json.

Usage (from project root):
    python tests/test_agent.py

Environment variables:
    FULL_ACCESS_SA_JSON   Path to GCP service-account JSON with Firestore access.
                          Falls back to the developer's local path if unset.
    TEST_TEARDOWN_LOGS    Set to "1" to also delete log documents for test
                          trackers on teardown (default: config-only deletion).

WARNING: Teardown removes system/config entries for the tracker names listed in
_TEST_TRACKERS ("Expenses", "Food Intake", "Sleep"). If your production
Firestore database uses those same tracker names, DO NOT run this suite against
it — or rename _TEST_TRACKERS entries to match unique test-only names.
"""
import json
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

# ── Credentials and env config — BEFORE any Google Cloud imports ─────────────
from dotenv import load_dotenv
load_dotenv()

# Temperature=0 gives deterministic LLM output during test runs.
# LiteLLM reads LITELLM_DEFAULT_TEMPERATURE automatically.
os.environ.setdefault("LITELLM_DEFAULT_TEMPERATURE", "0")

# Full-access SA for Firestore; override via env var for CI / other machines.
_sa_path = os.getenv(
    "FULL_ACCESS_SA_JSON",
    r"C:\Users\Jakaria.Ahmed\.gemini\antigravity\agent-gcp-sa-full-access.json",
)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _sa_path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import orchestrator
from src.tools.firestore_tool import db, ensure_config_exists, get_known_trackers
from src.utils.logger import get_logger

logger = get_logger(__name__)

LOG_FILE = Path(__file__).parent / "user-logs.txt"
JSON_REPORT_FILE = Path(__file__).parent / "test-report.json"

# Max retry attempts for non-HITL tests that fail (LLM non-determinism mitigation).
# HITL tests (create_tracker) are never retried — they mutate Firestore state.
MAX_RETRIES = 2

# Tracker names created by this test suite — removed from system/config on teardown.
_TEST_TRACKERS = ["Expenses", "Food Intake", "Sleep"]

# Also delete log documents on teardown (default off — safer for shared DBs).
_TEARDOWN_LOGS = os.getenv("TEST_TEARDOWN_LOGS", "0") == "1"


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
    depends_on: str | None = None           # ID of test that must have PASSED for this to run
    expect_values_count: int | None = None  # expected len(values) for add_log tool calls


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
        expect_values_count=5,   # [Date, Item, Amount, Category, Notes]
        depends_on="T02",
    ),
    TestCase(
        id="T04",
        description="Context memory: show logs without naming tracker",
        user_input="show me recent logs",
        expect_tool="get_logs_summary",
        expect_tracker="Expenses",
        expect_in_response=["350", "groceries", "dmart"],
        depends_on="T02",
    ),
    TestCase(
        id="T05",
        description="Log another expense with natural language date",
        user_input="also add coffee 80 rupees this morning",
        expect_tool="add_log",
        expect_tracker="Expenses",
        expect_values_count=5,   # [Date, Item, Amount, Category, Notes]
        depends_on="T02",
    ),
    TestCase(
        id="T06",
        description="Create a second tracker — Food Intake (HITL expected)",
        user_input="create a Food Intake tracker with columns Meal, Calories, Time. Track what I eat each day.",
        expect_tool="create_tracker",
        expect_tracker="Food Intake",
        confirm_hitl=True,
        depends_on="T02",
    ),
    TestCase(
        id="T07",
        description="Log to second tracker — auto-detect from context",
        user_input="had dal rice for lunch today around 1pm, about 450 calories",
        expect_tool="add_log",
        expect_tracker="Food Intake",
        expect_values_count=4,   # [Date, Meal, Calories, Time]
        depends_on="T06",
    ),
    TestCase(
        id="T08",
        description="Context switch: query Expenses after Food Intake turn",
        user_input="how much have I spent total?",
        expect_tool="get_logs_summary",
        expect_tracker="Expenses",
        depends_on="T02",
    ),
    TestCase(
        id="T09",
        description="Ambiguous input — agent should ask or infer sensibly",
        user_input="add 200",
        depends_on="T02",
    ),
    TestCase(
        id="T10",
        description="Decline tracker creation (cancel HITL)",
        user_input="create a Sleep tracker with columns Hours, Quality",
        expect_tool="create_tracker",
        confirm_hitl=False,
        expect_in_response=["cancel"],
        depends_on="T06",
    ),

    # ── Ambiguity tests ────────────────────────────────────────────────────

    TestCase(
        id="T11",
        description="Ambiguity: bare 'log' with no info — agent must ask for clarification",
        user_input="log",
        depends_on="T06",
    ),
    TestCase(
        id="T12",
        description="Ambiguity: reference non-existent tracker (Sleep was cancelled)",
        user_input="what's in my Sleep tracker?",
        # Sleep was never created; agent should say it doesn't exist (no tool call)
        expect_in_response=["sleep"],
        depends_on="T10",
    ),
    TestCase(
        id="T13",
        description="Ambiguity: cross-tracker conflict — calorie+cost in one sentence",
        user_input="I had biryani for lunch (600 calories) and it cost me 180 rupees",
        # Could be Food Intake or Expenses or both; agent must pick one or ask
        # We just observe — no strict tool/tracker assertion
        depends_on="T06",
    ),
    TestCase(
        id="T14",
        description="Ambiguity: numeric input with no clear tracker after multi-tracker setup",
        user_input="add 500 to the tracker",
        # With two trackers live the agent should ask which one or seek context
        depends_on="T06",
    ),

    # ── Extensive functional tests ─────────────────────────────────────────

    TestCase(
        id="T15",
        description="Explicit tracker name in query overrides active context",
        user_input="show me my Food Intake logs",
        expect_tool="get_logs_summary",
        expect_tracker="Food Intake",
        depends_on="T06",
    ),
    TestCase(
        id="T16",
        description="Multi-field natural-language log → Food Intake",
        user_input="yogurt and banana for breakfast this morning, roughly 280 calories, around 8am",
        expect_tool="add_log",
        expect_tracker="Food Intake",
        expect_values_count=4,   # [Date, Meal, Calories, Time]
        depends_on="T06",
    ),
    TestCase(
        id="T17",
        description="Explicit tracker override in log statement → Expenses",
        user_input="add to Expenses: petrol 600 rupees this evening",
        expect_tool="add_log",
        expect_tracker="Expenses",
        expect_values_count=5,   # [Date, Item, Amount, Category, Notes]
        depends_on="T02",
    ),
    TestCase(
        id="T18",
        description="Informal/colloquial language expense log",
        user_input="bought milk 60 bucks just now",
        expect_tool="add_log",
        expect_tracker="Expenses",
        expect_values_count=5,   # [Date, Item, Amount, Category, Notes]
        depends_on="T02",
    ),
    TestCase(
        id="T19",
        description="Query with explicit high limit — Food Intake last 20 entries",
        user_input="give me the last 20 entries from my Food Intake tracker",
        expect_tool="get_logs_summary",
        expect_tracker="Food Intake",
        depends_on="T06",
    ),
    TestCase(
        id="T20",
        description="List all trackers — agent should name both live trackers, not Sleep",
        user_input="what trackers do I have and what are they for?",
        # Direct LLM response; no tool call, must mention both active trackers
        expect_in_response=["Expenses", "Food"],
        depends_on="T06",
    ),

    # ── Negative tests ─────────────────────────────────────────────────────

    TestCase(
        id="T21",
        description="Negative: completely off-topic input — agent must NOT call any tool",
        user_input="The weather is really nice today, isn't it?",
        # expect_tool=None (default) — passes as long as no tool call is made
    ),
]


# ── Firestore lifecycle ───────────────────────────────────────────────────────

async def teardown_test_state(thread_id: str) -> None:
    """Remove test-created Firestore state so each run starts clean.

    By default only removes system/config entries for _TEST_TRACKERS.
    Set TEST_TEARDOWN_LOGS=1 to also delete log documents (useful for
    hard-reset; avoid in shared databases with real data under the same names).

    Also deletes the LangGraph checkpoint subcollections for thread_id.
    """
    loop = asyncio.get_event_loop()

    def _delete_subcollection(col_ref):
        for doc in col_ref.stream():
            doc.reference.delete()

    def _run_teardown():
        # 1. Remove tracker config entries for test-owned tracker names
        config_ref = db.collection("system").document("config")
        config_doc = config_ref.get()
        if config_doc.exists:
            data = config_doc.to_dict()
            trackers = data.get("trackers", {})
            descriptions = data.get("tracker_descriptions", {})
            changed = False
            for name in _TEST_TRACKERS:
                if name in trackers:
                    del trackers[name]
                    changed = True
                if name in descriptions:
                    del descriptions[name]
                    changed = True
            if changed:
                config_ref.set(
                    {"trackers": trackers, "tracker_descriptions": descriptions},
                    merge=True,
                )

        # 2. Optionally delete log documents and stats
        if _TEARDOWN_LOGS:
            for name in _TEST_TRACKERS:
                logs_col = db.collection("trackers").document(name).collection("logs")
                _delete_subcollection(logs_col)
                stats_ref = db.collection("stats").document(name)
                if stats_ref.get().exists:
                    stats_ref.delete()

        # 3. Delete LangGraph checkpoint subcollections for this thread
        cp_doc = db.collection("langgraph_checkpoints").document(thread_id)
        _delete_subcollection(cp_doc.collection("checkpoints"))
        _delete_subcollection(cp_doc.collection("writes"))

    await loop.run_in_executor(None, _run_teardown)
    logger.info(f"Teardown complete — thread={thread_id}, trackers={_TEST_TRACKERS}, logs={_TEARDOWN_LOGS}")


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
    tool_args: dict | None = None   # raw args passed to the tool — for values-count validation
    skipped: bool = False
    flaky: bool = False     # True if passed only after ≥1 retry
    attempts: int = 1
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
                 response: str, hitl_fired: bool, hitl_confirmed: bool | None,
                 tool_args: dict | None = None) -> tuple[bool, list[str]]:
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

    if case.expect_values_count is not None and tool_args:
        actual = len(tool_args.get("values", []))
        if actual != case.expect_values_count:
            failures.append(
                f"values count: expected {case.expect_values_count}, got {actual}"
            )

    return len(failures) == 0, failures


# ── Core test runner ─────────────────────────────────────────────────────────

async def run_test(
    case: TestCase,
    conversation_history: list[dict],
    last_active_tracker: str | None,
    thread_id: str,
) -> tuple[TestResult, list[dict], str | None]:
    tool_called = None
    tracker_used = None
    tool_args = None
    hitl_fired = False
    hitl_confirmed = None
    error = None
    elapsed = 0.0
    response = ""
    updated_history = conversation_history
    updated_tracker = last_active_tracker

    started_at = ts_full()
    t0 = time.monotonic()
    try:
        known_trackers = await get_known_trackers()
        state = await orchestrator.run(
            case.user_input, known_trackers, thread_id, conversation_history, last_active_tracker
        )

        if state.get("pending_tool") and state.get("confirmed") is None:
            # HITL checkpoint hit (expected only for create_tracker)
            hitl_fired = True
            tool_called = state["pending_tool"]
            tracker_used = extract_tracker(state)
            tool_args = state.get("pending_args")   # capture before resume clears them
            hitl_confirmed = case.confirm_hitl
            state = await orchestrator.resume(thread_id, case.confirm_hitl)
        else:
            tool_called = state.get("last_tool_called")
            tracker_used = state.get("last_active_tracker")
            tool_args = state.get("last_tool_args")

        elapsed = time.monotonic() - t0
        response = state.get("final_response") or ""
        updated_history = state.get("conversation_history", conversation_history)
        updated_tracker = state.get("last_active_tracker", last_active_tracker)

    except Exception as e:
        elapsed = time.monotonic() - t0
        error = str(e)

    finished_at = ts_full()
    passed, failures = check_result(
        case, tool_called, tracker_used, response, hitl_fired, hitl_confirmed, tool_args
    )
    if error:
        passed = False
        failures.append(f"Exception: {error}")

    result = TestResult(
        case=case,
        elapsed=elapsed,
        tool_called=tool_called,
        tracker_used=tracker_used,
        tool_args=tool_args,
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
    if result.skipped:
        status = "SKIP"
    elif result.passed:
        status = "PASS" + (" [FLAKY]" if result.flaky else "")
    else:
        status = "FAIL"

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
        f"  Attempts   : {result.attempts}",
        f"  Tool called: {result.tool_called or '(none — direct LLM response)'}",
        f"  Tracker    : {result.tracker_used or '(n/a)'}",
        f"  HITL fired : {result.hitl_fired}{hitl_info}",
        f"  Elapsed    : {result.elapsed:.1f}s",
    ]
    if result.tool_args and result.tool_called == "add_log":
        values = result.tool_args.get("values", [])
        lines.append(f"  Values ({len(values)}) : {values}")
    lines += [
        f"  Response:",
        f"    {result.response}",
    ]
    if result.skipped:
        lines.append(f"  SKIPPED: dependency '{result.case.depends_on}' did not pass")
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
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    flaky = sum(1 for r in results if r.flaky)
    runnable = total - skipped
    avg_elapsed = sum(r.elapsed for r in results) / total if total else 0

    lines = [
        "",
        "=" * 60,
        "  TEST SUMMARY",
        "=" * 60,
        f"  Total      : {total}",
        f"  Passed     : {passed} [OK]",
        f"  Failed     : {failed}",
        f"  Skipped    : {skipped}",
        f"  Flaky      : {flaky} (passed on retry)",
        f"  Pass rate  : {round(passed / runnable * 100)}% of runnable tests" if runnable else "  Pass rate  : n/a",
        f"  Avg latency: {avg_elapsed:.1f}s",
        "",
        "  Results by test:",
    ]
    for r in results:
        if r.skipped:
            icon = "[--]"
        elif r.passed:
            icon = "[OK]~" if r.flaky else "[OK] "
        else:
            icon = "[!!] "
        note = f"  [{r.failures[0]}]" if r.failures and not r.skipped else ""
        lines.append(f"    {icon} {r.case.id}: {r.case.description[:45]:<45} ({r.elapsed:.1f}s){note}")

    lines += ["", "  ANALYSIS:"]

    non_skipped = [r for r in results if not r.skipped]
    hitl_cases = [r for r in non_skipped if r.hitl_fired]
    tool_cases = [r for r in non_skipped if r.tool_called]
    direct_cases = [r for r in non_skipped if not r.tool_called and not r.error]
    error_cases = [r for r in non_skipped if r.error]

    lines.append(f"    • Tool calls      : {len(tool_cases)}/{len(non_skipped)}")
    lines.append(f"    • HITL prompts    : {len(hitl_cases)} (should all be create_tracker)")
    lines.append(f"    • Direct replies  : {len(direct_cases)}")
    lines.append(f"    • Errors          : {len(error_cases)}")

    wrong_hitl = [r for r in hitl_cases if r.tool_called != "create_tracker"]
    if wrong_hitl:
        lines.append(f"    [WW] HITL for non-create_tracker: {[r.tool_called for r in wrong_hitl]}")
    else:
        lines.append("    [OK] HITL only fired for create_tracker")

    slow = [r for r in non_skipped if r.elapsed > 15]
    if slow:
        lines.append(f"    [WW] Slow (>15s): {[r.case.id for r in slow]}")
    else:
        lines.append("    [OK] All responses under 15s")

    context_tests = [r for r in results if r.case.id in ("T04", "T07", "T08", "T15", "T19")]
    context_pass = [r for r in context_tests if r.passed]
    lines.append(f"    • Context-aware tests: {len(context_pass)}/{len(context_tests)} passed (T04,T07,T08,T15,T19)")

    ambig_tests = [r for r in results if r.case.id in ("T09", "T11", "T12", "T13", "T14")]
    ambig_pass = [r for r in ambig_tests if r.passed]
    lines.append(f"    • Ambiguity tests    : {len(ambig_pass)}/{len(ambig_tests)} passed (T09,T11,T12,T13,T14)")

    extensive_tests = [r for r in results if r.case.id in ("T15", "T16", "T17", "T18", "T19", "T20")]
    extensive_pass = [r for r in extensive_tests if r.passed]
    lines.append(f"    • Extensive tests    : {len(extensive_pass)}/{len(extensive_tests)} passed (T15-T20)")

    neg_tests = [r for r in results if r.case.id == "T21"]
    neg_pass = [r for r in neg_tests if r.passed]
    lines.append(f"    • Negative tests     : {len(neg_pass)}/{len(neg_tests)} passed (T21)")

    lines.append("=" * 60)
    return "\n".join(lines)


def write_json_report(results: list[TestResult], thread_id: str) -> None:
    """Write machine-readable JSON report for tracking pass% across runs."""
    total = len(results)
    passed = sum(1 for r in results if r.passed and not r.skipped)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    flaky = sum(1 for r in results if r.flaky)
    runnable = total - skipped

    report = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "thread_id": thread_id,
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "flaky": flaky,
        "pass_pct": round(passed / runnable * 100, 1) if runnable else 0,
        "results": [
            {
                "id": r.case.id,
                "description": r.case.description,
                "status": "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL"),
                "flaky": r.flaky,
                "attempts": r.attempts,
                "tool_called": r.tool_called,
                "tracker": r.tracker_used,
                "hitl_fired": r.hitl_fired,
                "elapsed_s": round(r.elapsed, 2),
                "failures": r.failures,
            }
            for r in results
        ],
    }
    JSON_REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    # Unique thread ID per run — prevents LangGraph checkpoint bleed between runs
    run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    thread_id = f"auto-test-{run_ts}"

    header = (
        f"\n{'='*60}\n"
        f"  Track Anything Agent — Automated Test Suite\n"
        f"  Run at : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  Thread : {thread_id}\n"
        f"  Cases  : {len(TEST_SUITE)} (T01-T10 core | T11-T14 ambiguity | T15-T20 extensive | T21 negative)\n"
        f"  Temp   : {os.environ.get('LITELLM_DEFAULT_TEMPERATURE', 'default')}\n"
        f"{'='*60}\n"
    )
    print(header)
    log_lines = [header]

    # ── Pre-run setup ────────────────────────────────────────────────────────
    await ensure_config_exists()
    print(f"[{ts()}] Tearing down previous test state (tracker config)...")
    try:
        await teardown_test_state(thread_id)
    except Exception as e:
        logger.warning(f"Pre-run teardown failed (non-fatal): {e}")

    conversation_history: list[dict] = []
    last_active_tracker: str | None = None
    results: list[TestResult] = []
    passed_ids: set[str] = set()   # IDs of tests that passed — used by depends_on checks

    for i, case in enumerate(TEST_SUITE, 1):

        # ── Dependency skip check ────────────────────────────────────────────
        if case.depends_on and case.depends_on not in passed_ids:
            print(f"[{ts()}] SKIP {case.id}: dependency '{case.depends_on}' did not pass")
            skipped_result = TestResult(
                case=case,
                elapsed=0.0,
                tool_called=None,
                tracker_used=None,
                response="",
                hitl_fired=False,
                hitl_confirmed=None,
                passed=False,
                failures=[],
                skipped=True,
                started_at=ts_full(),
                finished_at=ts_full(),
            )
            results.append(skipped_result)
            block = format_result_block(skipped_result)
            print(block)
            log_lines.append(block)
            continue

        # ── Run with retry ───────────────────────────────────────────────────
        print(f"[{ts()}] Running {case.id} ({i}/{len(TEST_SUITE)}): {case.description}")

        # HITL tests (create_tracker) mutate persistent state — never retry them
        max_attempts = 1 if case.expect_tool == "create_tracker" else MAX_RETRIES

        # Snapshot conversation state so retries start from the same point
        history_snapshot = conversation_history
        tracker_snapshot = last_active_tracker
        result = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                print(f"[{ts()}]   Retrying {case.id} (attempt {attempt}/{max_attempts})...")
                await asyncio.sleep(3)
                # Restore pre-attempt state so retry has a clean context
                conversation_history = history_snapshot
                last_active_tracker = tracker_snapshot

            attempt_result, new_history, new_tracker = await run_test(
                case, conversation_history, last_active_tracker, thread_id
            )
            attempt_result.attempts = attempt

            # Commit state update regardless — use latest on last attempt
            conversation_history = new_history
            last_active_tracker = new_tracker

            if attempt_result.passed:
                if attempt > 1:
                    attempt_result.flaky = True
                result = attempt_result
                break
            result = attempt_result   # keep last failed result if all retries exhausted

        block = format_result_block(result)
        print(block)
        log_lines.append(block)
        results.append(result)

        if result.passed:
            passed_ids.add(case.id)

        # Brief pause between tests to ease rate limits
        if i < len(TEST_SUITE):
            await asyncio.sleep(2)

    # ── Write reports first — before teardown so a crash there doesn't lose results ──
    summary = format_summary(results)
    print(summary)
    log_lines.append(summary)

    full_log = "\n".join(log_lines)
    LOG_FILE.write_text(full_log, encoding="utf-8")
    write_json_report(results, thread_id)
    print(f"\n  Full log saved to   : {LOG_FILE}")
    print(f"  JSON report saved to: {JSON_REPORT_FILE}")

    # ── Post-run teardown ────────────────────────────────────────────────────
    print(f"[{ts()}] Tearing down test state after run...")
    try:
        await teardown_test_state(thread_id)
    except Exception as e:
        logger.warning(f"Post-run teardown failed (non-fatal): {e}")


if __name__ == "__main__":
    asyncio.run(main())
