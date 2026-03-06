"""
Orchestrator: LangGraph-based workflow with Human-in-the-Loop (HITL) checkpoint.

Flow:
  parse_intent → [HITL confirm checkpoint — create_tracker only] → execute_tool → respond

For tools that do NOT require confirmation (add_log, get_logs_summary) the graph
routes directly to execute_tool without pausing.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import litellm
from langgraph.graph import StateGraph, END

from src.agent.prompts import get_system_prompt, filter_trackers_for_input
from src.agent.registry import get_tool_schemas_for_litellm, execute_tool
from src.tools.firestore_tool import db, get_tracker_descriptions
from src.utils.firestore_checkpointer import FirestoreCheckpointer
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

_LLM_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "tests" / "llm-logs.txt"


def _log_llm_call(model: str, messages: list, response=None, error: Exception | None = None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"\n{'='*60}", f"[{ts}]  model={model}"]
    lines.append("--- REQUEST ---")
    for m in messages:
        role = m.get("role", "?") if isinstance(m, dict) else getattr(m, "role", "?")
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        tool_calls = None if isinstance(m, dict) else getattr(m, "tool_calls", None)
        if tool_calls:
            tc_list = []
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    tc_list.append({"name": fn.get("name", "?"), "args": fn.get("arguments", "{}")})
                else:
                    tc_list.append({"name": tc.function.name, "args": tc.function.arguments})
            content = f"[tool_calls] {json.dumps(tc_list, indent=2)}"
        lines.append(f"  [{role}] {str(content)[:500]}")
    lines.append("--- RESPONSE ---")
    if error:
        lines.append(f"  ERROR: {error}")
    elif response:
        msg = response.choices[0].message
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    lines.append(f"  [tool_call] {fn.get('name', '?')}({fn.get('arguments', '{}')})")
                else:
                    lines.append(f"  [tool_call] {tc.function.name}({tc.function.arguments})")
        else:
            lines.append(f"  [assistant] {str(msg.content)[:1000]}")
    try:
        os.makedirs(_LLM_LOG_PATH.parent, exist_ok=True)
        with open(_LLM_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as log_err:
        logger.warning(f"Failed to write llm-logs.txt: {log_err}")


# Model fallback chain — tried in order on RateLimitError / ServiceUnavailable / BadRequest
# Uses Vertex AI with the GCP service account (GOOGLE_APPLICATION_CREDENTIALS)
# gemini-2.5-flash-lite excluded — returns 400 "function response parts" on Vertex AI
FALLBACK_MODELS = [
    "vertex_ai/gemini-2.0-flash",
    "vertex_ai/gemini-2.5-flash",
    "vertex_ai/gemini-2.5-pro",
]

# Only these tools pause for user confirmation before executing
TOOLS_REQUIRING_CONFIRMATION = {"create_tracker"}

# Cap conversation history to avoid bloating the context window
MAX_HISTORY_MESSAGES = 6       # older turns are condensed into state_summary
SUMMARIZE_THRESHOLD = 10       # trigger summarization when history exceeds this

# Phrases that indicate the assistant previously claimed no trackers existed
_NO_TRACKER_PHRASES = [
    "no trackers",
    "haven't created",
    "don't have any tracker",
    "no tracker exists",
    "haven't set up",
    "no trackers yet",
]


def call_llm(messages: list, tools: list | None = None, tool_choice: str | None = None):
    """Call LiteLLM with automatic model fallback on rate limit errors."""
    last_error = None
    for i, model in enumerate(FALLBACK_MODELS):
        try:
            response = litellm.completion(
                model=model,
                messages=messages,
                tools=tools or None,
                tool_choice=tool_choice,
                vertex_project=settings.GOOGLE_CLOUD_PROJECT,
                vertex_location=settings.VERTEX_LOCATION,
            )
            _log_llm_call(model, messages, response=response)
            return response
        except (litellm.RateLimitError, litellm.ServiceUnavailableError, litellm.BadRequestError) as e:
            _log_llm_call(model, messages, error=e)
            last_error = e
            if i < len(FALLBACK_MODELS) - 1:
                next_model = FALLBACK_MODELS[i + 1]
                logger.warning(f"Rate limit / unavailable on {model}, falling back to {next_model}")
            else:
                logger.error(f"All models exhausted. Last error: {e}")
    raise last_error


class AgentState(TypedDict):
    user_input: str
    known_trackers: dict
    tracker_descriptions: dict          # {name: description string}
    conversation_history: list[dict]    # cross-turn [user/assistant] message pairs (recent only)
    state_summary: str                  # condensed summary of older conversation history
    messages: list[dict]                # LiteLLM message history for current turn
    pending_tool: str | None            # tool name waiting for HITL confirmation
    pending_tool_call_id: str | None    # real tool_call id from the LLM response
    pending_args: dict | None           # tool args waiting for confirmation
    confirmed: bool | None              # None = not yet decided, True/False = user decided
    final_response: str | None
    last_tool_called: str | None        # set by execute_tool_node so test inspector can read it
    last_active_tracker: str | None     # most recently used tracker — sticky context across turns


def _summarize_history(history: list[dict]) -> str:
    """Condense old conversation turns into a short summary string."""
    messages = [
        {
            "role": "system",
            "content": (
                "Summarize the key tracking activities in this conversation in 2-3 sentences. "
                "Focus on what was logged, what trackers were used, and any important context. "
                "Be concise — this summary will replace the full history to save context space."
            ),
        },
        *history,
    ]
    resp = call_llm(messages)
    return resp.choices[0].message.content or ""


def _build_corrections(known_trackers: dict, recent_history: list[dict]) -> list[dict]:
    """If the assistant previously claimed no trackers exist but they do now,
    inject a system correction message to override that stale belief."""
    if not known_trackers:
        return []
    for msg in reversed(recent_history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "").lower()
            if any(phrase in content for phrase in _NO_TRACKER_PHRASES):
                names = list(known_trackers.keys())
                return [{
                    "role": "system",
                    "content": (
                        f"[System Correction] Trackers now exist: {names}. "
                        "Disregard any earlier claims of empty tracker state."
                    ),
                }]
    return []


def parse_intent_node(state: AgentState) -> AgentState:
    """Call LLM with tools. If it returns a tool_call, stage it for confirmation.

    On resume after HITL, confirmed is already set — skip LLM and pass state
    through so needs_confirmation can route directly to execute_tool.
    """
    if state.get("pending_tool") and state.get("confirmed") is not None:
        return state

    # 1. Prune old history into a summary if it has grown too long
    history = state.get("conversation_history", [])
    summary = state.get("state_summary", "")
    if len(history) > SUMMARIZE_THRESHOLD:
        summary = _summarize_history(history[:-MAX_HISTORY_MESSAGES])
        history = history[-MAX_HISTORY_MESSAGES:]

    # 2. Filter trackers to only those relevant to this user_input
    filtered_trackers = filter_trackers_for_input(
        state["user_input"],
        state["known_trackers"],
        state.get("tracker_descriptions", {}),
        state.get("last_active_tracker"),
    )

    # 3. Build system prompt with filtered trackers + optional summary
    system_prompt = get_system_prompt(
        filtered_trackers,
        state.get("tracker_descriptions", {}),
        state.get("last_active_tracker"),
        summary or None,
    )

    # 4. Build messages: system + recent history + stale-state correction + user turn
    recent_history = history[-MAX_HISTORY_MESSAGES:]
    corrections = _build_corrections(state["known_trackers"], recent_history)
    messages = (
        [{"role": "system", "content": system_prompt}]
        + recent_history
        + corrections
        + [{"role": "user", "content": state["user_input"]}]
    )

    tools = get_tool_schemas_for_litellm()
    response = call_llm(messages, tools=tools if tools else None, tool_choice="auto" if tools else None)

    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None)

    if tool_calls:
        call = tool_calls[0]
        args = json.loads(call.function.arguments)
        logger.info(f"LLM wants to call tool: {call.function.name} with args: {args}")
        return {
            **state,
            "messages": messages + [message],
            "conversation_history": history,
            "state_summary": summary,
            "pending_tool": call.function.name,
            "pending_tool_call_id": call.id,
            "pending_args": args,
            "confirmed": None,
            "final_response": None,
        }

    # No tool call — LLM responded directly; update conversation history
    updated_history = history + [
        {"role": "user", "content": state["user_input"]},
        {"role": "assistant", "content": message.content},
    ]
    return {
        **state,
        "messages": messages,
        "conversation_history": updated_history[-MAX_HISTORY_MESSAGES:],
        "state_summary": summary,
        "pending_tool": None,
        "pending_args": None,
        "confirmed": None,
        "final_response": message.content,
    }


async def execute_tool_node(state: AgentState) -> AgentState:
    """Execute the tool and return result to the LLM for a final response.

    For tools in TOOLS_REQUIRING_CONFIRMATION, confirmed=False means cancelled.
    For auto-executed tools, confirmed is None — always run.
    """
    tool_name = state["pending_tool"]
    requires_confirmation = tool_name in TOOLS_REQUIRING_CONFIRMATION

    if requires_confirmation and state.get("confirmed") is False:
        updated_history = state.get("conversation_history", []) + [
            {"role": "user", "content": state["user_input"]},
            {"role": "assistant", "content": "Action cancelled."},
        ]
        return {
            **state,
            "final_response": "Action cancelled.",
            "conversation_history": updated_history[-MAX_HISTORY_MESSAGES:],
            "state_summary": state.get("state_summary", ""),
        }

    tracker_name = state["pending_args"].get("tracker_name", "")
    result = await execute_tool(tool_name, **state["pending_args"])
    logger.info(f"Tool result: {result}")

    # Use the real tool_call_id — Gemini validates this strictly
    tool_call_id = state.get("pending_tool_call_id") or "hitl_confirmed"
    follow_up_messages = state["messages"] + [
        {"role": "tool", "content": str(result), "tool_call_id": tool_call_id},
    ]
    follow_up = call_llm(follow_up_messages)
    final_response = follow_up.choices[0].message.content

    # Store clean user/assistant pair — no tool tag suffix, to prevent the LLM from
    # learning the pattern and mimicking tool responses without actually calling tools.
    # Tracker context is carried via last_active_tracker → ACTIVE CONTEXT in the system prompt.
    updated_history = state.get("conversation_history", []) + [
        {"role": "user", "content": state["user_input"]},
        {"role": "assistant", "content": final_response},
    ]

    # Carry the active tracker forward; fall back to whatever was active before
    active_tracker = tracker_name or state.get("last_active_tracker")

    return {
        **state,
        "final_response": final_response,
        "conversation_history": updated_history[-MAX_HISTORY_MESSAGES:],
        "state_summary": state.get("state_summary", ""),
        "last_tool_called": tool_name,
        "last_active_tracker": active_tracker,
        "pending_tool": None,
        "pending_args": None,
    }


def needs_confirmation(state: AgentState) -> str:
    pending = state.get("pending_tool")
    confirmed = state.get("confirmed")

    if pending and confirmed is None:
        if pending in TOOLS_REQUIRING_CONFIRMATION:
            return "await_confirmation"
        return "execute"   # auto-run everything else

    if pending and confirmed is not None:
        return "execute"

    return END


# Build the graph
_builder = StateGraph(AgentState)
_builder.add_node("parse_intent", parse_intent_node)
_builder.add_node("execute_tool", execute_tool_node)
_builder.set_entry_point("parse_intent")
_builder.add_conditional_edges("parse_intent", needs_confirmation, {
    "await_confirmation": END,   # Graph pauses; caller resumes after user confirms
    "execute": "execute_tool",
    END: END,
})
_builder.add_edge("execute_tool", END)

checkpointer = FirestoreCheckpointer(db)
graph = _builder.compile(checkpointer=checkpointer)


async def run(
    user_input: str,
    known_trackers: dict,
    thread_id: str,
    conversation_history: list[dict] | None = None,
    last_active_tracker: str | None = None,
) -> AgentState:
    """Start a new turn in the workflow thread."""
    tracker_descriptions = await get_tracker_descriptions()
    config = {"configurable": {"thread_id": thread_id}}
    initial_state: AgentState = {
        "user_input": user_input,
        "known_trackers": known_trackers,
        "tracker_descriptions": tracker_descriptions,
        "conversation_history": conversation_history or [],
        "state_summary": "",
        "messages": [],
        "pending_tool": None,
        "pending_tool_call_id": None,
        "pending_args": None,
        "confirmed": None,
        "final_response": None,
        "last_tool_called": None,
        "last_active_tracker": last_active_tracker,
    }
    result = await graph.ainvoke(initial_state, config=config)
    return result


async def resume(thread_id: str, confirmed: bool) -> AgentState:
    """Resume a paused workflow after the user confirms or cancels."""
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke({"confirmed": confirmed}, config=config)
    return result
