"""
Terminal chat interface for track-anything-agent.

Bypasses Telegram entirely — drives the LangGraph orchestrator directly.
HITL confirmation is shown only for tracker creation; everything else executes immediately.

Usage (from project root):
    python tests/chat.py
"""
import sys
import asyncio
import warnings
import time
from datetime import datetime
from pathlib import Path

# Suppress noisy Pydantic v2 serialization warnings from LiteLLM internals
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")

# Ensure the project root is on the path so `src` imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.agent import orchestrator
from src.tools.firestore_tool import ensure_config_exists, get_known_trackers
from src.utils.logger import get_logger

logger = get_logger(__name__)

THREAD_ID = "cli-test-session"
DIVIDER = "─" * 50


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def print_agent(text: str, elapsed: float):
    print(f"\n[{ts()}] 🤖  Agent ({elapsed:.1f}s): {text}\n")


def print_proposal(tool: str, args: dict):
    print(f"\n[{ts()}] {'─'*46}")
    print(f"  Proposed action : {tool}")
    print(f"  Arguments       : {args}")
    print(f"{'─'*50}")


async def chat_loop():
    print(f"\n{DIVIDER}")
    print("  Track Anything — Terminal Chat")
    print(f"  Thread : {THREAD_ID}")
    print(f"  Started: {ts()}")
    print(f"{DIVIDER}")
    print("  Type your message. Ctrl+C to quit.\n")

    await ensure_config_exists()

    conversation_history: list[dict] = []

    while True:
        try:
            user_input = input(f"[{ts()}] You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if not user_input:
            continue

        # Refresh trackers each turn (in case a new one was just created)
        known_trackers = await get_known_trackers()

        t0 = time.monotonic()
        state = await orchestrator.run(user_input, known_trackers, THREAD_ID, conversation_history)

        # ── HITL checkpoint (create_tracker only) ─────────────────────────
        if state.get("pending_tool") and state.get("confirmed") is None:
            llm_elapsed = time.monotonic() - t0
            print_proposal(state["pending_tool"], state.get("pending_args", {}))
            print(f"  (LLM took {llm_elapsed:.1f}s to propose this)")
            try:
                answer = input(f"[{ts()}]   Proceed? [y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye.")
                break

            confirmed = answer in ("y", "yes")
            t1 = time.monotonic()
            state = await orchestrator.resume(THREAD_ID, confirmed)
            exec_elapsed = time.monotonic() - t1
            total_elapsed = time.monotonic() - t0
            reply = state.get("final_response") or ("Cancelled." if not confirmed else "Done.")
            print_agent(reply, total_elapsed)
            print(f"  ↳ tool exec + LLM follow-up: {exec_elapsed:.1f}s")
        else:
            elapsed = time.monotonic() - t0
            reply = state.get("final_response") or "Done."
            print_agent(reply, elapsed)

        # Carry conversation history into the next turn
        conversation_history = state.get("conversation_history", conversation_history)


if __name__ == "__main__":
    asyncio.run(chat_loop())
