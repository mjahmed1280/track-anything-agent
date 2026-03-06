"""
Orchestrator: LangGraph-based workflow with Human-in-the-Loop (HITL) checkpoint.

Flow:
  parse_intent → [HITL confirm checkpoint] → execute_tool → respond

The HITL checkpoint pauses the graph and emits a pending_confirmation state.
The caller (Telegram integration) sends an inline keyboard to the user.
When the user clicks confirm/cancel, the graph is resumed with the decision.
"""
from typing import TypedDict, Annotated, Any
from langgraph.graph import StateGraph, END

import litellm

from src.agent.prompts import get_system_prompt
from src.agent.registry import TOOL_REGISTRY, get_tool_schemas_for_litellm, execute_tool
from src.tools.firestore_tool import db
from src.utils.firestore_checkpointer import FirestoreCheckpointer
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)

# LiteLLM model — swap to "anthropic/claude-sonnet-4-6" by changing this one string
DEFAULT_MODEL = "gemini/gemini-2.5-flash"


class AgentState(TypedDict):
    user_input: str
    known_trackers: dict
    messages: list[dict]          # LiteLLM message history
    pending_tool: str | None      # tool name waiting for HITL confirmation
    pending_args: dict | None     # tool args waiting for confirmation
    confirmed: bool | None        # None = not yet decided, True/False = user decided
    final_response: str | None


def parse_intent_node(state: AgentState) -> AgentState:
    """Call LLM with tools. If it returns a tool_call, stage it for confirmation."""
    system_prompt = get_system_prompt(state["known_trackers"])
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": state["user_input"]},
    ]

    tools = get_tool_schemas_for_litellm()
    response = litellm.completion(
        model=DEFAULT_MODEL,
        messages=messages,
        tools=tools if tools else None,
        tool_choice="auto" if tools else None,
        api_key=settings.GEMINI_API_KEY,
    )

    message = response.choices[0].message
    tool_calls = getattr(message, "tool_calls", None)

    if tool_calls:
        call = tool_calls[0]
        import json
        args = json.loads(call.function.arguments)
        logger.info(f"LLM wants to call tool: {call.function.name} with args: {args}")
        return {
            **state,
            "messages": messages + [message],
            "pending_tool": call.function.name,
            "pending_args": args,
            "confirmed": None,
            "final_response": None,
        }

    # No tool call — LLM responded directly
    return {
        **state,
        "messages": messages,
        "pending_tool": None,
        "pending_args": None,
        "confirmed": None,
        "final_response": message.content,
    }


async def execute_tool_node(state: AgentState) -> AgentState:
    """Execute the confirmed tool and return result to the LLM for a final response."""
    if not state["confirmed"]:
        return {**state, "final_response": "Action cancelled."}

    result = await execute_tool(state["pending_tool"], **state["pending_args"])
    logger.info(f"Tool result: {result}")

    # Give the result back to the LLM for a human-readable response
    follow_up_messages = state["messages"] + [
        {"role": "tool", "content": str(result), "tool_call_id": "hitl_confirmed"},
    ]
    follow_up = litellm.completion(
        model=DEFAULT_MODEL,
        messages=follow_up_messages,
        api_key=settings.GEMINI_API_KEY,
    )
    return {
        **state,
        "final_response": follow_up.choices[0].message.content,
        "pending_tool": None,
        "pending_args": None,
    }


def needs_confirmation(state: AgentState) -> str:
    if state["pending_tool"] and state["confirmed"] is None:
        return "await_confirmation"
    if state["pending_tool"] and state["confirmed"] is not None:
        return "execute"
    return END


# Build the graph
_builder = StateGraph(AgentState)
_builder.add_node("parse_intent", parse_intent_node)
_builder.add_node("execute_tool", execute_tool_node)
_builder.set_entry_point("parse_intent")
_builder.add_conditional_edges("parse_intent", needs_confirmation, {
    "await_confirmation": END,   # Graph pauses here; caller resumes after user confirms
    "execute": "execute_tool",
    END: END,
})
_builder.add_edge("execute_tool", END)

checkpointer = FirestoreCheckpointer(db)
graph = _builder.compile(checkpointer=checkpointer)


async def run(user_input: str, known_trackers: dict, thread_id: str) -> AgentState:
    """Start or resume a workflow thread."""
    config = {"configurable": {"thread_id": thread_id}}
    initial_state: AgentState = {
        "user_input": user_input,
        "known_trackers": known_trackers,
        "messages": [],
        "pending_tool": None,
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
