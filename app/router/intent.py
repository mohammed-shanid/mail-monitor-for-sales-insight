"""Deterministic intent matching for founder questions.

Every FIXED question the founder can ask (project spec section 19) is
matched here via simple, tolerant phrase matching -- lowercased,
punctuation/apostrophe-insensitive, substring-based. No Claude call for
any of these: a known intent always resolves through plain
Python/SQL (project spec section 17).

The phrase lists below are illustrative ("match phrases such as..." in
the spec), not exhaustive -- match_intent() is deliberately tolerant
(missing apostrophes, extra whitespace, punctuation) rather than
requiring an exact string. handle_founder_question() is the full
deterministic pipeline: match -> query SQLite -> format text. It
returns None when nothing fixed matches, which is the caller's signal
(Phase 6) to fall back to Claude for an open-ended question.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from app.database import queries
from app.router.formatter import (
    format_count,
    format_enquiry_list,
    format_today_summary,
)
from app.router.open_ended import answer_open_ended_question


class Intent:
    COUNT_TODAY = "COUNT_TODAY"
    COUNT_MONTH = "COUNT_MONTH"
    COUNT_OPEN = "COUNT_OPEN"
    COUNT_CLOSED = "COUNT_CLOSED"
    GET_UNANSWERED = "GET_UNANSWERED"
    GET_TODAY = "GET_TODAY"
    GET_OPEN_DETAILS = "GET_OPEN_DETAILS"


# Checked top-to-bottom, first match wins. More specific "show me the
# details" phrasings are listed before the broader count phrasings they
# would otherwise be shadowed by (e.g. GET_OPEN_DETAILS's "show open
# enquiries" must be checked before COUNT_OPEN's "open enquiries",
# since the latter is a substring of the former).
_INTENT_ALIASES = [
    (
        Intent.GET_UNANSWERED,
        [
            "which are unanswered",
            "show pending response",
            "who hasn't replied",
            "who has not replied",
            "which customers are waiting",
            "unanswered enquiries",
            "unanswered",
        ],
    ),
    (
        Intent.GET_TODAY,
        [
            "show today's enquiries",
            "today's enquiry details",
            "list today's enquiries",
            "show today's enquiry details",
        ],
    ),
    (
        Intent.GET_OPEN_DETAILS,
        [
            "show open enquiries",
            "list pending enquiries",
            "details of open enquiries",
            # Not in the spec's illustrative list, but this exact phrase
            # is the worked example in project spec sections 1 and 24 --
            # included explicitly so that example matches.
            "show pending enquiries",
        ],
    ),
    (
        Intent.COUNT_TODAY,
        [
            "how many enquiries today",
            "enquiries today",
            "today's enquiries",
            "how many came today",
            "today enquiry count",
        ],
    ),
    (
        Intent.COUNT_MONTH,
        [
            "how many this month",
            "monthly enquiries",
            "this month's enquiries",
            "enquiries this month",
        ],
    ),
    (
        Intent.COUNT_CLOSED,
        [
            "how many closed",
            "how many are closed",
            "closed enquiries",
            "how many completed",
            "closed this month",
        ],
    ),
    (
        # "pending enquiries" / "which are open" read like they want a
        # list, but the project spec explicitly categorizes them as
        # COUNT_OPEN aliases (section 19) -- followed literally here.
        # Anyone who actually wants the list uses "show"/"list"/
        # "details of" (GET_OPEN_DETAILS above), matched first.
        Intent.COUNT_OPEN,
        [
            "how many open",
            "how many are open",
            "open enquiries",
            "pending enquiries",
            "what's pending",
            "which are open",
        ],
    ),
]


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("'", "")  # today's -> todays, what's -> whats
    text = re.sub(r"[^\w\s]", " ", text)  # drop punctuation (?, ., !, ...)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def match_intent(text: str) -> Optional[str]:
    """Return the matched Intent constant, or None if `text` doesn't
    look like any of the fixed founder questions.
    """
    normalized = _normalize(text)
    if not normalized:
        return None

    for intent, phrases in _INTENT_ALIASES:
        for phrase in phrases:
            if _normalize(phrase) in normalized:
                return intent
    return None


def handle_founder_question(conn: sqlite3.Connection, text: str) -> Optional[str]:
    """Full deterministic pipeline for a founder's WhatsApp/Telegram
    message: match a fixed intent, query SQLite, format the reply.

    Returns None when `text` doesn't match any fixed intent -- the
    caller (Phase 6) should then treat it as an open-ended question
    and fall back to Claude.
    """
    intent = match_intent(text)
    if intent is None:
        return None

    if intent == Intent.COUNT_TODAY:
        return format_today_summary(queries.get_today_summary(conn))

    if intent == Intent.COUNT_MONTH:
        return format_count("This month's enquiries", queries.count_this_month(conn))

    if intent == Intent.COUNT_OPEN:
        return format_count("Open enquiries", queries.count_open(conn))

    if intent == Intent.COUNT_CLOSED:
        return format_count("Closed enquiries", queries.count_closed(conn))

    if intent == Intent.GET_UNANSWERED:
        return format_enquiry_list("Unanswered Enquiries", "⏳", queries.get_unanswered(conn))

    if intent == Intent.GET_TODAY:
        return format_enquiry_list("Today's Enquiries", "📩", queries.get_today_enquiries(conn))

    if intent == Intent.GET_OPEN_DETAILS:
        return format_enquiry_list("Pending Enquiries", "⏳", queries.get_open_enquiries(conn))

    # Every Intent value above is handled -- reaching here would be a
    # programming error (a new Intent added without a handler), not a
    # user-input problem.
    raise AssertionError(f"No handler wired up for intent: {intent}")


_FALLBACK_MESSAGE = (
    "Sorry, I didn't understand that. Try asking things like:\n"
    "- How many enquiries today?\n"
    "- How many are open?\n"
    "- Show pending enquiries\n"
    "- Show ABC Industries"
)


def route_message(conn: sqlite3.Connection, text: str) -> str:
    """Top-level entry point for one incoming founder message (project
    spec section 23's full router flow), used by both the Telegram bot
    (Phase 6) and, later, the WhatsApp webhook:

        fixed intent?     -> SQLite + formatter (this module)
        known customer?   -> Claude summarizes their real thread history
        neither           -> a generic "try asking..." fallback

    Always returns a string -- never None -- so the caller always has
    something to send back.
    """
    fixed_answer = handle_founder_question(conn, text)
    if fixed_answer is not None:
        return fixed_answer

    open_ended_answer = answer_open_ended_question(conn, text)
    if open_ended_answer is not None:
        return open_ended_answer

    return _FALLBACK_MESSAGE
