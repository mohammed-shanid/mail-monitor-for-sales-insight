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


# =============================================================================
# Report path (SPEC.md §14), added Stage 2.
#
# `init_db()`/`ALL_STATEMENTS` above are UNCHANGED and still used by the bot
# (app.database.db.get_connection, scripts/sync_gmail.py, run_telegram_bot.py,
# and their tests) -- their `emails`/`enquiries` tables have a different
# column set and status vocabulary from the ones below, and the bot never
# touches this code path (Appendix A #11: the two must never both write to
# `enquiries`). report.py uses app.database.db.get_report_connection(),
# defined alongside this, exclusively.
#
# The live `data/regency.db` file currently holds the BOT's `emails`/
# `enquiries` tables. migrate() detects that shape (missing the `mailbox`
# column, which the report schema always has) and rebuilds them in place --
# a backed-up DROP + CREATE, per SPEC.md §14.1 and PHASE0_DECISIONS.md Q3.
# The file-level backup itself happens in db.py, before this module ever
# sees a writable connection; migrate() assumes that has already happened.
# =============================================================================

REPORT_SCHEMA_VERSION = "1"

CREATE_SCHEMA_META_TABLE = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key     TEXT PRIMARY KEY,
  value   TEXT NOT NULL
)
"""

CREATE_REPORT_EMAILS_TABLE = """
CREATE TABLE IF NOT EXISTS emails (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  gmail_message_id   TEXT    NOT NULL UNIQUE,
  gmail_thread_id    TEXT    NOT NULL,
  sender             TEXT    NOT NULL,
  sender_domain      TEXT,
  recipient          TEXT,
  subject            TEXT,
  body               TEXT,
  received_at        INTEGER NOT NULL,
  direction          TEXT    NOT NULL,
  is_auto_reply      INTEGER NOT NULL DEFAULT 0,
  has_attachments    INTEGER NOT NULL DEFAULT 0,
  ingested_at        INTEGER NOT NULL,
  processed          INTEGER NOT NULL DEFAULT 0
)
"""

CREATE_REPORT_EMAILS_THREAD_INDEX = """
CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(gmail_thread_id)
"""

CREATE_REPORT_EMAILS_RECEIVED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at)
"""

CREATE_REPORT_ENQUIRIES_TABLE = """
CREATE TABLE IF NOT EXISTS enquiries (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  gmail_thread_id    TEXT    NOT NULL UNIQUE,
  mailbox            TEXT    NOT NULL,
  customer_name      TEXT,
  company            TEXT,
  customer_email     TEXT,
  counterparty_type  TEXT,
  subject            TEXT,
  product            TEXT,
  requirement        TEXT,
  quantity           TEXT,
  status             TEXT    NOT NULL,
  last_sender        TEXT,
  is_priority        INTEGER NOT NULL DEFAULT 0,
  priority_evidence  TEXT,
  received_at        INTEGER NOT NULL,
  last_activity_at   INTEGER,
  closed_at          INTEGER,
  closure_evidence   TEXT,
  closure_kind       TEXT,
  computed_as_of     INTEGER,
  updated_at         INTEGER NOT NULL
)
"""

CREATE_REPORT_ENQUIRIES_RECEIVED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_enq_received ON enquiries(received_at)
"""

CREATE_REPORT_ENQUIRIES_STATUS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_enq_status ON enquiries(status)
"""

CREATE_ENQUIRY_BRANDS_TABLE = """
CREATE TABLE IF NOT EXISTS enquiry_brands (
  enquiry_id   INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  brand        TEXT    NOT NULL,
  raw_mention  TEXT,
  PRIMARY KEY (enquiry_id, brand)
)
"""

CREATE_AI_VERDICTS_TABLE = """
CREATE TABLE IF NOT EXISTS ai_verdicts (
  gmail_message_id  TEXT    NOT NULL,
  prompt_version    TEXT    NOT NULL,
  model             TEXT    NOT NULL,
  verdict_json      TEXT    NOT NULL,
  created_at        INTEGER NOT NULL,
  PRIMARY KEY (gmail_message_id, prompt_version)
)
"""

CREATE_REPORT_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS report_runs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  window_start   INTEGER NOT NULL,
  window_end     INTEGER NOT NULL,
  mode           TEXT    NOT NULL,
  mailbox        TEXT    NOT NULL,
  metrics_json   TEXT    NOT NULL,
  report_text    TEXT    NOT NULL,
  delivered      INTEGER NOT NULL DEFAULT 0,
  delivery_error TEXT,
  created_at     INTEGER NOT NULL
)
"""

REPORT_SCHEMA_STATEMENTS = [
    CREATE_SCHEMA_META_TABLE,
    CREATE_REPORT_EMAILS_TABLE,
    CREATE_REPORT_EMAILS_THREAD_INDEX,
    CREATE_REPORT_EMAILS_RECEIVED_INDEX,
    CREATE_REPORT_ENQUIRIES_TABLE,
    CREATE_REPORT_ENQUIRIES_RECEIVED_INDEX,
    CREATE_REPORT_ENQUIRIES_STATUS_INDEX,
    CREATE_ENQUIRY_BRANDS_TABLE,
    CREATE_AI_VERDICTS_TABLE,
    CREATE_REPORT_RUNS_TABLE,
]


def is_legacy_bot_schema(conn: sqlite3.Connection) -> bool:
    """True if this file's `enquiries` table is the bot's old shape --
    detected by the absence of the `mailbox` column, which every report
    schema (present or future version) always has. False for a fresh
    file (no `enquiries` table at all) or an already-migrated one.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "enquiries" not in tables:
        return False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(enquiries)")}
    return "mailbox" not in columns


def migrate(conn: sqlite3.Connection) -> None:
    """Create or upgrade this connection's database to the report
    schema (SPEC.md §14.2). Idempotent -- safe to call on every
    report.py startup.

    If the bot's old-shaped `emails`/`enquiries` tables are present,
    they are dropped and recreated in the new shape (SPEC.md §14.1,
    PHASE0_DECISIONS.md Q3: the old rows are all re-derivable from
    Gmail, and a file-level backup is the caller's responsibility --
    see app.database.db.get_report_connection -- before this function
    is ever given a writable connection to a legacy file).
    """
    if is_legacy_bot_schema(conn):
        conn.execute("DROP TABLE IF EXISTS enquiries")
        conn.execute("DROP TABLE IF EXISTS emails")

    for statement in REPORT_SCHEMA_STATEMENTS:
        conn.execute(statement)

    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('report_schema_version', ?)",
        (REPORT_SCHEMA_VERSION,),
    )
    conn.commit()
