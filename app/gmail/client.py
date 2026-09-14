"""Gmail message fetching and parsing.

Two layers, kept deliberately separate:
  - fetch_* / list_* / get_* functions call the Gmail API.
  - parse_message / extract_body are pure functions that take
    already-fetched dicts and return plain data, so they can be unit
    tested with fixture payloads -- no network, no credentials.

Only ever reads (list/get). Nothing here sends, modifies, or deletes
mail -- that matches the read-only OAuth scope in app.gmail.auth.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Keep bodies bounded before they ever reach the database or Claude --
# real customer enquiries are short; nothing legitimate needs more than
# this, and it caps cost/latency if something unusual slips through.
MAX_BODY_CHARS = 20_000


@dataclass
class EmailMessage:
    """A parsed Gmail message with just the fields the rest of the
    system needs. Deliberately flat -- no nested MIME structure leaks
    past this module.
    """

    gmail_message_id: str
    gmail_thread_id: str
    sender: str
    recipient: str
    subject: str
    received_at: Optional[str]  # raw RFC 2822 "Date" header value
    body: str


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text: drops tags/scripts/styles, keeps visible
    text. Not a full renderer -- just enough to turn an HTML email into
    clean text instead of raw markup before it reaches the database or
    Claude.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return "\n".join(self._chunks)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        logger.warning("Failed to parse HTML email body; using raw HTML as a fallback")
        return html
    return parser.get_text()


def _decode_part_data(data: str) -> str:
    """Gmail encodes message part bodies as URL-safe base64, often
    without the trailing '=' padding Python's decoder expects."""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        logger.warning("Failed to decode a Gmail message part body")
        return ""


def extract_body(payload: dict) -> str:
    """Walk a Gmail message payload and return clean plain text.

    Handles text/plain, text/html, and multipart/* (alternative or
    mixed), including nested multiparts -- e.g. a multipart/mixed
    message containing a multipart/alternative body plus an
    attachment. Prefers text/plain over text/html when both are
    present. Attachments (parts with a filename and no inline data we
    recognize as text) are ignored.
    """
    plain_text: Optional[str] = None
    html_text: Optional[str] = None

    def walk(part: dict) -> None:
        nonlocal plain_text, html_text
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")

        if mime_type == "text/plain" and data and plain_text is None:
            plain_text = _decode_part_data(data)
        elif mime_type == "text/html" and data and html_text is None:
            html_text = _decode_part_data(data)
        elif mime_type.startswith("multipart/"):
            for sub_part in part.get("parts", []) or []:
                walk(sub_part)
        elif mime_type.startswith("text/") and data and plain_text is None:
            # Unrecognized text/* part (rare) -- treat as plain text.
            plain_text = _decode_part_data(data)

    walk(payload)

    text = plain_text if plain_text is not None else _html_to_text(html_text or "")
    text = text.strip()

    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n... [truncated]"

    return text


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_message(raw: dict) -> EmailMessage:
    """Convert a raw Gmail API message (fetched with format='full')
    into an EmailMessage. Pure function -- no network calls -- so it's
    easy to unit test with fixture payloads.
    """
    payload = raw.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    return EmailMessage(
        gmail_message_id=raw.get("id", ""),
        gmail_thread_id=raw.get("threadId", ""),
        sender=_header(headers, "From"),
        recipient=_header(headers, "To"),
        subject=_header(headers, "Subject"),
        received_at=_header(headers, "Date") or None,
        body=extract_body(payload),
    )


def list_message_ids(service, max_results: int = 10, query: Optional[str] = None) -> list[str]:
    """Return Gmail message IDs for the mailbox's most recent messages
    (optionally filtered by Gmail search `query`), most recent first.
    Returns an empty list (rather than raising) on a Gmail API error,
    so a transient failure doesn't crash a whole sync run.
    """
    try:
        response = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q=query)
            .execute()
        )
    except HttpError as error:
        logger.error("Gmail API error while listing messages: %s", error)
        return []

    return [m["id"] for m in response.get("messages", []) or []]


def get_raw_message(service, message_id: str) -> Optional[dict]:
    """Fetch one full Gmail message by ID. Returns None on a Gmail API
    error so callers can skip a bad message instead of crashing the
    whole sync.
    """
    try:
        return (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as error:
        logger.error("Gmail API error while fetching message %s: %s", message_id, error)
        return None


def fetch_messages(service, max_results: int = 10, query: Optional[str] = None) -> list[EmailMessage]:
    """Fetch and parse the mailbox's most recent messages. Messages
    that fail to fetch are logged and skipped rather than aborting the
    whole batch.
    """
    message_ids = list_message_ids(service, max_results=max_results, query=query)
    logger.info("Fetched %d message id(s) from Gmail", len(message_ids))

    messages: list[EmailMessage] = []
    for message_id in message_ids:
        raw = get_raw_message(service, message_id)
        if raw is None:
            continue
        messages.append(parse_message(raw))

    return messages
