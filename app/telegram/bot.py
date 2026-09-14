"""Telegram long-polling loop: the V1 founder-facing bot runtime.

    Telegram getUpdates -> app.router.intent.route_message() -> Telegram sendMessage

No webhook or public HTTPS endpoint needed -- see README. This file
has no business logic of its own; swapping to WhatsApp later means
writing app/whatsapp/webhook.py + client.py against the same
route_message(), not touching this one.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.database.db import get_connection
from app.router.intent import route_message
from app.telegram.client import TelegramError, get_updates, send_message

logger = logging.getLogger(__name__)

# How long to back off after a Telegram API/network failure before
# retrying getUpdates, so a transient outage doesn't spin the loop.
_ERROR_BACKOFF_SECONDS = 5


def _handle_update(update: dict) -> None:
    message = update.get("message")
    if not message or "text" not in message:
        # Non-text update (photo, sticker, edited message, channel
        # post, ...) -- V1 only understands text questions.
        logger.info("Ignoring non-text Telegram update %s", update.get("update_id"))
        return

    chat_id = message["chat"]["id"]
    text = message["text"]
    logger.info("Telegram message received from chat %s", chat_id)

    with get_connection() as conn:
        reply = route_message(conn, text)

    send_message(chat_id, reply)
    logger.info("Telegram reply sent to chat %s", chat_id)


def run_polling_loop(poll_timeout: int = 30) -> None:
    """Long-poll Telegram forever, replying to each incoming message.

    Runs until interrupted (Ctrl+C). A failure fetching updates is
    logged and retried after a short backoff; a failure handling one
    update is logged and skipped -- neither crashes the loop, so one
    bad message or one blip in connectivity doesn't take the bot down.
    """
    offset: Optional[int] = None
    logger.info("Telegram bot started (long polling)")

    while True:
        try:
            updates = get_updates(offset=offset, timeout=poll_timeout)
        except TelegramError as exc:
            logger.error("Telegram getUpdates failed: %s", exc)
            time.sleep(_ERROR_BACKOFF_SECONDS)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                _handle_update(update)
            except Exception:
                logger.exception(
                    "Unexpected error handling Telegram update %s", update.get("update_id")
                )
