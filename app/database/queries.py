"""Repository functions for the `emails` and `enquiries` tables.

Everything here is plain SQL through sqlite3 -- no ORM. Phase 4
(ingestion) is the only caller of the write functions; Phase 5 (founder
query layer) will add read-only count/filter functions on top of this
same module. Claude is never involved here (project spec section 17).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from app.enquiry.models import Email, Enquiry, LastSender, Status

logger = logging.getLogger(__name__)


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string, matching the timestamp
    convention documented in app.database.schema.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_email(row: sqlite3.Row) -> Email:
    return Email(
        id=row["id"],
        gmail_message_id=row["gmail_message_id"],
        gmail_thread_id=row["gmail_thread_id"],
        sender=row["sender"],
        recipient=row["recipient"],
        subject=row["subject"],
        body=row["body"],
        received_at=row["received_at"],
        processed=bool(row["processed"]),
    )


def _row_to_enquiry(row: sqlite3.Row) -> Enquiry:
    return Enquiry(
        id=row["id"],
        gmail_thread_id=row["gmail_thread_id"],
        customer_name=row["customer_name"],
        customer_email=row["customer_email"],
        subject=row["subject"],
        product=row["product"],
        quantity=row["quantity"],
        status=row["status"],
        last_sender=row["last_sender"],
        received_at=row["received_at"],
        last_activity_at=row["last_activity_at"],
        closed_at=row["closed_at"],
    )


# --- emails ---------------------------------------------------------------


def email_exists(conn: sqlite3.Connection, gmail_message_id: str) -> bool:
    """True if this Gmail message has already been synced.

    This is the idempotency check (project spec section 28):
    gmail_message_id is the message's identity, and Phase 4 must skip
    messages that are already in the database rather than reprocessing
    them.
    """
    row = conn.execute(
        "SELECT 1 FROM emails WHERE gmail_message_id = ?", (gmail_message_id,)
    ).fetchone()
    return row is not None


def insert_email(
    conn: sqlite3.Connection,
    *,
    gmail_message_id: str,
    gmail_thread_id: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    received_at: Optional[str],
    processed: bool = False,
) -> Optional[Email]:
    """Insert one email row. Returns the inserted Email, or None if a
    row with this gmail_message_id already exists (duplicate sync --
    logged, not an error).
    """
    if email_exists(conn, gmail_message_id):
        logger.info("Email %s already processed; skipping insert", gmail_message_id)
        return None

    cursor = conn.execute(
        """
        INSERT INTO emails
            (gmail_message_id, gmail_thread_id, sender, recipient, subject, body, received_at, processed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (gmail_message_id, gmail_thread_id, sender, recipient, subject, body, received_at, int(processed)),
    )
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _row_to_email(row)


def get_email_by_message_id(conn: sqlite3.Connection, gmail_message_id: str) -> Optional[Email]:
    row = conn.execute(
        "SELECT * FROM emails WHERE gmail_message_id = ?", (gmail_message_id,)
    ).fetchone()
    return _row_to_email(row) if row else None


def mark_email_processed(conn: sqlite3.Connection, gmail_message_id: str) -> None:
    """Flag an email as processed once its enquiry/status handling is
    done, so a later sync run can tell it apart from an email that was
    fetched but not yet acted on (e.g. a Claude call failed).
    """
    conn.execute(
        "UPDATE emails SET processed = 1 WHERE gmail_message_id = ?", (gmail_message_id,)
    )


# --- enquiries --------------------------------------------------------------


def get_enquiry_by_thread(conn: sqlite3.Connection, gmail_thread_id: str) -> Optional[Enquiry]:
    """The new-vs-existing-thread check Phase 4's branching rule (project
    spec section 11) is built on: does an enquiry already exist for
    this Gmail thread?
    """
    row = conn.execute(
        "SELECT * FROM enquiries WHERE gmail_thread_id = ?", (gmail_thread_id,)
    ).fetchone()
    return _row_to_enquiry(row) if row else None


def create_enquiry(
    conn: sqlite3.Connection,
    *,
    gmail_thread_id: str,
    customer_name: Optional[str],
    customer_email: Optional[str],
    subject: str,
    product: Optional[str],
    quantity: Optional[int],
    last_sender: str,
    received_at: str,
    status: str = Status.NEW,
    last_activity_at: Optional[str] = None,
) -> Enquiry:
    """Create a new enquiry for a Gmail thread we haven't seen before.

    Idempotent: if the thread already has an enquiry (e.g. a retried
    sync, or a race), this logs a warning and returns the existing row
    instead of raising or creating a duplicate -- gmail_thread_id is
    UNIQUE (project spec section 28).
    """
    if status not in Status.ALL:
        raise ValueError(f"Invalid status: {status!r}")
    if last_sender not in LastSender.ALL:
        raise ValueError(f"Invalid last_sender: {last_sender!r}")

    existing = get_enquiry_by_thread(conn, gmail_thread_id)
    if existing is not None:
        logger.warning(
            "create_enquiry called for a thread that already exists (%s); returning existing row",
            gmail_thread_id,
        )
        return existing

    last_activity_at = last_activity_at or received_at

    cursor = conn.execute(
        """
        INSERT INTO enquiries
            (gmail_thread_id, customer_name, customer_email, subject, product,
             quantity, status, last_sender, received_at, last_activity_at, closed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            gmail_thread_id,
            customer_name,
            customer_email,
            subject,
            product,
            quantity,
            status,
            last_sender,
            received_at,
            last_activity_at,
        ),
    )
    row = conn.execute("SELECT * FROM enquiries WHERE id = ?", (cursor.lastrowid,)).fetchone()
    logger.info("New enquiry created for thread %s", gmail_thread_id)
    return _row_to_enquiry(row)


def update_enquiry_activity(
    conn: sqlite3.Connection,
    gmail_thread_id: str,
    *,
    last_sender: str,
    last_activity_at: str,
    status: Optional[str] = None,
) -> Optional[Enquiry]:
    """Record a new message on an existing enquiry: who sent it and
    when (project spec section 14), and optionally move `status`.

    This does NOT decide the status -- that's app.enquiry.status
    (Phase 4), applying Claude's intent classification plus business
    rules. This function just persists whatever the caller decided.
    Returns None if the thread doesn't exist.
    """
    if last_sender not in LastSender.ALL:
        raise ValueError(f"Invalid last_sender: {last_sender!r}")
    if status is not None and status not in Status.ALL:
        raise ValueError(f"Invalid status: {status!r}")

    if get_enquiry_by_thread(conn, gmail_thread_id) is None:
        logger.error("update_enquiry_activity: no enquiry for thread %s", gmail_thread_id)
        return None

    if status is not None:
        conn.execute(
            """
            UPDATE enquiries
            SET last_sender = ?, last_activity_at = ?, status = ?
            WHERE gmail_thread_id = ?
            """,
            (last_sender, last_activity_at, status, gmail_thread_id),
        )
    else:
        conn.execute(
            """
            UPDATE enquiries
            SET last_sender = ?, last_activity_at = ?
            WHERE gmail_thread_id = ?
            """,
            (last_sender, last_activity_at, gmail_thread_id),
        )

    return get_enquiry_by_thread(conn, gmail_thread_id)


def close_enquiry(
    conn: sqlite3.Connection, gmail_thread_id: str, closed_at: Optional[str] = None
) -> Optional[Enquiry]:
    """Mark an enquiry CLOSED. Conservative by design (project spec
    section 16) -- this is a separate, explicit call, never a side
    effect of updating activity, so a caller can never close an
    enquiry by accident while just recording a reply.
    """
    closed_at = closed_at or now_iso()

    if get_enquiry_by_thread(conn, gmail_thread_id) is None:
        logger.error("close_enquiry: no enquiry for thread %s", gmail_thread_id)
        return None

    conn.execute(
        "UPDATE enquiries SET status = ?, closed_at = ? WHERE gmail_thread_id = ?",
        (Status.CLOSED, closed_at, gmail_thread_id),
    )
    logger.info("Enquiry closed for thread %s", gmail_thread_id)
    return get_enquiry_by_thread(conn, gmail_thread_id)


# --- founder query layer (Phase 5) ------------------------------------------
#
# Everything below is read-only, plain SQL, and answers a fixed founder
# question deterministically -- no Claude involved (project spec
# section 17: "Do not use Claude to count database records").
#
# Design notes:
#   - IGNORED enquiries (messages Claude decided weren't genuine
#     enquiries) are excluded from every count/list here -- the founder
#     asking "how many enquiries today" means real enquiries, and it
#     keeps the arithmetic honest: received == open + closed.
#   - "Today" / "this month" are computed in UTC (SQLite's `date('now')`
#     default), matching the UTC storage convention in
#     app.database.schema. For a India-based business this can be off
#     by up to ~5.5 hours around local midnight -- a known V1
#     simplification, not a bug; a later version could apply a
#     configured display timezone.
#   - "Unanswered" (project spec section 21) is last_sender == customer
#     AND not already CLOSED/IGNORED -- a closed thread never needs a
#     reply, no matter who sent the final message.

_NON_ENQUIRY_STATUSES = (Status.IGNORED,)
_TERMINAL_STATUSES = (Status.CLOSED, Status.IGNORED)


def count_today(conn: sqlite3.Connection) -> int:
    """How many genuine enquiries were received today (UTC)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiries "
        "WHERE date(received_at) = date('now') AND status NOT IN (?)",
        _NON_ENQUIRY_STATUSES,
    ).fetchone()
    return row["n"]


def count_this_month(conn: sqlite3.Connection) -> int:
    """How many genuine enquiries were received this calendar month (UTC)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiries "
        "WHERE strftime('%Y-%m', received_at) = strftime('%Y-%m', 'now') AND status NOT IN (?)",
        _NON_ENQUIRY_STATUSES,
    ).fetchone()
    return row["n"]


def count_open(conn: sqlite3.Connection) -> int:
    """Enquiries still in progress (not CLOSED, not IGNORED)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiries WHERE status NOT IN (?, ?)",
        _TERMINAL_STATUSES,
    ).fetchone()
    return row["n"]


def count_closed(conn: sqlite3.Connection) -> int:
    """Enquiries that reached a final outcome (accepted or rejected)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM enquiries WHERE status = ?", (Status.CLOSED,)
    ).fetchone()
    return row["n"]


def get_today_summary(conn: sqlite3.Connection) -> dict:
    """The small dashboard the founder gets for "how many enquiries
    today" (project spec section 24): how many of TODAY's enquiries are
    closed, still open, and unanswered -- not the running totals from
    count_open()/count_closed(), which cover every enquiry ever.
    """
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS received,
            SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS closed,
            SUM(CASE WHEN status NOT IN (?, ?) THEN 1 ELSE 0 END) AS open,
            SUM(CASE WHEN last_sender = ? AND status NOT IN (?, ?) THEN 1 ELSE 0 END) AS unanswered
        FROM enquiries
        WHERE date(received_at) = date('now') AND status NOT IN (?)
        """,
        (
            Status.CLOSED,
            Status.CLOSED,
            Status.IGNORED,
            LastSender.CUSTOMER,
            Status.CLOSED,
            Status.IGNORED,
            Status.IGNORED,
        ),
    ).fetchone()
    return {
        "received": row["received"] or 0,
        "closed": row["closed"] or 0,
        "open": row["open"] or 0,
        "unanswered": row["unanswered"] or 0,
    }


def get_unanswered(conn: sqlite3.Connection) -> List[Enquiry]:
    """Enquiries waiting on an employee reply (project spec section
    21): last_sender is the customer, and the thread isn't already
    finished. Oldest-waiting first.
    """
    rows = conn.execute(
        "SELECT * FROM enquiries WHERE last_sender = ? AND status NOT IN (?, ?) "
        "ORDER BY last_activity_at ASC",
        (LastSender.CUSTOMER, Status.CLOSED, Status.IGNORED),
    ).fetchall()
    return [_row_to_enquiry(row) for row in rows]


def get_today_enquiries(conn: sqlite3.Connection) -> List[Enquiry]:
    """Today's genuine enquiries, oldest first."""
    rows = conn.execute(
        "SELECT * FROM enquiries WHERE date(received_at) = date('now') AND status NOT IN (?) "
        "ORDER BY received_at ASC",
        _NON_ENQUIRY_STATUSES,
    ).fetchall()
    return [_row_to_enquiry(row) for row in rows]


def get_open_enquiries(conn: sqlite3.Connection) -> List[Enquiry]:
    """Every still-open enquiry (not CLOSED, not IGNORED), oldest first."""
    rows = conn.execute(
        "SELECT * FROM enquiries WHERE status NOT IN (?, ?) ORDER BY received_at ASC",
        _TERMINAL_STATUSES,
    ).fetchall()
    return [_row_to_enquiry(row) for row in rows]


def get_enquiry_by_customer(conn: sqlite3.Connection, name_query: str) -> List[Enquiry]:
    """Enquiries whose customer_name contains `name_query`
    (case-insensitive), most recent first -- used for "show ABC
    Industries" / "what happened with ABC Industries" style questions.
    """
    rows = conn.execute(
        "SELECT * FROM enquiries WHERE customer_name LIKE ? ESCAPE '\\' ORDER BY received_at DESC",
        (f"%{_escape_like(name_query)}%",),
    ).fetchall()
    return [_row_to_enquiry(row) for row in rows]


def get_emails_by_thread(conn: sqlite3.Connection, gmail_thread_id: str) -> List[Email]:
    """Every stored email belonging to one Gmail thread, oldest first --
    the real thread history Claude summarizes for an open-ended founder
    question (project spec section 24), never anything invented.
    """
    rows = conn.execute(
        "SELECT * FROM emails WHERE gmail_thread_id = ? ORDER BY received_at ASC",
        (gmail_thread_id,),
    ).fetchall()
    return [_row_to_email(row) for row in rows]


def _escape_like(value: str) -> str:
    """Escape SQL LIKE wildcards in user-supplied text so a customer
    name containing '%' or '_' can't alter the query's meaning.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
