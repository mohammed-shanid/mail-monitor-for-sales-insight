"""Unit tests for app.router.open_ended. Claude (app.ai.claude) is
mocked throughout; the database is a real temp-file SQLite database.
"""

from __future__ import annotations

import pytest

import app.router.open_ended as open_ended_module
from app.database.db import get_connection
from app.database.queries import create_enquiry, insert_email, now_iso
from app.enquiry.models import LastSender
from app.router.open_ended import answer_open_ended_question, find_relevant_enquiry


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _seed_enquiry_with_email(conn, *, customer_name="ABC Industries", thread_id="thread-1"):
    received_at = now_iso()
    create_enquiry(
        conn,
        gmail_thread_id=thread_id,
        customer_name=customer_name,
        customer_email="purchase@abc.com",
        subject="MCCB Requirement",
        product="MCCB 250A",
        quantity=20,
        last_sender=LastSender.CUSTOMER,
        received_at=received_at,
    )
    insert_email(
        conn,
        gmail_message_id=f"{thread_id}-msg1",
        gmail_thread_id=thread_id,
        sender="ABC Industries <purchase@abc.com>",
        recipient="enquiry@regencyelectricals.com",
        subject="MCCB Requirement",
        body="We need 20 units of MCCB 250A.",
        received_at=received_at,
    )


# --- find_relevant_enquiry -------------------------------------------------


def test_find_relevant_enquiry_strips_lead_in_phrase(db_path):
    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        result = find_relevant_enquiry(conn, "What happened with ABC Industries?")

    assert result is not None
    assert result.customer_name == "ABC Industries"


def test_find_relevant_enquiry_matches_bare_customer_name(db_path):
    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        result = find_relevant_enquiry(conn, "ABC Industries")

    assert result is not None


def test_find_relevant_enquiry_returns_none_for_unknown_customer(db_path):
    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        result = find_relevant_enquiry(conn, "What happened with Nonexistent Corp?")

    assert result is None


# --- answer_open_ended_question --------------------------------------------


def test_answer_open_ended_question_returns_none_when_no_enquiry_found(db_path):
    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        result = answer_open_ended_question(conn, "What happened with Nonexistent Corp?")

    assert result is None


def test_answer_open_ended_question_returns_claude_summary(db_path, monkeypatch):
    monkeypatch.setattr(
        open_ended_module,
        "summarize_enquiry",
        lambda **kwargs: "ABC Industries wants 20 MCCB 250A units; awaiting a quotation.",
    )

    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        result = answer_open_ended_question(conn, "What happened with ABC Industries?")

    assert result == "ABC Industries wants 20 MCCB 250A units; awaiting a quotation."


def test_answer_open_ended_question_passes_real_thread_history_to_claude(db_path, monkeypatch):
    captured = {}

    def fake_summarize(**kwargs):
        captured.update(kwargs)
        return "summary"

    monkeypatch.setattr(open_ended_module, "summarize_enquiry", fake_summarize)

    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        answer_open_ended_question(conn, "ABC Industries")

    assert captured["customer_name"] == "ABC Industries"
    assert captured["product"] == "MCCB 250A"
    assert captured["quantity"] == 20
    assert len(captured["thread_messages"]) == 1
    assert "20 units of MCCB 250A" in captured["thread_messages"][0]["body"]


def test_answer_open_ended_question_gives_specific_message_when_claude_fails(db_path, monkeypatch):
    monkeypatch.setattr(open_ended_module, "summarize_enquiry", lambda **kwargs: None)

    with get_connection(db_path) as conn:
        _seed_enquiry_with_email(conn)
        result = answer_open_ended_question(conn, "ABC Industries")

    assert result is not None
    assert "couldn't summarize" in result.lower()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
