"""Headroom compression utility for MCP responses."""
import json
import logging
from typing import Any, List, Union

from app.core.config import settings

logger = logging.getLogger(__name__)


def compress_for_prompt(data: Union[List[str], List[dict], dict, str]) -> str:
    """
    Compress data for inclusion in LLM prompts using Headroom.

    Only compresses if:
    1. HEADROOM_COMPRESSION_ENABLED is True
    2. Serialized data exceeds HEADROOM_MIN_CHARS (default 1000)

    Returns the original data as string if compression is disabled or data is small.
    """
    if isinstance(data, str):
        serialized = data
    else:
        serialized = json.dumps(data, ensure_ascii=False)

    if not settings.HEADROOM_COMPRESSION_ENABLED:
        return serialized

    if len(serialized) < settings.HEADROOM_MIN_CHARS:
        return serialized

    try:
        import headroom

        messages = [{"role": "user", "content": serialized}]
        result = headroom.compress(messages, compress_user_messages=True)
        compressed = result.messages[0]["content"]

        original_len = len(serialized)
        compressed_len = len(compressed)
        ratio = (1 - compressed_len / original_len) * 100

        logger.info(
            f"Headroom compression: {original_len} -> {compressed_len} chars ({ratio:.1f}% reduction)"
        )

        return compressed
    except ImportError:
        logger.warning("headroom-ai not installed, skipping compression")
        return serialized
    except Exception as e:
        logger.warning(f"Headroom compression failed, using original: {e}")
        return serialized


def compress_file_list(files: List[str]) -> str:
    """Compress a list of file paths for prompt inclusion."""
    if not files:
        return "None"
    return compress_for_prompt(files)


def compress_test_list(tests: List[str]) -> str:
    """Compress a list of test paths for prompt inclusion."""
    if not tests:
        return "None"
    return compress_for_prompt(tests)


def compress_flow_list(flows: List[str]) -> str:
    """Compress a list of flow names for prompt inclusion."""
    if not flows:
        return "None"
    return compress_for_prompt(flows)
