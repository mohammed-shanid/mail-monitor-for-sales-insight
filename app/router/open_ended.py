"""Open-ended founder questions about one specific enquiry (project
spec section 24) -- the one place Claude is used to *answer* a
founder's question, always grounded in that enquiry's real stored
email thread, never free-form chat.

Scope is deliberately narrow: project spec section 17 only lists
"summarizing an enquiry/thread" and "answering...open-ended questions
about a specific enquiry" as valid AI uses -- not general conversation.
So if the question can't be tied to a known customer already in our
own database, this returns None and the caller (app.router.intent)
sends a generic "I didn't understand" reply instead of handing
arbitrary text to Claude.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Optional

from app.ai.claude import summarize_enquiry
from app.database.queries import get_emails_by_thread, get_enquiry_by_customer
from app.enquiry.models import Enquiry

logger = logging.getLogger(__name__)

# Common lead-in phrasings for "tell me about a customer" questions
# (project spec section 1 examples: "Show ABC Industries", "What
# happened with ABC Industries?"). Illustrative, not exhaustive --
# find_relevant_enquiry() also tries the raw text as a bare customer
# name if none of these match.
_LEAD_IN_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"^what happened with\s+",
        r"^what(?:'s| is) happening with\s+",
        r"^what(?:'s| is) the status of\s+",
        r"^status of\s+",
        r"^show\s+",
        r"^tell me about\s+",
        r"^details? (?:of|for)\s+",
        r"^update on\s+",
    ]
]


def _extract_customer_query(text: str) -> Optional[str]:
    """Strip a recognized lead-in phrase to guess at a customer name,
    e.g. "What happened with ABC Industries?" -> "abc industries".
    Returns None if no recognized lead-in phrase is present.
    """
    normalized = text.strip().lower().rstrip("?!. ")
    for pattern in _LEAD_IN_PATTERNS:
        match = pattern.match(normalized)
        if match:
            candidate = normalized[match.end():].strip()
            return candidate or None
    return None


def find_relevant_enquiry(conn: sqlite3.Connection, text: str) -> Optional[Enquiry]:
    """Best-effort, deterministic match from free text to a known
    customer's most recent enquiry. No Claude involved for this lookup
    -- the universe of customer names already lives in our own
    database (project spec section 17: don't use AI for what's
    deterministic).
    """
    candidate = _extract_customer_query(text)
    if candidate:
        matches = get_enquiry_by_customer(conn, candidate)
        if matches:
            return matches[0]  # most recent -- see get_enquiry_by_customer's ordering

    # No recognized lead-in phrase (or it didn't match anyone) -- try
    # the raw text itself, in case it's just a bare customer name.
    matches = get_enquiry_by_customer(conn, text.strip())
    return matches[0] if matches else None


def answer_open_ended_question(conn: sqlite3.Connection, text: str) -> Optional[str]:
    """Try to answer a founder's open-ended question about a specific
    enquiry.

    Returns None only when no matching enquiry could be found at all --
    the caller sends a generic fallback reply in that case. If a match
    *was* found but Claude couldn't summarize it, this returns a
    specific "couldn't summarize" message instead of None, so the
    founder isn't told "I don't understand" about a customer we do
    have on file.
    """
    enquiry = find_relevant_enquiry(conn, text)
    if enquiry is None:
        return None

    emails = get_emails_by_thread(conn, enquiry.gmail_thread_id)
    if not emails:
        logger.warning("Enquiry %s has no stored emails to summarize", enquiry.gmail_thread_id)
        return "Sorry, I couldn't find any message history for that enquiry."

    thread_messages = [
        {"sender": email.sender, "body": email.body, "received_at": email.received_at}
        for email in emails
    ]

    summary = summarize_enquiry(
        customer_name=enquiry.customer_name,
        product=enquiry.product,
        quantity=enquiry.quantity,
        status=enquiry.status,
        thread_messages=thread_messages,
    )
    if summary is None:
        logger.error("Claude summarization failed for thread %s", enquiry.gmail_thread_id)
        return "Sorry, I couldn't summarize that enquiry right now -- please try again in a moment."

    return summary
