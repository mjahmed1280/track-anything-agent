from typing import Callable, Any
from src.utils.logger import get_logger

logger = get_logger(__name__)

# Central registry: tool_name -> callable
TOOL_REGISTRY: dict[str, Callable] = {}


def register_tool(name: str):
    """Decorator to register a function as a named tool."""
    def decorator(fn: Callable):
        TOOL_REGISTRY[name] = fn
        logger.info(f"Registered tool: {name}")
        return fn
    return decorator


async def execute_tool(name: str, **kwargs) -> dict[str, Any]:
    """Execute a registered tool by name. Always returns {status, message, data?}."""
    if name not in TOOL_REGISTRY:
        return {"status": "error", "message": f"Unknown tool: {name}"}
    try:
        result = await TOOL_REGISTRY[name](**kwargs)
        return result
    except Exception as e:
        logger.error(f"Tool '{name}' failed: {e}")
        return {"status": "error", "message": str(e)}


def get_tool_schemas_for_gemini() -> list[dict]:
    """Generate Gemini Function Calling schema from registered tools."""
    # Each tool function should have a __tool_schema__ attribute set at registration.
    # This is a placeholder — schemas are defined alongside each tool's @register_tool usage.
    schemas = []
    for name, fn in TOOL_REGISTRY.items():
        schema = getattr(fn, "__tool_schema__", None)
        if schema:
            schemas.append(schema)
    return schemas


def get_tool_schemas_for_litellm() -> list[dict]:
    """Generate LiteLLM/OpenAI-compatible tool schema from registered tools."""
    schemas = []
    for name, fn in TOOL_REGISTRY.items():
        schema = getattr(fn, "__tool_schema__", None)
        if schema:
            # LiteLLM uses the OpenAI format: {"type": "function", "function": {...}}
            schemas.append({"type": "function", "function": schema})
    return schemas
