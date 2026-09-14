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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
