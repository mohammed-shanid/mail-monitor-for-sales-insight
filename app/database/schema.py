"""SQLite schema for the two V1 tables.

Deliberately just two tables (project spec section 13) -- no
customers, employees, or quotations tables in V1.

Timestamp convention: `received_at`, `last_activity_at`, and
`closed_at` are all stored as UTC ISO 8601 strings, e.g.
"2026-09-12T16:45:41+00:00" (see app.database.queries.now_iso()).
SQLite's date()/datetime()/strftime() all parse this format directly,
which is what the Phase 5 founder-query functions (count_today,
count_this_month, ...) rely on.
"""

from __future__ import annotations

import sqlite3

CREATE_EMAILS_TABLE = """
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_message_id TEXT NOT NULL UNIQUE,
    gmail_thread_id TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    received_at TEXT,
    processed INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_EMAILS_THREAD_INDEX = """
CREATE INDEX IF NOT EXISTS idx_emails_gmail_thread_id
    ON emails (gmail_thread_id)
"""

# enquiries.gmail_thread_id is UNIQUE -- that's the idempotency guarantee
# from project spec section 28 ("gmail_thread_id should be unique in
# enquiries"), and it's exactly the lookup Phase 4's new-vs-existing
# thread check needs.
CREATE_ENQUIRIES_TABLE = """
CREATE TABLE IF NOT EXISTS enquiries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_thread_id TEXT NOT NULL UNIQUE,
    customer_name TEXT,
    customer_email TEXT,
    subject TEXT NOT NULL DEFAULT '',
    product TEXT,
    quantity INTEGER,
    status TEXT NOT NULL DEFAULT 'NEW',
    last_sender TEXT NOT NULL DEFAULT 'customer',
    received_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    closed_at TEXT
)
"""

CREATE_ENQUIRIES_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_enquiries_status ON enquiries (status)
"""

ALL_STATEMENTS = [
    CREATE_EMAILS_TABLE,
    CREATE_EMAILS_THREAD_INDEX,
    CREATE_ENQUIRIES_TABLE,
    CREATE_ENQUIRIES_STATUS_INDEX,
]


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they don't already exist.

    Safe to call on every startup/connection -- CREATE TABLE/INDEX IF
    NOT EXISTS is idempotent, so re-running it never touches existing
    data (project spec section 28: sync must not duplicate records).
    """
    cursor = conn.cursor()
    for statement in ALL_STATEMENTS:
        cursor.execute(statement)
    conn.commit()
