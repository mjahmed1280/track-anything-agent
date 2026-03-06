"""
Orchestrator: LangGraph-based workflow with Human-in-the-Loop (HITL) checkpoint.

Flow:
  parse_intent → [HITL confirm checkpoint — create_tracker only] → execute_tool → respond

For tools that do NOT require confirmation (add_log, get_logs_summary) the graph
routes directly to execute_tool without pausing.
"""
import json
from typing import TypedDict

import litellm
from langgraph.graph import StateGraph, END

from src.agent.prompts import get_system_prompt
from src.agent.registry import get_tool_schemas_for_litellm, execute_tool
from src.tools.firestore_tool import db, get_tracker_descriptions
from src.utils.firestore_checkpointer import FirestoreCheckpointer
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Model fallback chain — tried in order on RateLimitError
FALLBACK_MODELS = [
    "gemini/gemini-2.5-flash-lite",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-3.1-flash-lite-preview",
    "gemini/gemini-2.0-flash",
]

# Only these tools pause for user confirmation before executing
TOOLS_REQUIRING_CONFIRMATION = {"create_tracker"}

# Cap conversation history to avoid bloating the context window
MAX_HISTORY_MESSAGES = 20


def call_llm(messages: list, tools: list | None = None, tool_choice: str | None = None):
    """Call LiteLLM with automatic model fallback on rate limit errors."""
    last_error = None
    for i, model in enumerate(FALLBACK_MODELS):
        try:
            return litellm.completion(
                model=model,
                messages=messages,
                tools=tools or None,
                tool_choice=tool_choice,
                api_key=settings.GEMINI_API_KEY,
            )
        except litellm.RateLimitError as e:
            last_error = e
            if i < len(FALLBACK_MODELS) - 1:
                next_model = FALLBACK_MODELS[i + 1]
                logger.warning(f"Rate limit on {model}, falling back to {next_model}")
            else:
                logger.error(f"All models exhausted. Last error: {e}")
    raise last_error


class AgentState(TypedDict):
    user_input: str
    known_trackers: dict
    tracker_descriptions: dict          # {name: description string}
    conversation_history: list[dict]    # cross-turn [user/assistant] message pairs
    messages: list[dict]                # LiteLLM message history for current turn
    pending_tool: str | None            # tool name waiting for HITL confirmation
    pending_tool_call_id: str | None    # real tool_call id from the LLM response
    pending_args: dict | None           # tool args waiting for confirmation
    confirmed: bool | None              # None = not yet decided, True/False = user decided
    final_response: str | None


def parse_intent_node(state: AgentState) -> AgentState:
    """Call LLM with tools. If it returns a tool_call, stage it for confirmation.

    On resume after HITL, confirmed is already set — skip LLM and pass state
    through so needs_confirmation can route directly to execute_tool.
    """
    if state.get("pending_tool") and state.get("confirmed") is not None:
        return state

    system_prompt = get_system_prompt(state["known_trackers"], state.get("tracker_descriptions", {}))

    # Build messages: system + recent conversation history + current user input
    recent_history = state.get("conversation_history", [])[-MAX_HISTORY_MESSAGES:]
    messages = (
        [{"role": "system", "content": system_prompt}]
        + recent_history
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
            "pending_tool": call.function.name,
            "pending_tool_call_id": call.id,
            "pending_args": args,
            "confirmed": None,
            "final_response": None,
        }

    # No tool call — LLM responded directly; update conversation history
    updated_history = state.get("conversation_history", []) + [
        {"role": "user", "content": state["user_input"]},
        {"role": "assistant", "content": message.content},
    ]
    return {
        **state,
        "messages": messages,
        "conversation_history": updated_history[-MAX_HISTORY_MESSAGES:],
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
        }

    result = await execute_tool(tool_name, **state["pending_args"])
    logger.info(f"Tool result: {result}")

    # Use the real tool_call_id — Gemini validates this strictly
    tool_call_id = state.get("pending_tool_call_id") or "hitl_confirmed"
    follow_up_messages = state["messages"] + [
        {"role": "tool", "content": str(result), "tool_call_id": tool_call_id},
    ]
    follow_up = call_llm(follow_up_messages)
    final_response = follow_up.choices[0].message.content

    # Append a condensed summary to conversation history for next turn context
    updated_history = state.get("conversation_history", []) + [
        {"role": "user", "content": state["user_input"]},
        {"role": "assistant", "content": f"[{tool_name}] {final_response}"},
    ]
    return {
        **state,
        "final_response": final_response,
        "conversation_history": updated_history[-MAX_HISTORY_MESSAGES:],
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
) -> AgentState:
    """Start a new turn in the workflow thread."""
    tracker_descriptions = await get_tracker_descriptions()
    config = {"configurable": {"thread_id": thread_id}}
    initial_state: AgentState = {
        "user_input": user_input,
        "known_trackers": known_trackers,
        "tracker_descriptions": tracker_descriptions,
        "conversation_history": conversation_history or [],
        "messages": [],
        "pending_tool": None,
        "pending_tool_call_id": None,
        "pending_args": None,
        "confirmed": None,
        "final_response": None,
    }
    result = await graph.ainvoke(initial_state, config=config)
    return result


async def resume(thread_id: str, confirmed: bool) -> AgentState:
    """Resume a paused workflow after the user confirms or cancels."""
    config = {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke({"confirmed": confirmed}, config=config)
    return result
