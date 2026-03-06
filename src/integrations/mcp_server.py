"""
MCP Server (Optional) — Exposes registered tools as an MCP-compatible server.

This allows the tool registry to be consumed by any MCP-compatible client
(e.g., Claude Desktop, other agents) without changing the tool implementations.

Currently a stub. Activate by running:
  uvicorn src.integrations.mcp_server:app --port 8081
"""
from fastapi import FastAPI
from src.agent.registry import TOOL_REGISTRY, execute_tool
from src.utils.logger import get_logger

logger = get_logger(__name__)

app = FastAPI(title="track-anything MCP Server")


@app.get("/tools")
async def list_tools():
    """List all registered tools and their schemas."""
    tools = []
    for name, fn in TOOL_REGISTRY.items():
        schema = getattr(fn, "__tool_schema__", {"name": name, "description": ""})
        tools.append(schema)
    return {"tools": tools}


@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, args: dict):
    """Execute a registered tool by name with the provided args."""
    logger.info(f"[MCP] Calling tool: {tool_name} args={args}")
    result = await execute_tool(tool_name, **args)
    return result
