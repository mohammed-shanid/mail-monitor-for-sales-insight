"""Report-path Gmail ingestion (SPEC.md §5, §6.1, §18), added Stage 2.

    build_query -> list (paginated) -> filter by internalDate in Python
    -> group touched threads -> hydrate each via threads.get -> parse,
    clean -> upsert idempotently

Distinct from app.gmail.sync (the bot's mutate-in-place sync, retired
from use -- see that module's docstring): this module only ever WRITES
raw email rows via upsert_report_email(). It never touches
`enquiries` -- computing enquiry state from the stored messages is
Stage 3's job (app.enquiry.thread_state, not built yet). Mailbox
identity verification (SPEC.md §21.1) is report.py's job, before this
module is ever called -- see app.gmail.auth.verify_mailbox_identity.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.database.queries import upsert_report_email
from app.enquiry.status import classify_address, direction_for_address_class, extract_email_address
from app.gmail.client import (
    EmailMessage,
    build_query,
    get_raw_message_or_raise,
    get_thread,
    list_message_ids_paginated,
    parse_message,
)
from app.gmail.parser import clean_body

logger = logging.getLogger(__name__)

# The real practical body-storage cap (PHASE0_DECISIONS.md Q17):
# applied AFTER quote/signature stripping, here -- not the same as
# app.gmail.client.MAX_BODY_CHARS, which is a much larger safety net
# applied at MIME-parsing time, before stripping ever sees the text.
STORAGE_BODY_MAX_CHARS = 20_000


class IngestAbortedError(RuntimeError):
    """Raised when GMAIL_MAX_MESSAGES is exceeded -- SPEC.md §5.3:
    "abort with a clear message rather than truncating silently."
    report.py maps this to exit code 3 (closest fit: a Gmail-stage
    fetch that cannot safely proceed, not a partial-fetch API error,
    but the same "stop rather than under-report" family).
    """


@dataclass(frozen=True)
class IngestResult:
    """Counts for the SPEC.md §13.4 progress lines."""

    messages_listed: int  # from the initial window listing (one page or many)
    threads_touched: int
    messages_hydrated: int  # total distinct messages across all hydrated threads
    messages_new: int  # newly inserted this run (vs already-present, idempotent-skipped)


def _sender_domain(sender_header: str) -> Optional[str]:
    address = extract_email_address(sender_header)
    if not address or "@" not in address:
        return None
    return address.rsplit("@", 1)[-1].lower()


def ingest_window(
    conn: sqlite3.Connection,
    service,
    *,
    window_start_ms: int,
    window_end_ms: int,
    tz,
    mailbox: str,
    employee_aliases: List[str],
    internal_domains: List[str],
    max_messages: int,
    ingested_at_ms: int,
    limit: Optional[int] = None,
) -> IngestResult:
    """Fetch, hydrate, and idempotently store every message relevant to
    `[window_start_ms, window_end_ms]` (SPEC.md §5.2-§5.3, §18.1).

    `ingested_at_ms` is the caller's injected clock (never
    `datetime.now()` here -- CLAUDE.md/SPEC.md §12). `limit` is the
    `--limit N` development aid: caps the initial listing, not the
    hydrated set (a capped listing can still touch fewer threads, each
    still hydrated in full).
    """
    query = build_query(window_start_ms, window_end_ms, tz)
    logger.info("Gmail query: %s", query)

    message_ids = list_message_ids_paginated(service, query, limit=limit)
    logger.info("Listed %d message id(s) in window", len(message_ids))

    touched_thread_ids = set()
    listed_by_id: Dict[str, EmailMessage] = {}

    for message_id in message_ids:
        raw = get_raw_message_or_raise(service, message_id)
        parsed = parse_message(raw)
        listed_by_id[message_id] = parsed
        # internalDate is the authoritative timestamp (SPEC.md §5.3) --
        # Gmail's after:/before: query above is only a coarse
        # pre-filter, so a listed message can still fall outside the
        # exact window (or, at the boundary, a message just inside the
        # window can be missing from a coarse day-granular query -- but
        # that gap is exactly what makes internalDate filtering here,
        # not trust in the query, non-negotiable).
        if parsed.internal_date_ms is not None and window_start_ms <= parsed.internal_date_ms <= window_end_ms:
            touched_thread_ids.add(parsed.gmail_thread_id)

    logger.info("%d touched thread(s) in window", len(touched_thread_ids))

    hydrated_by_id: Dict[str, EmailMessage] = {}
    for thread_id in touched_thread_ids:
        thread = get_thread(service, thread_id)
        for raw_message in thread.get("messages", []) or []:
            parsed = parse_message(raw_message)
            hydrated_by_id[parsed.gmail_message_id] = parsed

    # Union, not concat -- a message can appear in both the initial
    # listing and its own thread's hydration; dedupe by message id
    # before it ever reaches the database (on top of the DB's own
    # UNIQUE constraint, which is the real idempotency guarantee).
    all_messages: Dict[str, EmailMessage] = {**listed_by_id, **hydrated_by_id}

    if len(all_messages) > max_messages:
        raise IngestAbortedError(
            f"{len(all_messages)} messages exceed GMAIL_MAX_MESSAGES "
            f"({max_messages}); aborting rather than truncating silently."
        )

    new_count = 0
    for message in all_messages.values():
        address_class = classify_address(
            message.sender,
            mailbox=mailbox,
            employee_aliases=employee_aliases,
            internal_domains=internal_domains,
        )
        direction = direction_for_address_class(address_class)

        if message.internal_date_ms is not None:
            received_at_ms = message.internal_date_ms
        else:
            # Should not happen for a real Gmail API response -- every
            # message has internalDate. Defensive fallback so one
            # malformed message doesn't abort the whole ingest; logged
            # loudly so it's never silent.
            logger.warning(
                "Message %s has no internalDate; using ingest time as a fallback",
                message.gmail_message_id,
            )
            received_at_ms = ingested_at_ms

        inserted = upsert_report_email(
            conn,
            gmail_message_id=message.gmail_message_id,
            gmail_thread_id=message.gmail_thread_id,
            sender=message.sender,
            sender_domain=_sender_domain(message.sender),
            recipient=message.recipient or None,
            subject=message.subject or None,
            body=clean_body(message.body)[:STORAGE_BODY_MAX_CHARS],
            received_at=received_at_ms,
            direction=direction,
            is_auto_reply=message.is_auto_reply,
            has_attachments=bool(message.attachments),
            ingested_at=ingested_at_ms,
        )
        if inserted:
            new_count += 1

    return IngestResult(
        messages_listed=len(message_ids),
        threads_touched=len(touched_thread_ids),
        messages_hydrated=len(all_messages),
        messages_new=new_count,
    )
