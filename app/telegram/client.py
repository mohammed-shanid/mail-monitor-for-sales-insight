"""Telegram Bot API client -- the V1 prototyping channel (see README;
WhatsApp Business Cloud API remains the eventual founder-facing
channel). Only the official Bot API over HTTPS -- no browser
automation, no unofficial libraries, no session/cookie scraping (the
same constraint the project spec places on WhatsApp).

Uses the standard library's urllib rather than adding a new HTTP
dependency -- this is two simple JSON-over-HTTPS calls, no need for
`requests`.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from app.config import TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_LONG_POLL_TIMEOUT = 30  # seconds -- Telegram holds the connection open this long


class TelegramError(RuntimeError):
    """Raised when TELEGRAM_BOT_TOKEN is missing, the network request
    fails, or the Telegram API itself reports failure (ok: false).
    Never includes the bot token in its message.
    """


def _call(method: str, params: Dict[str, Any], *, timeout: int) -> Any:
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")

    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(url, data=data)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except OSError as exc:
        # Covers urllib.error.URLError AND a bare socket.timeout/
        # TimeoutError -- both are OSError subclasses, and a long-poll
        # request that simply times out with no new messages (the
        # normal, expected case when nothing has been sent) raises a
        # raw socket.timeout that urllib does NOT wrap in URLError.
        # Letting this escape uncaught would crash the whole polling
        # loop on every quiet period.
        raise TelegramError(f"Network error calling Telegram {method}: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise TelegramError(f"Malformed response from Telegram {method}: {exc}") from exc

    if not payload.get("ok"):
        raise TelegramError(f"Telegram API error calling {method}: {payload}")

    return payload["result"]


def get_updates(offset: Optional[int] = None, timeout: int = _LONG_POLL_TIMEOUT) -> List[dict]:
    """Long-poll for new messages since `offset`.

    `offset` should be the previous highest update_id + 1, so Telegram
    doesn't redeliver already-handled updates. The HTTP request timeout
    is padded past Telegram's own long-poll `timeout` so the connection
    isn't cut before Telegram would naturally respond.
    """
    params: Dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    return _call("getUpdates", params, timeout=timeout + 10)


def send_message(chat_id: int, text: str) -> dict:
    """Send a plain-text reply to one Telegram chat."""
    return _call("sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
