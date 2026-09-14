"""Unit tests for app.gmail.sync -- the Gmail -> SQLite ingestion
pipeline and its branching rule.

Claude (app.ai.claude) is mocked throughout; the database is a real
temp-file SQLite database (via tmp_path), never data/regency.db.
Fixture emails are fake -- no real Gmail/Regency data.
"""

from __future__ import annotations

import pytest

import app.gmail.sync as sync_module
from app.database.db import get_connection
from app.database.queries import get_email_by_message_id, get_enquiry_by_thread
from app.enquiry.models import LastSender, Status
from app.ai.claude import EnquiryExtraction
from app.gmail.client import EmailMessage

COMPANY_DOMAIN = "regencyelectricals.com"


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture(autouse=True)
def company_domain(monkeypatch):
    monkeypatch.setattr(sync_module, "COMPANY_EMAIL_DOMAIN", COMPANY_DOMAIN)


def customer_email(gmail_message_id="msg-1", gmail_thread_id="thread-1", subject="MCCB Requirement", body="We need 20 units."):
    return EmailMessage(
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        sender="ABC Industries <purchase@abc.com>",
        recipient="enquiry@regencyelectricals.com",
        subject=subject,
        received_at="Mon, 1 Sep 2025 09:12:00 +0530",
        body=body,
    )


def employee_email(gmail_message_id="msg-2", gmail_thread_id="thread-1", body="Please find our quote attached."):
    return EmailMessage(
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        sender="Sales <sales@regencyelectricals.com>",
        recipient="purchase@abc.com",
        subject="Re: MCCB Requirement",
        received_at="Mon, 1 Sep 2025 11:00:00 +0530",
        body=body,
    )


# --- new-thread branch -------------------------------------------------


def test_new_enquiry_is_created_with_status_new(db_path, monkeypatch):
    extraction = EnquiryExtraction(
        is_enquiry=True,
        customer_name="ABC Industries",
        customer_email="purchase@abc.com",
        product="Schneider MCCB 250A",
        quantity=20,
    )
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: extraction)

    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, customer_email())

        enquiry = get_enquiry_by_thread(conn, "thread-1")
        email = get_email_by_message_id(conn, "msg-1")

    assert enquiry is not None
    assert enquiry.status == Status.NEW
    assert enquiry.customer_name == "ABC Industries"
    assert enquiry.quantity == 20
    assert enquiry.last_sender == LastSender.CUSTOMER
    assert email.processed is True


def test_non_enquiry_email_is_marked_ignored(db_path, monkeypatch):
    extraction = EnquiryExtraction(
        is_enquiry=False, customer_name=None, customer_email=None, product=None, quantity=None
    )
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: extraction)

    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, customer_email(subject="Newsletter", body="Buy now!"))
        enquiry = get_enquiry_by_thread(conn, "thread-1")

    assert enquiry is not None
    assert enquiry.status == Status.IGNORED


def test_new_thread_from_employee_is_ignored_without_calling_claude(db_path, monkeypatch):
    """Regression test: a genuine new customer enquiry, by definition,
    starts with a message from OUTSIDE the company. A live test run
    found an internal "kindly share pricing" email (sent employee ->
    employee, both @regencyelectricals.com) get a false-positive
    is_enquiry=True from Claude, since sender domain wasn't part of
    the prompt. Now it's short-circuited before Claude is even called.
    """
    claude_calls = []
    monkeypatch.setattr(
        sync_module, "extract_new_enquiry", lambda subject, body: claude_calls.append(1)
    )

    internal_email = employee_email(
        gmail_message_id="internal-1", gmail_thread_id="internal-thread",
        body="Kindly share the pricing for 200 mtrs of 3core cable.",
    )
    internal_email.subject = "Enquiry"

    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, internal_email)
        enquiry = get_enquiry_by_thread(conn, "internal-thread")
        email = get_email_by_message_id(conn, "internal-1")

    assert enquiry is not None
    assert enquiry.status == Status.IGNORED
    assert enquiry.customer_name is None
    assert claude_calls == []  # Claude never called -- deterministic short-circuit
    assert email.processed is True


def test_customer_email_fallback_used_when_claude_omits_it(db_path, monkeypatch):
    extraction = EnquiryExtraction(
        is_enquiry=True, customer_name="ABC Industries", customer_email=None, product="MCCB", quantity=None
    )
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: extraction)

    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, customer_email())
        enquiry = get_enquiry_by_thread(conn, "thread-1")

    assert enquiry.customer_email == "purchase@abc.com"


def test_failed_extraction_leaves_message_unprocessed_for_retry(db_path, monkeypatch):
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: None)

    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, customer_email())

        email = get_email_by_message_id(conn, "msg-1")
        enquiry = get_enquiry_by_thread(conn, "thread-1")

    assert email is not None
    assert email.processed is False
    assert enquiry is None  # no enquiry created on failure


def test_retry_succeeds_without_duplicate_insert(db_path, monkeypatch):
    message = customer_email()

    # First sync: Claude fails.
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: None)
    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, message)

    # Second sync (e.g. next cron run): Claude succeeds this time.
    extraction = EnquiryExtraction(
        is_enquiry=True, customer_name="ABC Industries", customer_email="purchase@abc.com", product="MCCB", quantity=5
    )
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: extraction)
    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, message)

        email_count = conn.execute(
            "SELECT COUNT(*) AS n FROM emails WHERE gmail_message_id = 'msg-1'"
        ).fetchone()["n"]
        enquiry = get_enquiry_by_thread(conn, "thread-1")
        email = get_email_by_message_id(conn, "msg-1")

    assert email_count == 1  # never duplicated
    assert enquiry is not None
    assert enquiry.status == Status.NEW
    assert email.processed is True


def test_duplicate_sync_of_processed_message_is_a_no_op(db_path, monkeypatch):
    extraction = EnquiryExtraction(
        is_enquiry=True, customer_name="ABC Industries", customer_email="purchase@abc.com", product="MCCB", quantity=5
    )
    calls = []
    monkeypatch.setattr(
        sync_module, "extract_new_enquiry", lambda subject, body: (calls.append(1), extraction)[1]
    )

    message = customer_email()
    with get_connection(db_path) as conn:
        sync_module.sync_message(conn, message)
        sync_module.sync_message(conn, message)  # re-run, e.g. cron overlap

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM enquiries WHERE gmail_thread_id = 'thread-1'"
        ).fetchone()["n"]

    assert count == 1
    assert len(calls) == 1  # Claude not called again for an already-processed message


# --- existing-thread branch ---------------------------------------------


def _seed_enquiry(conn):
    """Create an existing NEW enquiry via the new-thread path, using a
    real create_enquiry call so tests below build on realistic state.
    """
    from app.database.queries import create_enquiry, now_iso

    return create_enquiry(
        conn,
        gmail_thread_id="thread-1",
        customer_name="ABC Industries",
        customer_email="purchase@abc.com",
        subject="MCCB Requirement",
        product="MCCB 250A",
        quantity=20,
        last_sender=LastSender.CUSTOMER,
        received_at=now_iso(),
    )


def test_existing_thread_customer_accepted_closes_enquiry(db_path, monkeypatch):
    monkeypatch.setattr(sync_module, "classify_reply_intent", lambda body: "ACCEPTED")

    with get_connection(db_path) as conn:
        _seed_enquiry(conn)
        sync_module.sync_message(conn, customer_email(gmail_message_id="msg-2", body="Yes, please proceed."))
        enquiry = get_enquiry_by_thread(conn, "thread-1")

    assert enquiry.status == Status.CLOSED
    assert enquiry.closed_at is not None
    assert enquiry.last_sender == LastSender.CUSTOMER


def test_existing_thread_employee_quotation_sets_quotation_sent(db_path, monkeypatch):
    monkeypatch.setattr(sync_module, "classify_reply_intent", lambda body: "GENERAL_REPLY")

    with get_connection(db_path) as conn:
        _seed_enquiry(conn)
        sync_module.sync_message(conn, employee_email())
        enquiry = get_enquiry_by_thread(conn, "thread-1")

    assert enquiry.status == Status.QUOTATION_SENT
    assert enquiry.last_sender == LastSender.EMPLOYEE
    assert enquiry.closed_at is None


def test_existing_thread_failed_intent_classification_is_not_marked_processed(db_path, monkeypatch):
    monkeypatch.setattr(sync_module, "classify_reply_intent", lambda body: None)

    with get_connection(db_path) as conn:
        _seed_enquiry(conn)
        sync_module.sync_message(conn, customer_email(gmail_message_id="msg-2", body="Some reply"))

        enquiry = get_enquiry_by_thread(conn, "thread-1")
        email = get_email_by_message_id(conn, "msg-2")

    assert enquiry.status == Status.NEW  # unchanged
    assert email.processed is False


def test_new_message_on_existing_thread_does_not_duplicate_enquiry(db_path, monkeypatch):
    monkeypatch.setattr(sync_module, "classify_reply_intent", lambda body: "NEEDS_INFORMATION")

    with get_connection(db_path) as conn:
        _seed_enquiry(conn)
        sync_module.sync_message(conn, customer_email(gmail_message_id="msg-2", body="What's the lead time?"))

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM enquiries WHERE gmail_thread_id = 'thread-1'"
        ).fetchone()["n"]

    assert count == 1


# --- run_sync ----------------------------------------------------------


def test_run_sync_processes_every_fetched_message(db_path, monkeypatch):
    messages = [customer_email(gmail_message_id="m1", gmail_thread_id="t1"), customer_email(gmail_message_id="m2", gmail_thread_id="t2")]
    monkeypatch.setattr(sync_module, "fetch_messages", lambda service, max_results=25: messages)

    processed = []
    monkeypatch.setattr(sync_module, "sync_message", lambda conn, message: processed.append(message.gmail_message_id))

    with get_connection(db_path) as conn:
        count = sync_module.run_sync(conn, service=object(), max_results=25)

    assert count == 2
    assert processed == ["m1", "m2"]


def test_run_sync_processes_batch_oldest_first_even_if_gmail_returns_newest_first(db_path, monkeypatch):
    """Regression test: Gmail's messages.list returns newest-first. If
    a thread's reply were processed before its original message, the
    original would be misclassified as a "reply" on an "existing"
    thread instead of triggering new-enquiry extraction -- silently
    losing the real enquiry (this happened in a live run before the
    _sort_key fix in app.gmail.sync.run_sync).
    """
    original = customer_email(
        gmail_message_id="original",
        gmail_thread_id="thread-1",
        subject="MCCB Requirement",
        body="We need 20 units of MCCB 250A.",
    )
    original.received_at = "Mon, 1 Sep 2025 09:00:00 +0000"

    reply = customer_email(
        gmail_message_id="reply",
        gmail_thread_id="thread-1",
        subject="Re: MCCB Requirement",
        body="Any update on this?",
    )
    reply.received_at = "Mon, 1 Sep 2025 10:00:00 +0000"

    # Gmail returns newest-first -- reply before original.
    monkeypatch.setattr(sync_module, "fetch_messages", lambda service, max_results=25: [reply, original])

    extraction = EnquiryExtraction(
        is_enquiry=True, customer_name="ABC Industries", customer_email="purchase@abc.com", product="MCCB 250A", quantity=20
    )
    monkeypatch.setattr(sync_module, "extract_new_enquiry", lambda subject, body: extraction)
    monkeypatch.setattr(sync_module, "classify_reply_intent", lambda body: "FOLLOW_UP")

    with get_connection(db_path) as conn:
        sync_module.run_sync(conn, service=object(), max_results=25)
        enquiry = get_enquiry_by_thread(conn, "thread-1")

    # The ORIGINAL message must have triggered extraction (status NEW,
    # real customer/product data) -- not the reply being mistaken for
    # the thread's opening message.
    assert enquiry.status != Status.IGNORED
    assert enquiry.customer_name == "ABC Industries"
    assert enquiry.product == "MCCB 250A"
    assert enquiry.received_at < enquiry.last_activity_at


def test_run_sync_skips_one_bad_message_without_aborting(db_path, monkeypatch):
    messages = [customer_email(gmail_message_id="m1", gmail_thread_id="t1"), customer_email(gmail_message_id="m2", gmail_thread_id="t2")]
    monkeypatch.setattr(sync_module, "fetch_messages", lambda service, max_results=25: messages)

    def flaky_sync(conn, message):
        if message.gmail_message_id == "m1":
            raise RuntimeError("simulated bug")

    monkeypatch.setattr(sync_module, "sync_message", flaky_sync)

    with get_connection(db_path) as conn:
        count = sync_module.run_sync(conn, service=object(), max_results=25)

    assert count == 2  # both messages counted as fetched, one just failed processing


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
