"""Incremental Gmail -> SQLite ingestion.

This is where Phases 1-3 come together, implementing the mandatory
branching rule from the project spec (section 11):

    for each fetched Gmail message:
        already processed (emails.gmail_message_id)? -> skip
        does an enquiry already exist for this thread?
            NO  -> Claude: is this an enquiry? extract customer/product
            YES -> Claude: classify the latest message's intent
        apply business rules (app.enquiry.status) -> write to SQLite

Safe to run repeatedly (e.g. on a cron/systemd timer): already-fully-
processed messages are skipped via emails.gmail_message_id, and each
enquiry's gmail_thread_id is unique, so nothing is ever duplicated
(project spec section 28). A message whose Claude call failed is left
with emails.processed = 0 so the *next* run retries it -- it is not
silently dropped.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from app.ai.claude import classify_reply_intent, extract_new_enquiry
from app.config import COMPANY_EMAIL_DOMAIN
from app.database.queries import (
    close_enquiry,
    get_email_by_message_id,
    get_enquiry_by_thread,
    insert_email,
    mark_email_processed,
    now_iso,
    update_enquiry_activity,
)
from app.database.queries import create_enquiry as _create_enquiry
from app.enquiry.models import Enquiry, LastSender, Status
from app.enquiry.status import classify_sender, extract_email_address, next_status_for_message
from app.gmail.client import EmailMessage, fetch_messages

logger = logging.getLogger(__name__)


def _parse_date(raw_date: Optional[str]) -> Optional[datetime]:
    if not raw_date:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _sort_key(message: EmailMessage) -> datetime:
    """Sort key for processing a fetched batch oldest-first.

    Gmail's messages.list returns newest-first. If a thread's newest
    message were processed before its original one, the original would
    be misclassified as a "reply" on an "existing" thread (running
    intent classification) instead of triggering new-enquiry
    extraction -- silently losing the real enquiry. Sorting the batch
    chronologically before processing guarantees the true first message
    of any thread is seen first. (This only orders messages within one
    fetched batch; if an original message falls outside a batch that
    only picks up a later reply, that's the same kind of edge case as
    the thread-continuity limitation documented in README.md.)
    """
    parsed = _parse_date(message.received_at)
    return parsed if parsed is not None else datetime.max.replace(tzinfo=timezone.utc)


def _to_iso(raw_date: Optional[str]) -> str:
    """Convert an RFC 2822 'Date' header into the UTC ISO 8601 string
    convention used throughout the database (see app.database.schema).
    Falls back to "now" if the header is missing or unparseable --
    logged, never raised, so one bad header doesn't abort a sync.
    """
    parsed = _parse_date(raw_date)
    if parsed is not None:
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    if raw_date:
        logger.warning("Could not parse email Date header %r", raw_date)
    return now_iso()


def _handle_new_thread(
    conn: sqlite3.Connection, message: EmailMessage, sender_type: str, received_at: str
) -> bool:
    """New Gmail thread: run enquiry-detection/extraction. Returns
    True if the message was fully processed (so it can be marked
    processed), False if it should be retried on the next sync.
    """
    logger.info("New thread detected: %s", message.gmail_thread_id)

    if sender_type == LastSender.EMPLOYEE:
        # A genuine new customer enquiry, by definition, starts with a
        # message from OUTSIDE the company. A thread whose first
        # message comes from our own domain is internal mail (e.g. one
        # employee asking a colleague for pricing) that can read as
        # enquiry-shaped in isolation but isn't a customer enquiry --
        # this happened in a live test run (an internal "kindly share
        # pricing" email got a false-positive is_enquiry=True). Skip
        # the Claude call entirely (deterministic, per project spec
        # section 17) rather than rely on Claude to infer it wasn't
        # given the sender's domain in the first place.
        logger.info(
            "Thread %s starts with an internal message; marking IGNORED without a Claude call",
            message.gmail_thread_id,
        )
        _create_enquiry(
            conn,
            gmail_thread_id=message.gmail_thread_id,
            customer_name=None,
            customer_email=None,
            subject=message.subject,
            product=None,
            quantity=None,
            last_sender=sender_type,
            received_at=received_at,
            status=Status.IGNORED,
        )
        return True

    extraction = extract_new_enquiry(message.subject, message.body)
    if extraction is None:
        logger.error(
            "Claude extraction failed for thread %s; leaving unprocessed for the next sync",
            message.gmail_thread_id,
        )
        return False

    # Fall back to the Gmail sender address only when the sender really
    # is the customer -- never attribute a customer_email to whichever
    # employee happened to start the thread.
    fallback_email = (
        extract_email_address(message.sender) if sender_type == LastSender.CUSTOMER else None
    )

    status = Status.NEW if extraction.is_enquiry else Status.IGNORED
    _create_enquiry(
        conn,
        gmail_thread_id=message.gmail_thread_id,
        customer_name=extraction.customer_name,
        customer_email=extraction.customer_email or fallback_email,
        subject=message.subject,
        product=extraction.product,
        quantity=extraction.quantity,
        last_sender=sender_type,
        received_at=received_at,
        status=status,
    )
    logger.info(
        "Thread %s: enquiry created with status %s", message.gmail_thread_id, status
    )
    return True


def _handle_existing_thread(
    conn: sqlite3.Connection,
    message: EmailMessage,
    existing_enquiry: Enquiry,
    sender_type: str,
    received_at: str,
) -> bool:
    """Existing Gmail thread: run reply-intent classification and apply
    the status business rules. Returns True if fully processed.
    """
    logger.info("Existing enquiry updated: %s", message.gmail_thread_id)

    intent = classify_reply_intent(message.body)
    if intent is None:
        logger.error(
            "Claude intent classification failed for thread %s; leaving unprocessed for the next sync",
            message.gmail_thread_id,
        )
        return False

    new_status = next_status_for_message(
        sender_type=sender_type,
        intent=intent,
        message_body=message.body,
        current_status=existing_enquiry.status,
    )

    if new_status == Status.CLOSED and existing_enquiry.status != Status.CLOSED:
        close_enquiry(conn, message.gmail_thread_id, closed_at=received_at)
        update_enquiry_activity(
            conn, message.gmail_thread_id, last_sender=sender_type, last_activity_at=received_at
        )
    else:
        update_enquiry_activity(
            conn,
            message.gmail_thread_id,
            last_sender=sender_type,
            last_activity_at=received_at,
            status=new_status,
        )

    return True


def sync_message(conn: sqlite3.Connection, message: EmailMessage) -> None:
    """Process one already-fetched Gmail message end to end."""
    existing_email = get_email_by_message_id(conn, message.gmail_message_id)
    if existing_email is not None and existing_email.processed:
        logger.info("Message %s already processed; skipping", message.gmail_message_id)
        return

    received_at = _to_iso(message.received_at)

    if existing_email is None:
        insert_email(
            conn,
            gmail_message_id=message.gmail_message_id,
            gmail_thread_id=message.gmail_thread_id,
            sender=message.sender,
            recipient=message.recipient,
            subject=message.subject,
            body=message.body,
            received_at=received_at,
        )
    else:
        logger.info("Retrying previously unprocessed message %s", message.gmail_message_id)

    sender_type = classify_sender(message.sender, COMPANY_EMAIL_DOMAIN)
    existing_enquiry = get_enquiry_by_thread(conn, message.gmail_thread_id)

    if existing_enquiry is None:
        success = _handle_new_thread(conn, message, sender_type, received_at)
    else:
        success = _handle_existing_thread(conn, message, existing_enquiry, sender_type, received_at)

    if success:
        mark_email_processed(conn, message.gmail_message_id)


def run_sync(conn: sqlite3.Connection, service, max_results: int = 25) -> int:
    """Fetch recent Gmail messages and ingest each one.

    Returns the number of messages fetched (not necessarily all newly
    processed -- already-seen ones are skipped, and one bad message is
    logged and skipped rather than aborting the whole run).
    """
    logger.info("Gmail sync started")
    messages = fetch_messages(service, max_results=max_results)
    logger.info("Fetched %d message(s) from Gmail", len(messages))

    # Process oldest-first within this batch -- see _sort_key.
    messages = sorted(messages, key=_sort_key)

    for message in messages:
        try:
            sync_message(conn, message)
        except Exception:
            logger.exception(
                "Unexpected error processing message %s; skipping", message.gmail_message_id
            )

    return len(messages)
