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

from app.enquiry.models import Direction, Email, Enquiry, LastSender, ReportEmail, Status

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


# =============================================================================
# Report path (SPEC.md), added Stage 2. Everything above is untouched and
# still used by the bot, against its own old-shaped tables (see
# app.database.schema module docstring). Everything below targets the
# report schema's `emails`/`enquiries` tables (app.database.db.
# get_report_connection) and is never called by the bot.
# =============================================================================


def _row_to_report_email(row: sqlite3.Row) -> ReportEmail:
    return ReportEmail(
        id=row["id"],
        gmail_message_id=row["gmail_message_id"],
        gmail_thread_id=row["gmail_thread_id"],
        sender=row["sender"],
        sender_domain=row["sender_domain"],
        recipient=row["recipient"],
        subject=row["subject"],
        body=row["body"],
        received_at=row["received_at"],
        direction=row["direction"],
        is_auto_reply=bool(row["is_auto_reply"]),
        has_attachments=bool(row["has_attachments"]),
        ingested_at=row["ingested_at"],
        processed=bool(row["processed"]),
    )


def report_email_exists(conn: sqlite3.Connection, gmail_message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM emails WHERE gmail_message_id = ?", (gmail_message_id,)
    ).fetchone()
    return row is not None


def upsert_report_email(
    conn: sqlite3.Connection,
    *,
    gmail_message_id: str,
    gmail_thread_id: str,
    sender: str,
    sender_domain: Optional[str],
    recipient: Optional[str],
    subject: Optional[str],
    body: Optional[str],
    received_at: int,
    direction: str,
    is_auto_reply: bool,
    has_attachments: bool,
    ingested_at: int,
) -> bool:
    """Idempotent insert into the report path's `emails` table
    (SPEC.md §18.1: `gmail_message_id` UNIQUE, `INSERT ... ON CONFLICT
    DO NOTHING`). Returns True if a new row was inserted, False if this
    message was already present -- re-running an ingest over the same
    window never duplicates a row.
    """
    if direction not in Direction.ALL:
        raise ValueError(f"Invalid direction: {direction!r}")

    cursor = conn.execute(
        """
        INSERT INTO emails
            (gmail_message_id, gmail_thread_id, sender, sender_domain, recipient,
             subject, body, received_at, direction, is_auto_reply, has_attachments,
             ingested_at, processed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(gmail_message_id) DO NOTHING
        """,
        (
            gmail_message_id,
            gmail_thread_id,
            sender,
            sender_domain,
            recipient,
            subject,
            body,
            received_at,
            direction,
            int(is_auto_reply),
            int(has_attachments),
            ingested_at,
        ),
    )
    return cursor.rowcount > 0


def get_report_email_by_message_id(
    conn: sqlite3.Connection, gmail_message_id: str
) -> Optional[ReportEmail]:
    row = conn.execute(
        "SELECT * FROM emails WHERE gmail_message_id = ?", (gmail_message_id,)
    ).fetchone()
    return _row_to_report_email(row) if row else None


def get_report_emails_by_thread(
    conn: sqlite3.Connection, gmail_thread_id: str
) -> List[ReportEmail]:
    """Every stored report-path email for one Gmail thread, oldest
    first -- the full hydrated thread that enquiry state (Stage 3) is
    computed from.
    """
    rows = conn.execute(
        "SELECT * FROM emails WHERE gmail_thread_id = ? ORDER BY received_at ASC",
        (gmail_thread_id,),
    ).fetchall()
    return [_row_to_report_email(row) for row in rows]


def count_report_emails(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()
    return row["n"]


def count_report_threads(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(DISTINCT gmail_thread_id) AS n FROM emails").fetchone()
    return row["n"]


def get_touched_thread_ids(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> List[str]:
    """Distinct `gmail_thread_id` values with >=1 stored message whose
    `received_at` falls inside `[start_ms, end_ms]` inclusive --
    SPEC.md §0's "touched thread" definition. Re-derived from the DB
    (not carried over from the ingest step) so analysis can run
    against whatever is actually stored, including from an earlier run.
    """
    rows = conn.execute(
        "SELECT DISTINCT gmail_thread_id FROM emails WHERE received_at >= ? AND received_at <= ?",
        (start_ms, end_ms),
    ).fetchall()
    return [row["gmail_thread_id"] for row in rows]


def get_schema_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# =============================================================================
# AI verdict cache (SPEC.md §14.2, §15.2, §18.2), added Stage 3.
# =============================================================================


def get_ai_verdict_row(
    conn: sqlite3.Connection, gmail_message_id: str, prompt_version: str
) -> Optional[sqlite3.Row]:
    """The raw cached row for one (message, prompt_version), or None on
    a cache miss. Returns the row (not just the JSON) so callers can
    also see `model`/`created_at` without a second query.
    """
    return conn.execute(
        "SELECT * FROM ai_verdicts WHERE gmail_message_id = ? AND prompt_version = ?",
        (gmail_message_id, prompt_version),
    ).fetchone()


def upsert_ai_verdict(
    conn: sqlite3.Connection,
    *,
    gmail_message_id: str,
    prompt_version: str,
    model: str,
    verdict_json: str,
    created_at: int,
) -> None:
    """Write (or overwrite, for `--reprocess`) the cached verdict for
    one (message, prompt_version). PRIMARY KEY is the pair, so a
    re-analysis of the same message under the same prompt version
    replaces the row rather than duplicating it.
    """
    conn.execute(
        """
        INSERT INTO ai_verdicts (gmail_message_id, prompt_version, model, verdict_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(gmail_message_id, prompt_version) DO UPDATE SET
            model = excluded.model,
            verdict_json = excluded.verdict_json,
            created_at = excluded.created_at
        """,
        (gmail_message_id, prompt_version, model, verdict_json, created_at),
    )


def count_ai_verdicts(conn: sqlite3.Connection, prompt_version: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ai_verdicts WHERE prompt_version = ?", (prompt_version,)
    ).fetchone()
    return row["n"]


# =============================================================================
# `enquiries` / `enquiry_brands` / `report_runs` (SPEC.md §14.2), added
# Stage 4. `enquiries` is a cache of the most recently completed run's
# as-of state, not history -- `report_runs` is the durable artefact
# (see the comment above the CREATE TABLE statements in schema.py).
# =============================================================================


def upsert_enquiry_state(
    conn: sqlite3.Connection,
    *,
    mailbox: str,
    state,  # app.enquiry.models.EnquiryState
    computed_as_of: int,
    updated_at: int,
) -> int:
    """Write one thread's current EnquiryState, replacing whatever was
    there before for that `gmail_thread_id` (SPEC.md §14.2: `enquiries`
    is a most-recent-run cache, always fully recomputed -- SPEC.md
    §8.1 -- never patched field-by-field). Returns the row id.
    """
    conn.execute(
        """
        INSERT INTO enquiries (
            gmail_thread_id, mailbox, customer_name, company, customer_email,
            counterparty_type, subject, product, requirement, quantity, status,
            last_sender, is_priority, priority_evidence, received_at,
            last_activity_at, closed_at, closure_evidence, closure_kind,
            computed_as_of, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gmail_thread_id) DO UPDATE SET
            mailbox = excluded.mailbox,
            customer_name = excluded.customer_name,
            company = excluded.company,
            customer_email = excluded.customer_email,
            counterparty_type = excluded.counterparty_type,
            subject = excluded.subject,
            product = excluded.product,
            requirement = excluded.requirement,
            quantity = excluded.quantity,
            status = excluded.status,
            last_sender = excluded.last_sender,
            is_priority = excluded.is_priority,
            priority_evidence = excluded.priority_evidence,
            received_at = excluded.received_at,
            last_activity_at = excluded.last_activity_at,
            closed_at = excluded.closed_at,
            closure_evidence = excluded.closure_evidence,
            closure_kind = excluded.closure_kind,
            computed_as_of = excluded.computed_as_of,
            updated_at = excluded.updated_at
        """,
        (
            state.gmail_thread_id, mailbox, state.customer_name, state.company, state.customer_email,
            state.counterparty_type, state.subject, state.product, state.requirement, state.quantity,
            state.status, state.last_sender, int(state.is_priority), state.priority_evidence,
            state.received_at, state.last_activity_at, state.closed_at, state.closure_evidence,
            state.closure_kind, computed_as_of, updated_at,
        ),
    )
    row = conn.execute(
        "SELECT id FROM enquiries WHERE gmail_thread_id = ?", (state.gmail_thread_id,)
    ).fetchone()
    return row["id"]


def replace_enquiry_brands(conn: sqlite3.Connection, enquiry_id: int, brands: List[str]) -> None:
    """Replace the full brand set for one enquiry -- state is always
    fully recomputed (SPEC.md §8.1), so the brand set is too, never
    incrementally patched. `ON DELETE CASCADE` on enquiry_brands
    handles cleanup if the enquiry row itself is ever removed.
    """
    conn.execute("DELETE FROM enquiry_brands WHERE enquiry_id = ?", (enquiry_id,))
    for brand in dict.fromkeys(brands):  # de-dup, preserve order
        conn.execute(
            "INSERT INTO enquiry_brands (enquiry_id, brand, raw_mention) VALUES (?, ?, ?)",
            (enquiry_id, brand, brand),
        )


def insert_report_run(
    conn: sqlite3.Connection,
    *,
    window_start: int,
    window_end: int,
    mode: str,
    mailbox: str,
    metrics_json: str,
    report_text: str,
    delivered: bool,
    delivery_error: Optional[str],
    created_at: int,
) -> int:
    """Every delivered (or attempted) report, for audit and re-send
    (SPEC.md §14.2) -- the durable, reproducible artefact for this
    window, independent of whatever `enquiries` looks like later.
    """
    cursor = conn.execute(
        """
        INSERT INTO report_runs (
            window_start, window_end, mode, mailbox, metrics_json, report_text,
            delivered, delivery_error, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (window_start, window_end, mode, mailbox, metrics_json, report_text, int(delivered), delivery_error, created_at),
    )
    return cursor.lastrowid
