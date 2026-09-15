"""Unit tests for app.database.queries / app.database.db / app.database.schema.

Every test uses a temp-file SQLite database (via tmp_path) -- never the
real data/regency.db, and no Gmail/Claude/WhatsApp involved.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.database.db import get_connection
from app.database.queries import (
    close_enquiry,
    create_enquiry,
    email_exists,
    get_email_by_message_id,
    get_enquiry_by_thread,
    insert_email,
    mark_email_processed,
    now_iso,
    update_enquiry_activity,
)
from app.enquiry.models import LastSender, Status


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


# --- schema / connection ---------------------------------------------------


def test_get_connection_creates_tables(db_path):
    with get_connection(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "emails" in tables
    assert "enquiries" in tables


def test_get_connection_is_idempotent_across_calls(db_path):
    # Opening the connection twice (e.g. two sync runs) must not error
    # or wipe existing tables/data.
    with get_connection(db_path) as conn:
        insert_email(
            conn,
            gmail_message_id="m1",
            gmail_thread_id="t1",
            sender="a@example.com",
            recipient="enquiry@regencyelectrical.com",
            subject="Hi",
            body="body",
            received_at=now_iso(),
        )

    with get_connection(db_path) as conn:
        assert email_exists(conn, "m1")


# --- emails -----------------------------------------------------------------


def test_insert_and_fetch_email(db_path):
    with get_connection(db_path) as conn:
        received_at = now_iso()
        email = insert_email(
            conn,
            gmail_message_id="msg-1",
            gmail_thread_id="thread-1",
            sender="ABC Industries <purchase@abc.com>",
            recipient="enquiry@regencyelectrical.com",
            subject="MCCB Requirement",
            body="We need 20 units.",
            received_at=received_at,
        )
        assert email is not None
        assert email.id is not None
        assert email.processed is False

        fetched = get_email_by_message_id(conn, "msg-1")
        assert fetched is not None
        assert fetched.gmail_thread_id == "thread-1"
        assert fetched.subject == "MCCB Requirement"


def test_insert_email_is_idempotent(db_path):
    """Re-running a sync must not create duplicate email rows (project
    spec section 28)."""
    with get_connection(db_path) as conn:
        first = insert_email(
            conn,
            gmail_message_id="dup-1",
            gmail_thread_id="thread-1",
            sender="a@example.com",
            recipient="enquiry@regencyelectrical.com",
            subject="Subject",
            body="Body",
            received_at=now_iso(),
        )
        second = insert_email(
            conn,
            gmail_message_id="dup-1",
            gmail_thread_id="thread-1",
            sender="a@example.com",
            recipient="enquiry@regencyelectrical.com",
            subject="Subject",
            body="Body",
            received_at=now_iso(),
        )

        assert first is not None
        assert second is None  # duplicate, correctly skipped

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE gmail_message_id = 'dup-1'"
        ).fetchone()["n"]
        assert count == 1


def test_mark_email_processed(db_path):
    with get_connection(db_path) as conn:
        insert_email(
            conn,
            gmail_message_id="msg-2",
            gmail_thread_id="thread-2",
            sender="a@example.com",
            recipient="enquiry@regencyelectrical.com",
            subject="s",
            body="b",
            received_at=now_iso(),
        )
        mark_email_processed(conn, "msg-2")
        fetched = get_email_by_message_id(conn, "msg-2")

    assert fetched.processed is True


# --- enquiries ---------------------------------------------------------------


def test_create_and_fetch_enquiry(db_path):
    with get_connection(db_path) as conn:
        received_at = now_iso()
        enquiry = create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="ABC Industries",
            customer_email="purchase@abc.com",
            subject="MCCB Requirement",
            product="Schneider MCCB 250A",
            quantity=20,
            last_sender=LastSender.CUSTOMER,
            received_at=received_at,
        )

        assert enquiry.status == Status.NEW
        assert enquiry.last_activity_at == received_at  # defaults to received_at

        fetched = get_enquiry_by_thread(conn, "thread-1")
        assert fetched is not None
        assert fetched.customer_name == "ABC Industries"
        assert fetched.quantity == 20


def test_get_enquiry_by_thread_returns_none_when_missing(db_path):
    with get_connection(db_path) as conn:
        assert get_enquiry_by_thread(conn, "does-not-exist") is None


def test_create_enquiry_is_idempotent_for_same_thread(db_path):
    """gmail_thread_id is UNIQUE -- creating twice must not raise or
    duplicate; it should return the original row (project spec section
    28)."""
    with get_connection(db_path) as conn:
        first = create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="ABC Industries",
            customer_email="purchase@abc.com",
            subject="MCCB Requirement",
            product="MCCB",
            quantity=20,
            last_sender=LastSender.CUSTOMER,
            received_at=now_iso(),
        )
        second = create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="Someone Else",
            customer_email="other@example.com",
            subject="Different subject",
            product="Different product",
            quantity=1,
            last_sender=LastSender.CUSTOMER,
            received_at=now_iso(),
        )

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM enquiries WHERE gmail_thread_id = 'thread-1'"
        ).fetchone()["n"]

    assert count == 1
    assert second.customer_name == "ABC Industries"  # original row, untouched
    assert second.id == first.id


def test_create_enquiry_rejects_invalid_status(db_path):
    with get_connection(db_path) as conn:
        with pytest.raises(ValueError):
            create_enquiry(
                conn,
                gmail_thread_id="thread-x",
                customer_name=None,
                customer_email=None,
                subject="s",
                product=None,
                quantity=None,
                last_sender=LastSender.CUSTOMER,
                received_at=now_iso(),
                status="MADE_UP_STATUS",
            )


def test_update_enquiry_activity_updates_fields_without_touching_status(db_path):
    with get_connection(db_path) as conn:
        create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="ABC Industries",
            customer_email="purchase@abc.com",
            subject="s",
            product="p",
            quantity=1,
            last_sender=LastSender.CUSTOMER,
            received_at=now_iso(),
        )

        new_activity = now_iso()
        updated = update_enquiry_activity(
            conn, "thread-1", last_sender=LastSender.EMPLOYEE, last_activity_at=new_activity
        )

        assert updated.last_sender == LastSender.EMPLOYEE
        assert updated.last_activity_at == new_activity
        assert updated.status == Status.NEW  # untouched


def test_update_enquiry_activity_can_also_change_status(db_path):
    with get_connection(db_path) as conn:
        create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="ABC",
            customer_email="a@b.com",
            subject="s",
            product="p",
            quantity=1,
            last_sender=LastSender.CUSTOMER,
            received_at=now_iso(),
        )

        updated = update_enquiry_activity(
            conn,
            "thread-1",
            last_sender=LastSender.CUSTOMER,
            last_activity_at=now_iso(),
            status=Status.CUSTOMER_REPLIED,
        )

        assert updated.status == Status.CUSTOMER_REPLIED


def test_update_enquiry_activity_returns_none_for_missing_thread(db_path):
    with get_connection(db_path) as conn:
        result = update_enquiry_activity(
            conn, "no-such-thread", last_sender=LastSender.CUSTOMER, last_activity_at=now_iso()
        )
    assert result is None


def test_update_enquiry_activity_rejects_invalid_last_sender(db_path):
    with get_connection(db_path) as conn:
        create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="ABC",
            customer_email="a@b.com",
            subject="s",
            product="p",
            quantity=1,
            last_sender=LastSender.CUSTOMER,
            received_at=now_iso(),
        )
        with pytest.raises(ValueError):
            update_enquiry_activity(
                conn, "thread-1", last_sender="carrier_pigeon", last_activity_at=now_iso()
            )


def test_close_enquiry_sets_status_and_closed_at(db_path):
    with get_connection(db_path) as conn:
        create_enquiry(
            conn,
            gmail_thread_id="thread-1",
            customer_name="ABC",
            customer_email="a@b.com",
            subject="s",
            product="p",
            quantity=1,
            last_sender=LastSender.CUSTOMER,
            received_at=now_iso(),
        )
        closed = close_enquiry(conn, "thread-1")

    assert closed.status == Status.CLOSED
    assert closed.closed_at is not None


def test_close_enquiry_returns_none_for_missing_thread(db_path):
    with get_connection(db_path) as conn:
        assert close_enquiry(conn, "no-such-thread") is None


def test_rollback_on_exception_leaves_no_partial_writes(db_path):
    with pytest.raises(RuntimeError):
        with get_connection(db_path) as conn:
            insert_email(
                conn,
                gmail_message_id="msg-rollback",
                gmail_thread_id="thread-rollback",
                sender="a@example.com",
                recipient="enquiry@regencyelectrical.com",
                subject="s",
                body="b",
                received_at=now_iso(),
            )
            raise RuntimeError("simulated failure mid-transaction")

    with get_connection(db_path) as conn:
        assert not email_exists(conn, "msg-rollback")


# =============================================================================
# Report path (SPEC.md §14), added Stage 2 -- app.database.db.get_report_
# connection / app.database.schema.migrate(). Everything above is untouched
# and still exercises the bot's own get_connection()/init_db() exclusively.
# =============================================================================

import sqlite3 as _sqlite3
from pathlib import Path

from app.database import schema
from app.database.db import DatabaseError, get_report_connection
from app.database.queries import (
    count_report_emails,
    count_report_threads,
    get_report_email_by_message_id,
    get_report_emails_by_thread,
    get_schema_meta,
    report_email_exists,
    upsert_report_email,
)


def _create_legacy_bot_db(path: str) -> None:
    """Populate `path` with the bot's OLD-shaped emails/enquiries
    tables and a couple of rows -- simulates the real pre-Stage-2
    data/regency.db so migration tests exercise the actual detect +
    backup + rebuild path, not just a fresh file.
    """
    conn = _sqlite3.connect(path)
    for statement in schema.ALL_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO emails (gmail_message_id, gmail_thread_id, sender, recipient, "
        "subject, body, received_at, processed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-msg-1", "legacy-thread-1", "a@example.com", "b@example.com", "s", "b", now_iso(), 1),
    )
    conn.execute(
        "INSERT INTO enquiries (gmail_thread_id, customer_name, customer_email, subject, "
        "product, quantity, status, last_sender, received_at, last_activity_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-thread-1", "ABC", "a@example.com", "s", "p", 1, "NEW", "customer", now_iso(), now_iso()),
    )
    conn.commit()
    conn.close()


def test_get_report_connection_creates_report_schema_on_fresh_file(db_path):
    with get_report_connection(db_path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"schema_meta", "emails", "enquiries", "enquiry_brands", "ai_verdicts", "report_runs"} <= tables


def test_get_report_connection_report_schema_has_mailbox_column(db_path):
    with get_report_connection(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(enquiries)")}
    assert "mailbox" in columns
    assert "closure_kind" in columns


def test_get_report_connection_is_idempotent_across_calls(db_path):
    with get_report_connection(db_path) as conn:
        upsert_report_email(
            conn,
            gmail_message_id="m1",
            gmail_thread_id="t1",
            sender="a@example.com",
            sender_domain="example.com",
            recipient="b@example.com",
            subject="Hi",
            body="body",
            received_at=1000,
            direction="inbound",
            is_auto_reply=False,
            has_attachments=False,
            ingested_at=2000,
        )

    with get_report_connection(db_path) as conn:
        assert report_email_exists(conn, "m1")
        # Re-opening (e.g. a second run) must not wipe existing tables/data.
        assert count_report_emails(conn) == 1


def test_get_report_connection_migrates_legacy_bot_schema_in_place(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    _create_legacy_bot_db(db_path)

    with get_report_connection(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(enquiries)")}
        assert "mailbox" in columns  # rebuilt to the new shape
        # Old rows are gone -- rebuilt, not preserved (PHASE0_DECISIONS.md Q3).
        assert count_report_emails(conn) == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM enquiries").fetchone()["n"] == 0


def test_get_report_connection_backs_up_before_migrating_legacy_schema(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    _create_legacy_bot_db(db_path)

    with get_report_connection(db_path):
        pass

    backups = list(tmp_path.glob("legacy.db.bak.*"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0

    # The backup holds the ORIGINAL old-shaped data, untouched.
    backup_conn = _sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in backup_conn.execute("PRAGMA table_info(enquiries)")}
        assert "mailbox" not in columns
        row = backup_conn.execute("SELECT COUNT(*) FROM emails").fetchone()
        assert row[0] == 1
    finally:
        backup_conn.close()


def test_get_report_connection_does_not_back_up_a_fresh_file(tmp_path):
    db_path = str(tmp_path / "fresh.db")

    with get_report_connection(db_path):
        pass

    assert list(tmp_path.glob("fresh.db.bak.*")) == []


def test_get_report_connection_does_not_re_migrate_already_migrated_file(db_path):
    with get_report_connection(db_path) as conn:
        upsert_report_email(
            conn,
            gmail_message_id="m1",
            gmail_thread_id="t1",
            sender="a@example.com",
            sender_domain="example.com",
            recipient=None,
            subject=None,
            body=None,
            received_at=1000,
            direction="inbound",
            is_auto_reply=False,
            has_attachments=False,
            ingested_at=2000,
        )

    # Re-opening an already-migrated file must not touch existing rows
    # (and, since it is not the legacy bot shape, must never back up).
    with get_report_connection(db_path) as conn:
        assert count_report_emails(conn) == 1

    assert list(Path(db_path).parent.glob(Path(db_path).name + ".bak.*")) == []


def test_migrate_records_schema_version_in_schema_meta(db_path):
    with get_report_connection(db_path) as conn:
        version = get_schema_meta(conn, "report_schema_version")
    assert version == schema.REPORT_SCHEMA_VERSION


def test_migrate_backup_verification_failure_raises_database_error(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy.db")
    _create_legacy_bot_db(db_path)

    # Simulate a backup that comes out empty (e.g. disk full, copy
    # truncated) -- must raise DatabaseError rather than proceed to
    # drop the original tables.
    monkeypatch.setattr(
        "app.database.db.shutil.copy2", lambda src, dst: Path(dst).write_bytes(b"")
    )

    with pytest.raises(DatabaseError, match="empty"):
        with get_report_connection(db_path):
            pass

    # Original legacy file must be untouched -- migration never ran.
    conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(enquiries)")}
        assert "mailbox" not in columns
    finally:
        conn.close()


# --- upsert_report_email idempotency (ON CONFLICT DO NOTHING) ---------------


def test_upsert_report_email_idempotent(db_path):
    with get_report_connection(db_path) as conn:
        first = upsert_report_email(
            conn,
            gmail_message_id="dup-1",
            gmail_thread_id="t1",
            sender="a@example.com",
            sender_domain="example.com",
            recipient="b@example.com",
            subject="s",
            body="b",
            received_at=1000,
            direction="inbound",
            is_auto_reply=False,
            has_attachments=False,
            ingested_at=2000,
        )
        second = upsert_report_email(
            conn,
            gmail_message_id="dup-1",
            gmail_thread_id="t1",
            sender="a@example.com",
            sender_domain="example.com",
            recipient="b@example.com",
            subject="s",
            body="b",
            received_at=1000,
            direction="inbound",
            is_auto_reply=False,
            has_attachments=False,
            ingested_at=9999,  # different value -- must NOT overwrite
        )

    assert first is True  # newly inserted
    assert second is False  # already present, correctly skipped

    with get_report_connection(db_path) as conn:
        assert count_report_emails(conn) == 1
        fetched = get_report_email_by_message_id(conn, "dup-1")
        assert fetched.ingested_at == 2000  # first write wins, never overwritten


def test_upsert_report_email_rejects_invalid_direction(db_path):
    with get_report_connection(db_path) as conn:
        with pytest.raises(ValueError):
            upsert_report_email(
                conn,
                gmail_message_id="m1",
                gmail_thread_id="t1",
                sender="a@example.com",
                sender_domain="example.com",
                recipient=None,
                subject=None,
                body=None,
                received_at=1000,
                direction="sideways",
                is_auto_reply=False,
                has_attachments=False,
                ingested_at=2000,
            )


def test_get_report_emails_by_thread_returns_oldest_first(db_path):
    with get_report_connection(db_path) as conn:
        upsert_report_email(
            conn, gmail_message_id="m2", gmail_thread_id="t1", sender="a@x.com",
            sender_domain="x.com", recipient=None, subject=None, body=None,
            received_at=2000, direction="inbound", is_auto_reply=False,
            has_attachments=False, ingested_at=3000,
        )
        upsert_report_email(
            conn, gmail_message_id="m1", gmail_thread_id="t1", sender="a@x.com",
            sender_domain="x.com", recipient=None, subject=None, body=None,
            received_at=1000, direction="inbound", is_auto_reply=False,
            has_attachments=False, ingested_at=3000,
        )
        messages = get_report_emails_by_thread(conn, "t1")

    assert [m.gmail_message_id for m in messages] == ["m1", "m2"]


def test_count_report_threads_counts_distinct_threads(db_path):
    with get_report_connection(db_path) as conn:
        upsert_report_email(
            conn, gmail_message_id="m1", gmail_thread_id="t1", sender="a@x.com",
            sender_domain="x.com", recipient=None, subject=None, body=None,
            received_at=1000, direction="inbound", is_auto_reply=False,
            has_attachments=False, ingested_at=3000,
        )
        upsert_report_email(
            conn, gmail_message_id="m2", gmail_thread_id="t1", sender="a@x.com",
            sender_domain="x.com", recipient=None, subject=None, body=None,
            received_at=1500, direction="inbound", is_auto_reply=False,
            has_attachments=False, ingested_at=3000,
        )
        upsert_report_email(
            conn, gmail_message_id="m3", gmail_thread_id="t2", sender="a@x.com",
            sender_domain="x.com", recipient=None, subject=None, body=None,
            received_at=1600, direction="inbound", is_auto_reply=False,
            has_attachments=False, ingested_at=3000,
        )
        assert count_report_emails(conn) == 3
        assert count_report_threads(conn) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
