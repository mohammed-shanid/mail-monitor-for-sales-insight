"""Unit tests for the Phase 5 founder-query functions in
app.database.queries -- count_today, count_this_month, count_open,
count_closed, get_unanswered, get_today_enquiries, get_open_enquiries,
get_today_summary, get_enquiry_by_customer.

Every test uses a temp-file SQLite database, never data/regency.db.
"""

from __future__ import annotations

import pytest

from app.database.db import get_connection
from app.database.queries import (
    count_closed,
    count_open,
    count_this_month,
    count_today,
    create_enquiry,
    get_emails_by_thread,
    get_enquiry_by_customer,
    get_open_enquiries,
    get_today_enquiries,
    get_today_summary,
    get_unanswered,
    insert_email,
    now_iso,
    update_enquiry_activity,
)
from app.enquiry.models import LastSender, Status

OLD_DATE = "2020-01-01T00:00:00+00:00"  # clearly not today, not this month


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _seed(conn, **overrides):
    defaults = dict(
        gmail_thread_id="thread-1",
        customer_name="ABC Industries",
        customer_email="purchase@abc.com",
        subject="MCCB Requirement",
        product="MCCB 250A",
        quantity=20,
        last_sender=LastSender.CUSTOMER,
        received_at=now_iso(),
    )
    defaults.update(overrides)
    return create_enquiry(conn, **defaults)


# --- count_today / count_this_month --------------------------------------


def test_count_today_only_counts_todays_enquiries(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="today-1")
        _seed(conn, gmail_thread_id="old-1", received_at=OLD_DATE)

        assert count_today(conn) == 1


def test_count_today_excludes_ignored(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="real", status=Status.NEW)
        _seed(conn, gmail_thread_id="spam", status=Status.IGNORED)

        assert count_today(conn) == 1


def test_count_this_month_excludes_older_months(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="this-month")
        _seed(conn, gmail_thread_id="old", received_at=OLD_DATE)

        assert count_this_month(conn) == 1


# --- count_open / count_closed --------------------------------------------


def test_count_open_and_count_closed_partition_correctly(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="new", status=Status.NEW)
        _seed(conn, gmail_thread_id="replied", status=Status.CUSTOMER_REPLIED)
        _seed(conn, gmail_thread_id="closed", status=Status.CLOSED)
        _seed(conn, gmail_thread_id="ignored", status=Status.IGNORED)

        assert count_open(conn) == 2
        assert count_closed(conn) == 1


# --- get_unanswered ---------------------------------------------------------


def test_get_unanswered_only_includes_open_threads_waiting_on_customer(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="waiting", customer_name="Waiting Co", last_sender=LastSender.CUSTOMER)

        replied = _seed(conn, gmail_thread_id="replied", customer_name="Replied Co", last_sender=LastSender.CUSTOMER)
        update_enquiry_activity(conn, replied.gmail_thread_id, last_sender=LastSender.EMPLOYEE, last_activity_at=now_iso())

        closed = _seed(conn, gmail_thread_id="closed", customer_name="Closed Co", last_sender=LastSender.CUSTOMER)
        update_enquiry_activity(
            conn, closed.gmail_thread_id, last_sender=LastSender.CUSTOMER, last_activity_at=now_iso(), status=Status.CLOSED
        )

        names = [e.customer_name for e in get_unanswered(conn)]

    assert names == ["Waiting Co"]


def test_get_unanswered_orders_oldest_waiting_first(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="newer", customer_name="Newer Co", received_at="2026-01-02T00:00:00+00:00")
        _seed(conn, gmail_thread_id="older", customer_name="Older Co", received_at="2026-01-01T00:00:00+00:00")

        names = [e.customer_name for e in get_unanswered(conn)]

    assert names == ["Older Co", "Newer Co"]


# --- get_today_enquiries / get_open_enquiries -----------------------------


def test_get_today_enquiries_excludes_old_and_ignored(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="today", customer_name="Today Co")
        _seed(conn, gmail_thread_id="old", customer_name="Old Co", received_at=OLD_DATE)
        _seed(conn, gmail_thread_id="ignored", customer_name="Ignored Co", status=Status.IGNORED)

        names = [e.customer_name for e in get_today_enquiries(conn)]

    assert names == ["Today Co"]


def test_get_open_enquiries_excludes_closed_and_ignored(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="open", customer_name="Open Co", status=Status.NEW)
        _seed(conn, gmail_thread_id="closed", customer_name="Closed Co", status=Status.CLOSED)
        _seed(conn, gmail_thread_id="ignored", customer_name="Ignored Co", status=Status.IGNORED)

        names = [e.customer_name for e in get_open_enquiries(conn)]

    assert names == ["Open Co"]


# --- get_today_summary -----------------------------------------------------


def test_get_today_summary_counts_breakdown_of_todays_enquiries(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="waiting", last_sender=LastSender.CUSTOMER, status=Status.NEW)

        replied = _seed(conn, gmail_thread_id="replied", last_sender=LastSender.CUSTOMER, status=Status.NEW)
        update_enquiry_activity(conn, replied.gmail_thread_id, last_sender=LastSender.EMPLOYEE, last_activity_at=now_iso())

        _seed(conn, gmail_thread_id="closed", status=Status.CLOSED)
        _seed(conn, gmail_thread_id="ignored", status=Status.IGNORED)
        _seed(conn, gmail_thread_id="old", received_at=OLD_DATE)  # not today -- excluded entirely

        summary = get_today_summary(conn)

    assert summary == {"received": 3, "closed": 1, "open": 2, "unanswered": 1}


def test_get_today_summary_is_all_zero_when_nothing_today(db_path):
    with get_connection(db_path) as conn:
        summary = get_today_summary(conn)

    assert summary == {"received": 0, "closed": 0, "open": 0, "unanswered": 0}


# --- get_enquiry_by_customer -----------------------------------------------


def test_get_enquiry_by_customer_case_insensitive_partial_match(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, customer_name="ABC Industries")

        results = get_enquiry_by_customer(conn, "abc")

    assert len(results) == 1
    assert results[0].customer_name == "ABC Industries"


def test_get_enquiry_by_customer_no_match_returns_empty_list(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, customer_name="ABC Industries")

        assert get_enquiry_by_customer(conn, "XYZ") == []


def test_get_enquiry_by_customer_escapes_like_wildcards(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="t1", customer_name="ABC Industries")
        _seed(conn, gmail_thread_id="t2", customer_name="A_C Traders")

        # A literal "%" query must not act as a wildcard matching everyone.
        assert get_enquiry_by_customer(conn, "%") == []
        # A literal "_" must only match a literal underscore, not "any char".
        results = get_enquiry_by_customer(conn, "A_C")

    assert [e.customer_name for e in results] == ["A_C Traders"]


def test_get_enquiry_by_customer_orders_most_recent_first(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="t1", customer_name="ABC Industries", received_at="2026-01-01T00:00:00+00:00")
        _seed(conn, gmail_thread_id="t2", customer_name="ABC Traders", received_at="2026-01-02T00:00:00+00:00")

        names = [e.customer_name for e in get_enquiry_by_customer(conn, "ABC")]

    assert names == ["ABC Traders", "ABC Industries"]


# --- get_emails_by_thread ---------------------------------------------------


def test_get_emails_by_thread_returns_oldest_first(db_path):
    with get_connection(db_path) as conn:
        insert_email(
            conn, gmail_message_id="m2", gmail_thread_id="t1", sender="sales@regencyelectricals.com",
            recipient="purchase@abc.com", subject="Re: MCCB", body="Quote attached.",
            received_at="2026-01-02T00:00:00+00:00",
        )
        insert_email(
            conn, gmail_message_id="m1", gmail_thread_id="t1", sender="purchase@abc.com",
            recipient="enquiry@regencyelectricals.com", subject="MCCB", body="We need 20 units.",
            received_at="2026-01-01T00:00:00+00:00",
        )

        emails = get_emails_by_thread(conn, "t1")

    assert [e.gmail_message_id for e in emails] == ["m1", "m2"]


def test_get_emails_by_thread_returns_empty_list_for_unknown_thread(db_path):
    with get_connection(db_path) as conn:
        assert get_emails_by_thread(conn, "no-such-thread") == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
