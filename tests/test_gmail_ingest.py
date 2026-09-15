"""Unit tests for app.gmail.ingest -- the report path's fetch/hydrate/
upsert orchestration. Gmail is mocked throughout (MagicMock `service`);
the DB is a real temp-file report connection (app.database.db.
get_report_connection) so idempotency is genuinely exercised.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.database.db import get_report_connection
from app.database.queries import count_report_emails, get_report_emails_by_thread
from app.gmail.client import GmailFetchError
from app.gmail.ingest import IngestAbortedError, ingest_window

IST = ZoneInfo("Asia/Kolkata")

MAILBOX = "u.ruma@regencyelectricals.com"
WINDOW_START = 1757443200000  # some instant
WINDOW_END = WINDOW_START + 86_400_000 - 1


def _msg(msg_id, thread_id, *, sender, internal_date_ms, subject="s", body_data=None):
    payload = {"headers": [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]}
    if body_data is not None:
        payload["mimeType"] = "text/plain"
        payload["body"] = {"data": body_data}
    return {"id": msg_id, "threadId": thread_id, "internalDate": str(internal_date_ms), "payload": payload}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _make_service(list_pages, get_by_id, threads_by_id):
    """A MagicMock Gmail service whose messages().list()/get() and
    threads().get() are driven by plain dicts, mirroring the real API
    shape closely enough for ingest_window()'s calls.
    """
    service = MagicMock()

    list_execute = service.users.return_value.messages.return_value.list.return_value.execute
    list_execute.side_effect = list_pages

    def get_message(userId, id, format):  # noqa: A002
        return MagicMock(execute=MagicMock(return_value=get_by_id[id]))

    service.users.return_value.messages.return_value.get.side_effect = get_message

    def get_thread(userId, id, format):  # noqa: A002
        return MagicMock(execute=MagicMock(return_value=threads_by_id[id]))

    service.users.return_value.threads.return_value.get.side_effect = get_thread

    return service


def test_ingest_window_stores_touched_thread_messages(db_path):
    # One thread, one message inside the window, from a customer.
    msg = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 1000)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],
        get_by_id={"m1": msg},
        threads_by_id={"t1": {"id": "t1", "messages": [msg]}},
    )

    with get_report_connection(db_path) as conn:
        result = ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
        )

    assert result.messages_listed == 1
    assert result.threads_touched == 1
    assert result.messages_hydrated == 1
    assert result.messages_new == 1

    with get_report_connection(db_path) as conn:
        assert count_report_emails(conn) == 1


def test_ingest_window_hydration_pulls_pre_window_messages(db_path):
    # Thread has 2 messages: one before the window (not touched by the
    # listing) and one inside it. Hydration must still store both.
    old_msg = _msg("m0", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START - 100_000)
    new_msg = _msg("m1", "t1", sender="sales@regencyelectricals.com", internal_date_ms=WINDOW_START + 1000)

    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],  # only the in-window message is "listed"
        get_by_id={"m1": new_msg},
        threads_by_id={"t1": {"id": "t1", "messages": [old_msg, new_msg]}},
    )

    with get_report_connection(db_path) as conn:
        result = ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
        )
        thread_messages = get_report_emails_by_thread(conn, "t1")

    assert result.messages_hydrated == 2  # both old and new, via hydration
    assert {m.gmail_message_id for m in thread_messages} == {"m0", "m1"}


def test_ingest_window_direction_employee_is_outbound(db_path):
    msg = _msg("m1", "t1", sender="u.ruma@regencyelectricals.com", internal_date_ms=WINDOW_START + 1000)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],
        get_by_id={"m1": msg},
        threads_by_id={"t1": {"id": "t1", "messages": [msg]}},
    )

    with get_report_connection(db_path) as conn:
        ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
        )
        stored = get_report_emails_by_thread(conn, "t1")[0]

    assert stored.direction == "outbound"


def test_ingest_window_direction_customer_is_inbound(db_path):
    msg = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 1000)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],
        get_by_id={"m1": msg},
        threads_by_id={"t1": {"id": "t1", "messages": [msg]}},
    )

    with get_report_connection(db_path) as conn:
        ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
        )
        stored = get_report_emails_by_thread(conn, "t1")[0]

    assert stored.direction == "inbound"


def test_ingest_window_body_is_quote_stripped_before_storage(db_path):
    import base64

    body_text = (
        "We need 20 more units.\n\n"
        "On Mon, 1 Sep 2025, ABC wrote:\n> old quoted content"
    )
    data = base64.urlsafe_b64encode(body_text.encode()).decode()
    msg = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 1000, body_data=data)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],
        get_by_id={"m1": msg},
        threads_by_id={"t1": {"id": "t1", "messages": [msg]}},
    )

    with get_report_connection(db_path) as conn:
        ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
        )
        stored = get_report_emails_by_thread(conn, "t1")[0]

    assert "We need 20 more units." in stored.body
    assert "old quoted content" not in stored.body


def test_ingest_window_is_idempotent_across_two_runs(db_path):
    msg = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 1000)

    def run():
        service = _make_service(
            list_pages=[{"messages": [{"id": "m1"}]}],
            get_by_id={"m1": msg},
            threads_by_id={"t1": {"id": "t1", "messages": [msg]}},
        )
        with get_report_connection(db_path) as conn:
            return ingest_window(
                conn, service,
                window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
            )

    first = run()
    second = run()

    assert first.messages_new == 1
    assert second.messages_new == 0  # idempotent -- no duplicate on re-run

    with get_report_connection(db_path) as conn:
        assert count_report_emails(conn) == 1  # zero duplicate rows


def test_ingest_window_excludes_messages_outside_window_from_touched_threads(db_path):
    # Listed (by the coarse Gmail query) but its internalDate actually
    # falls outside the exact window -- must not count as touched.
    msg = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_END + 1_000_000)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],
        get_by_id={"m1": msg},
        threads_by_id={},
    )

    with get_report_connection(db_path) as conn:
        result = ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
        )

    assert result.threads_touched == 0
    # The listed-but-out-of-window message itself is still stored (it
    # was fetched and parsed -- only thread *hydration* is skipped).
    assert result.messages_hydrated == 1


def test_ingest_window_aborts_when_exceeding_max_messages(db_path):
    msg = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 1000)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}]}],
        get_by_id={"m1": msg},
        threads_by_id={"t1": {"id": "t1", "messages": [msg]}},
    )

    with get_report_connection(db_path) as conn:
        with pytest.raises(IngestAbortedError, match="GMAIL_MAX_MESSAGES"):
            ingest_window(
                conn, service,
                window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                max_messages=0, ingested_at_ms=WINDOW_START + 5000,
            )

    with get_report_connection(db_path) as conn:
        # Aborted before any write -- nothing partially stored.
        assert count_report_emails(conn) == 0


def test_ingest_window_propagates_gmail_fetch_error(db_path):
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = GmailFetchError("boom")

    with get_report_connection(db_path) as conn:
        with pytest.raises(GmailFetchError):
            ingest_window(
                conn, service,
                window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                max_messages=2000, ingested_at_ms=WINDOW_START + 5000,
            )


def test_ingest_window_respects_limit_dev_aid(db_path):
    m1 = _msg("m1", "t1", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 1000)
    m2 = _msg("m2", "t2", sender="purchase@abc.com", internal_date_ms=WINDOW_START + 2000)
    service = _make_service(
        list_pages=[{"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}],
        get_by_id={"m1": m1, "m2": m2},
        threads_by_id={"t1": {"id": "t1", "messages": [m1]}, "t2": {"id": "t2", "messages": [m2]}},
    )

    with get_report_connection(db_path) as conn:
        result = ingest_window(
            conn, service,
            window_start_ms=WINDOW_START, window_end_ms=WINDOW_END, tz=IST,
            mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
            max_messages=2000, ingested_at_ms=WINDOW_START + 5000, limit=2,
        )

    # The listing was capped to 2 ids (m1, m2) -- m3 was never fetched,
    # confirmed by messages().get() only ever being called for ids
    # present in get_by_id (a KeyError on "m3" would fail this test).
    assert result.messages_listed == 2
