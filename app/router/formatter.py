"""Turns deterministic query results into the plain-text replies sent
back to the founder over WhatsApp/Telegram.

Kept separate from app.router.intent (matching) and
app.database.queries (data) so wording/emoji changes never touch
either of those. No Claude here either -- formatting is just string
building (project spec section 17).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional

from app.enquiry.models import Enquiry


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_elapsed(since: str, now: Optional[datetime] = None) -> str:
    """How long ago an ISO timestamp was, as "45m", "3h", or "2d 4h".

    `now` defaults to the current UTC time; tests pass a fixed value
    for a deterministic result.
    """
    now = now or datetime.now(timezone.utc)
    delta = now - _parse_iso(since)
    total_minutes = max(int(delta.total_seconds() // 60), 0)

    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)

    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h"
    return f"{minutes}m"


def format_count(label: str, count: int) -> str:
    """A one-line answer for a plain COUNT_* intent, e.g.
    "Open enquiries: 8".
    """
    return f"{label}: {count}"


def format_today_summary(summary: dict) -> str:
    """The small same-day dashboard for "how many enquiries today"
    (project spec section 24 worked example). `summary` is the dict
    returned by app.database.queries.get_today_summary().
    """
    return (
        "📩 Today's Enquiries\n\n"
        f"Received: {summary['received']}\n"
        f"Closed: {summary['closed']}\n"
        f"Open: {summary['open']}\n"
        f"Unanswered: {summary['unanswered']}"
    )


def format_enquiry_list(title: str, emoji: str, enquiries: Iterable[Enquiry]) -> str:
    """A numbered list of enquiries (project spec section 24 "Pending
    Enquiries" worked example): customer, product, received time, and
    elapsed time since received.

    Note: times are shown as stored, in UTC -- see the timezone caveat
    in app.database.queries.
    """
    enquiries = list(enquiries)
    if not enquiries:
        return f"{emoji} {title}\n\nNone right now."

    lines: List[str] = [f"{emoji} {title}", ""]
    for i, enquiry in enumerate(enquiries, start=1):
        # Spec's worked example keeps the leading zero (e.g. "09:12 AM").
        received_time = _parse_iso(enquiry.received_at).strftime("%I:%M %p")
        lines.append(f"{i}. {enquiry.customer_name or 'Unknown customer'}")
        lines.append(f"   Product: {enquiry.product or 'Not specified'}")
        lines.append(f"   Received: {received_time}")
        lines.append(f"   Waiting: {format_elapsed(enquiry.received_at)}")
        lines.append("")

    return "\n".join(lines).rstrip()
