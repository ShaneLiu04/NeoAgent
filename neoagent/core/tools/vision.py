"""Vision/image analysis tool using multimodal LLM capabilities.

CyberClaw has no vision support — this tool enables image analysis
when the underlying model supports it (GPT-4o, Claude Sonnet/Opus, Gemini).
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from ..config import get_config
from ..security import Capability
from .base import RiskLevel
from .base import neoagent_tool


def _encode_image(image_path: str) -> tuple[str, str]:
    """Read and base64-encode an image file. Returns (base64_string, mime_type)."""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    ext_to_mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    mime = ext_to_mime.get(path.suffix.lower(), "image/png")

    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return encoded, mime


@neoagent_tool(
    name="analyze_image",
    description="Analyze an image file using the current multimodal LLM. Describe contents, extract text, or answer questions about the image.",
    capabilities=Capability.FILESYSTEM_READ,
    risk_level=RiskLevel.LOW,
    timeout_seconds=60.0,
    tags=["vision", "image", "multimodal"],
)
async def analyze_image(image_path: str, question: str = "Describe this image in detail.") -> str:
    """Analyze an image using the LLM's vision capabilities.

    Args:
        image_path: Path to the image file (PNG, JPG, GIF, WebP, BMP)
        question: What to ask about the image
    """
    try:
        encoded, mime = _encode_image(image_path)
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        return f"Failed to read image: {e}"

    # Check file size (max 5MB base64-encoded)
    if len(encoded) > 5_000_000:
        return "Error: image too large. Max 5MB after base64 encoding."

    data_url = f"data:{mime};base64,{encoded}"

    # Create multimodal message
    from langchain_core.messages import HumanMessage

    msg = HumanMessage(content=[
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": data_url}},
    ])

    from ..provider import get_provider

    cfg = get_config()
    llm = get_provider()
    try:
        response = await llm.ainvoke([msg])
        return response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        error_msg = str(e).lower()
        if "vision" in error_msg or "multimodal" in error_msg or "image" in error_msg:
            return (
                f"The current model ({cfg.llm.default_model}) does not support image analysis.\n"
                "Use a multimodal model like: gpt-4o, claude-sonnet-4-6, claude-opus-4-7, or gemini-2.0-flash."
            )
        return f"Analysis failed: {e}"
