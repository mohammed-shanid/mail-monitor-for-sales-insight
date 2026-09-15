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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# A generous DoS-style safety net only -- caps a pathological payload
# before it ever reaches Python string processing. NOT the real
# storage cap: that is app.gmail.ingest.STORAGE_BODY_MAX_CHARS
# (20 000), applied AFTER quote/signature stripping so a message whose
# raw MIME part (new content + full quoted history) exceeds this
# module's old, smaller default doesn't get chopped before parser.py
# ever sees the quote marker it's looking for (PHASE0_DECISIONS.md
# Q17).
MAX_BODY_CHARS = 200_000

# Deterministic auto-reply detection (PHASE0_DECISIONS.md Q12) -- headers
# first; Claude's judgement (Stage 3) is only a second signal when none of
# these are present. Header names are matched case-insensitively; values
# are matched as a substring, also case-insensitively.
_AUTO_REPLY_HEADER_VALUES = {
    "auto-submitted": ("auto-replied",),
    "x-autoreply": None,  # presence alone is the signal
    "x-autorespond": None,
    "precedence": ("bulk", "auto_reply"),
}
_AUTO_REPLY_SUBJECT_PREFIXES = ("automatic reply:", "out of office", "out of office:")


class GmailFetchError(RuntimeError):
    """Raised by the report-path fetch functions on any Gmail API
    failure. Unlike the bot's list_message_ids/get_raw_message (which
    swallow errors and return empty/None so one bad sync doesn't crash
    the whole run), a partial fetch for a report is worse than no
    report at all (SPEC.md §17) -- so these raise, and report.py aborts
    the run rather than compute metrics on an incomplete window.
    """


@dataclass
class EmailMessage:
    """A parsed Gmail message with just the fields the rest of the
    system needs. Deliberately flat -- no nested MIME structure leaks
    past this module.

    `internal_date_ms`, `cc`, `is_auto_reply`, and `attachments` are
    report-path additions (Stage 2, SPEC.md §5.3-§5.4) -- all default
    to values `parse_message()` already produces for a fixture that
    doesn't set them, so the bot's existing equality-based tests are
    unaffected.
    """

    gmail_message_id: str
    gmail_thread_id: str
    sender: str
    recipient: str
    subject: str
    received_at: Optional[str]  # raw RFC 2822 "Date" header value
    body: str
    internal_date_ms: Optional[int] = None  # epoch ms UTC, from internalDate
    cc: str = ""
    is_auto_reply: bool = False
    attachments: List[Tuple[str, str]] = field(default_factory=list)  # (filename, mime_type)


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text: drops tags/scripts/styles, keeps visible
    text. Not a full renderer -- just enough to turn an HTML email into
    clean text instead of raw markup before it reaches the database or
    Claude.

    Also stops collecting text once it sees an element carrying
    Gmail's own `gmail_quote` class (SPEC.md §5.4) -- Gmail wraps a
    reply's quoted history in `<div class="gmail_quote">...</div>` in
    the HTML part, placed after the reply's own new content. Rather
    than tracking that div's precise close tag (real Gmail HTML is
    riddled with unclosed void elements like `<br>`/`<img>` that would
    make a start/end-tag-matching stack undercount and never resume,
    silently eating the rest of the message), this simply stops
    collecting text for the remainder of the payload once the quote
    marker is seen -- correct in practice, since the quote div is
    always the trailing content of a Gmail-generated reply.
    """

    _SKIP_TAGS = ("script", "style")

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._quote_reached = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        class_value = next((v for k, v in attrs if k == "class" and v), "")
        if "gmail_quote" in class_value.split():
            self._quote_reached = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and not self._quote_reached and data.strip():
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


def _is_auto_reply(headers: list[dict], subject: str) -> bool:
    """Deterministic auto-reply detection (PHASE0_DECISIONS.md Q12):
    headers first (`Auto-Submitted: auto-replied`, `X-Autoreply`,
    `X-Autorespond`, `Precedence: bulk|auto_reply`), then a subject
    prefix ("Automatic reply:", "Out of Office"/"Out of office:").
    Claude's judgement (Stage 3) is only a second signal when none of
    these deterministic markers are present.
    """
    for name, expected_values in _AUTO_REPLY_HEADER_VALUES.items():
        value = _header(headers, name)
        if not value:
            continue
        if expected_values is None:
            return True
        value_lower = value.lower()
        if any(expected in value_lower for expected in expected_values):
            return True

    subject_lower = (subject or "").strip().lower()
    return any(subject_lower.startswith(prefix) for prefix in _AUTO_REPLY_SUBJECT_PREFIXES)


def _extract_attachments(payload: dict) -> List[Tuple[str, str]]:
    """Filename + MIME type only, per SPEC.md §5.4 -- never downloads or
    parses attachment contents. An "attachment" is any part with a
    filename; inline images referenced by a `Content-ID` are Gmail
    parts too, but without a filename they are not counted here.
    """
    attachments: List[Tuple[str, str]] = []

    def walk(part: dict) -> None:
        filename = part.get("filename")
        if filename:
            attachments.append((filename, part.get("mimeType", "")))
        for sub_part in part.get("parts", []) or []:
            walk(sub_part)

    walk(payload)
    return attachments


def parse_message(raw: dict) -> EmailMessage:
    """Convert a raw Gmail API message (fetched with format='full')
    into an EmailMessage. Pure function -- no network calls -- so it's
    easy to unit test with fixture payloads.

    `internalDate` (epoch ms, as a numeric string in the raw API
    response), `Cc`, `is_auto_reply`, and `attachments` are report-path
    additions (Stage 2) -- all fall back to values a fixture that
    doesn't set them already produces (None / "" / False / []), so
    existing equality-based tests against this function are unaffected.
    """
    payload = raw.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    subject = _header(headers, "Subject")

    internal_date_raw = raw.get("internalDate")
    try:
        internal_date_ms = int(internal_date_raw) if internal_date_raw is not None else None
    except (TypeError, ValueError):
        logger.warning("Malformed internalDate on message %s: %r", raw.get("id"), internal_date_raw)
        internal_date_ms = None

    return EmailMessage(
        gmail_message_id=raw.get("id", ""),
        gmail_thread_id=raw.get("threadId", ""),
        sender=_header(headers, "From"),
        recipient=_header(headers, "To"),
        subject=subject,
        received_at=_header(headers, "Date") or None,
        body=extract_body(payload),
        internal_date_ms=internal_date_ms,
        cc=_header(headers, "Cc"),
        is_auto_reply=_is_auto_reply(headers, subject),
        attachments=_extract_attachments(payload),
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


# =============================================================================
# Report path (SPEC.md §5), added Stage 2. Everything above is unchanged and
# still used by the bot (list_message_ids/get_raw_message/fetch_messages all
# swallow API errors and fetch a single page -- fine for "the 10 most recent
# messages", wrong for a windowed report). The functions below raise
# GmailFetchError instead of swallowing, and paginate/hydrate fully.
# =============================================================================


def build_query(start_ms: int, end_ms: int, tz) -> str:
    """The report path's Gmail search query (SPEC.md §5.2, amended).

    Deliberately does NOT restrict to `in:inbox` -- that is Appendix A
    non-negotiable #1: employee replies live in SENT, and a query that
    excludes it makes every enquiry look pending. Deliberately does NOT
    widen the lower bound either (no THREAD_LOOKBACK_DAYS) -- thread
    hydration (get_thread, below) already pulls in a touched thread's
    pre-window messages, which is the only thing a lookback would be
    for; a separate lookback here would just be redundant with it.

    `after:`/`before:` are date-granular and timezone-fuzzy on Gmail's
    side -- this is a coarse pre-filter only. Exact window filtering
    against `internalDate` happens in Python (see app.gmail.ingest).
    """
    start_date = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).astimezone(tz).date()
    end_date = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).astimezone(tz).date()
    before_date = end_date + timedelta(days=1)
    return f"after:{start_date:%Y/%m/%d} before:{before_date:%Y/%m/%d} -in:drafts -in:chats"


def list_message_ids_paginated(service, query: str, limit: Optional[int] = None) -> List[str]:
    """Every message ID matching `query`, following `nextPageToken`
    fully (SPEC.md §5.3: "Never assume one page"). Raises
    GmailFetchError on any Gmail API error -- a partial listing must
    abort the run, not silently under-report (SPEC.md §17).

    `limit` is `--limit N`, a development aid (SPEC.md §13.2): stops
    once at least `limit` ids have been collected and truncates to
    exactly that many. It is not exact windowing -- just a fetch cap.
    """
    ids: List[str] = []
    page_token: Optional[str] = None

    while True:
        request_kwargs = {"userId": "me", "q": query}
        if page_token:
            request_kwargs["pageToken"] = page_token
        try:
            response = service.users().messages().list(**request_kwargs).execute()
        except HttpError as error:
            raise GmailFetchError(f"Gmail API error while listing messages: {error}") from error

        ids.extend(m["id"] for m in response.get("messages", []) or [])
        if limit is not None and len(ids) >= limit:
            return ids[:limit]

        page_token = response.get("nextPageToken")
        if not page_token:
            return ids


def get_raw_message_or_raise(service, message_id: str) -> dict:
    """Like get_raw_message(), but raises GmailFetchError instead of
    returning None on failure -- see module docstring for why the
    report path never swallows a fetch error.
    """
    try:
        return (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as error:
        raise GmailFetchError(
            f"Gmail API error while fetching message {message_id}: {error}"
        ) from error


def get_thread(service, thread_id: str) -> dict:
    """Every message of one Gmail thread (SPEC.md §5.3 hydration),
    including messages outside the original fetch window -- enquiry
    state is a function of the whole thread, so a partial thread
    produces a wrong status at window boundaries (Appendix A #2).
    Raises GmailFetchError on failure.
    """
    try:
        return (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
    except HttpError as error:
        raise GmailFetchError(f"Gmail API error while fetching thread {thread_id}: {error}") from error
