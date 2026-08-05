"""Minimal Telegram Bot API client for notifications (CTV2-1381).

Single function: send_message.  Uses httpx with an explicit timeout.
Never raises — returns (ok, provider_message_id, error).
The bot token is NEVER included in error strings or logs.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bool, str | None, str | None]:
    """Send a Telegram message.  Returns (ok, message_id_or_none, error_or_none).

    The bot token is never included in the returned error string.
    """
    url = f"{_TELEGRAM_API}/bot{bot_token}/sendMessage"
    body: dict[str, Any] = {"chat_id": chat_id, "text": text}
    t = httpx.Timeout(timeout)
    try:
        kwargs: dict[str, Any] = {"timeout": t}
        if transport is not None:
            kwargs["transport"] = transport
        with httpx.Client(**kwargs) as client:
            resp = client.post(url, json=body)
        if resp.status_code == 200:
            data = resp.json()
            msg_id = str(data.get("result", {}).get("message_id", ""))
            return True, msg_id or None, None
        return False, None, f"HTTP {resp.status_code}"
    except httpx.TimeoutException as exc:
        return False, None, f"timeout: {type(exc).__name__}"
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
