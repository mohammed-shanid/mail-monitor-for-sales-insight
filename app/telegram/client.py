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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import app.config as config
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


# =============================================================================
# Report path (SPEC.md §16), added Stage 4. `_call`/`get_updates`/
# `send_message` above are unchanged and still used by the bot exclusively.
# `send(text) -> DeliveryResult` is the ONLY report-path entry point
# (CLAUDE.md: "app/telegram/client.py knows nothing about enquiries.
# Its surface is send(text) -> DeliveryResult") -- it reads
# TELEGRAM_CHAT_ID/TELEGRAM_CHUNK_CHARS/TELEGRAM_MAX_RETRIES from config
# itself rather than taking them as parameters, so the signature stays
# exactly `send(text: str)`.
# =============================================================================


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    parts_sent: int
    parts_total: int
    error: Optional[str] = None


def chunk_text(text: str, limit: int) -> List[str]:
    """Split `text` into pieces no longer than `limit` chars, breaking
    only at blank-line (paragraph/section) boundaries -- never
    mid-enquiry (SPEC.md §16.2). Falls back to a hard split only for a
    single block that alone exceeds `limit` (should not happen for a
    well-formed report, but must not raise either).
    """
    if len(text) <= limit:
        return [text]

    blocks = text.split("\n\n")
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for block in blocks:
        added_len = len(block) + (2 if current else 0)
        if current and current_len + added_len > limit:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))

    final: List[str] = []
    for chunk in chunks:
        if len(chunk) <= limit:
            final.append(chunk)
        else:
            final.extend(chunk[i : i + limit] for i in range(0, len(chunk), limit))
    return final


def _post_raw(method: str, params: Dict[str, Any], *, timeout: int) -> Tuple[bool, dict, Optional[int]]:
    """Low-level POST that returns (ok, payload, http_status) instead
    of raising on an HTTP error status -- so the retry loop below can
    inspect the status/retry_after instead of just seeing "it failed".
    Still raises TelegramError for a missing token or a response that
    isn't parseable JSON at all (nothing a retry could fix).
    """
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")

    url = _API_BASE.format(token=TELEGRAM_BOT_TOKEN, method=method)
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(url, data=data)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
            return True, payload, response.status
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return False, payload, exc.code
    except OSError as exc:
        raise TelegramError(f"Network error calling Telegram {method}: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise TelegramError(f"Malformed response from Telegram {method}: {exc}") from exc


def _send_one_part(chat_id: str, text: str, *, max_retries: int) -> None:
    """Send one already-chunked part, retrying on 429 (honouring
    Telegram's own `retry_after`) and 5xx, up to `max_retries`
    additional attempts (SPEC.md §16.4). Raises TelegramError on a
    non-retryable failure or after retries are exhausted.
    """
    attempt = 0
    while True:
        ok, payload, status = _post_raw("sendMessage", {"chat_id": chat_id, "text": text}, timeout=15)
        if ok:
            return

        retryable = status == 429 or (status is not None and status >= 500)
        if retryable and attempt < max_retries:
            retry_after = None
            if status == 429:
                retry_after = (payload.get("parameters") or {}).get("retry_after")
            delay = retry_after if retry_after is not None else 2**attempt
            logger.warning(
                "Telegram sendMessage failed (status %s), retrying in %ds (attempt %d/%d)",
                status, delay, attempt + 1, max_retries,
            )
            time.sleep(delay)
            attempt += 1
            continue

        raise TelegramError(f"Telegram API error calling sendMessage (status {status}): {payload}")


def send(text: str) -> DeliveryResult:
    """The report path's ONLY way to reach Telegram (SPEC.md §16.1).
    No knowledge of enquiries, brands, or metrics -- just plain text
    in, a DeliveryResult out. Chunks at TELEGRAM_CHUNK_CHARS, prefixing
    continuation parts with "(Part i/n)" (SPEC.md §16.2); sends are
    sequential, and a mid-sequence failure is reported as partial
    delivery naming how many parts actually landed (SPEC.md §16.4).
    Never claims success unless the Telegram API confirmed every part.
    """
    chat_id = config.TELEGRAM_CHAT_ID
    if not chat_id:
        return DeliveryResult(success=False, parts_sent=0, parts_total=0, error="TELEGRAM_CHAT_ID is not set")

    try:
        chunk_limit = int(config.TELEGRAM_CHUNK_CHARS)
    except (TypeError, ValueError):
        chunk_limit = 3800
    try:
        max_retries = int(config.TELEGRAM_MAX_RETRIES)
    except (TypeError, ValueError):
        max_retries = 3

    parts = chunk_text(text, chunk_limit)
    total = len(parts)

    for i, part in enumerate(parts, start=1):
        body = part if total == 1 else f"(Part {i}/{total})\n{part}"
        try:
            _send_one_part(chat_id, body, max_retries=max_retries)
        except TelegramError as exc:
            logger.error("Telegram delivery failed at part %d/%d: %s", i, total, exc)
            return DeliveryResult(success=False, parts_sent=i - 1, parts_total=total, error=str(exc))

    logger.info("Telegram delivery succeeded (%d part(s))", total)
    return DeliveryResult(success=True, parts_sent=total, parts_total=total)
