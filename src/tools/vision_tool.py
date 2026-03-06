"""
Vision Tool — Analyze images using Gemini's multimodal capability.

Flow:
  1. Receive image bytes (from Telegram photo download)
  2. Send to Gemini vision with VISION_PROMPT
  3. Return structured description for the orchestrator
  4. Orchestrator proposes a log action → user confirms via Telegram inline keyboard
"""
import base64
from typing import Any

import litellm

from src.agent.prompts import VISION_PROMPT
from src.utils.config import settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def analyze_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """
    Send an image to the vision LLM and return a structured description
    of any trackable data found.

    Returns:
        {
            "status": "success" | "error",
            "description": str,
            "message": str   # user-facing proposal text
        }
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    try:
        response = litellm.completion(
            model="gemini/gemini-2.5-flash",
            api_key=settings.GEMINI_API_KEY,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        )

        raw = response.choices[0].message.content
        logger.info(f"[Vision] LLM response: {raw[:200]}")
        return {"status": "success", "description": raw, "message": raw}

    except Exception as e:
        logger.error(f"[Vision] Error: {e}")
        return {"status": "error", "description": "", "message": str(e)}
