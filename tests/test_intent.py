"""Unit tests for app.router.intent -- phrase matching and the full
deterministic founder-question pipeline.
"""

from __future__ import annotations

import pytest

import app.router.intent as intent_module
from app.database.db import get_connection
from app.database.queries import create_enquiry, now_iso, update_enquiry_activity
from app.enquiry.models import LastSender, Status
from app.router.intent import Intent, handle_founder_question, match_intent, route_message


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


# --- match_intent: exact-ish phrasing from the project spec ---------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("How many enquiries today?", Intent.COUNT_TODAY),
        ("enquiries today", Intent.COUNT_TODAY),
        ("How many came today", Intent.COUNT_TODAY),
        ("How many this month?", Intent.COUNT_MONTH),
        ("monthly enquiries", Intent.COUNT_MONTH),
        ("How many are open?", Intent.COUNT_OPEN),
        ("what's pending", Intent.COUNT_OPEN),
        ("How many are closed?", Intent.COUNT_CLOSED),
        ("how many completed", Intent.COUNT_CLOSED),
        ("Show pending enquiries", Intent.GET_OPEN_DETAILS),
        ("show open enquiries", Intent.GET_OPEN_DETAILS),
        ("list pending enquiries", Intent.GET_OPEN_DETAILS),
        ("Show today's enquiries", Intent.GET_TODAY),
        ("list today's enquiries", Intent.GET_TODAY),
        ("who hasn't replied", Intent.GET_UNANSWERED),
        ("which customers are waiting", Intent.GET_UNANSWERED),
    ],
)
def test_match_intent_recognizes_spec_phrases(text, expected):
    assert match_intent(text) == expected


# --- tolerance: casing, whitespace, punctuation, apostrophes -------------


@pytest.mark.parametrize(
    "text",
    [
        "HOW MANY ENQUIRIES TODAY?",
        "  how   many   enquiries   today  ",
        "how many enquiries today!!",
        "how many enquiries todays",  # missing/extra apostrophe variant
    ],
)
def test_match_intent_is_tolerant_to_phrasing_variation(text):
    assert match_intent(text) == Intent.COUNT_TODAY


def test_match_intent_returns_none_for_open_ended_question():
    assert match_intent("What happened with ABC Industries?") is None
    assert match_intent("Show ABC Industries") is None


def test_match_intent_returns_none_for_empty_or_blank():
    assert match_intent("") is None
    assert match_intent("   ") is None


def test_get_open_details_is_not_shadowed_by_count_open():
    # "show open enquiries" contains "open enquiries" (a COUNT_OPEN
    # alias) as a substring -- GET_OPEN_DETAILS must win.
    assert match_intent("show open enquiries") == Intent.GET_OPEN_DETAILS


# --- handle_founder_question: full pipeline against a real temp DB -------


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


def test_handle_founder_question_returns_none_for_unmatched_text(db_path):
    with get_connection(db_path) as conn:
        result = handle_founder_question(conn, "What happened with ABC Industries?")
    assert result is None


def test_handle_founder_question_count_today(db_path):
    with get_connection(db_path) as conn:
        _seed(conn)
        result = handle_founder_question(conn, "How many enquiries today?")

    assert "Today's Enquiries" in result
    assert "Received: 1" in result


def test_handle_founder_question_count_open(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, gmail_thread_id="t1")
        _seed(conn, gmail_thread_id="t2")
        result = handle_founder_question(conn, "How many are open?")

    assert result == "Open enquiries: 2"


def test_handle_founder_question_count_closed(db_path):
    with get_connection(db_path) as conn:
        enquiry = _seed(conn)
        update_enquiry_activity(
            conn, enquiry.gmail_thread_id, last_sender=LastSender.CUSTOMER,
            last_activity_at=now_iso(), status=Status.CLOSED,
        )
        result = handle_founder_question(conn, "How many are closed?")

    assert result == "Closed enquiries: 1"


def test_handle_founder_question_get_open_details_lists_customer(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, customer_name="ABC Industries", product="MCCB 250A")
        result = handle_founder_question(conn, "Show pending enquiries")

    assert "Pending Enquiries" in result
    assert "ABC Industries" in result
    assert "MCCB 250A" in result


def test_handle_founder_question_get_unanswered_only_includes_customer_waiting(db_path):
    with get_connection(db_path) as conn:
        waiting = _seed(conn, gmail_thread_id="waiting", customer_name="Waiting Co")
        replied = _seed(conn, gmail_thread_id="replied", customer_name="Replied Co")
        update_enquiry_activity(
            conn, replied.gmail_thread_id, last_sender=LastSender.EMPLOYEE, last_activity_at=now_iso()
        )

        result = handle_founder_question(conn, "which customers are waiting")

    assert "Waiting Co" in result
    assert "Replied Co" not in result


def test_handle_founder_question_get_today_details(db_path):
    with get_connection(db_path) as conn:
        _seed(conn, customer_name="ABC Industries")
        result = handle_founder_question(conn, "Show today's enquiries")

    assert "Today's Enquiries" in result
    assert "ABC Industries" in result


def test_handle_founder_question_count_month(db_path):
    with get_connection(db_path) as conn:
        _seed(conn)
        result = handle_founder_question(conn, "How many enquiries this month?")

    assert result == "This month's enquiries: 1"


# --- route_message: full pipeline including open-ended fallback ---------


def test_route_message_uses_fixed_intent_when_it_matches(db_path):
    with get_connection(db_path) as conn:
        _seed(conn)
        result = route_message(conn, "How many are open?")

    assert result == "Open enquiries: 1"


def test_route_message_falls_back_to_open_ended_for_known_customer(db_path, monkeypatch):
    monkeypatch.setattr(intent_module, "answer_open_ended_question", lambda conn, text: "a Claude summary")

    with get_connection(db_path) as conn:
        _seed(conn)
        result = route_message(conn, "What happened with ABC Industries?")

    assert result == "a Claude summary"


def test_route_message_returns_generic_fallback_when_nothing_matches(db_path, monkeypatch):
    monkeypatch.setattr(intent_module, "answer_open_ended_question", lambda conn, text: None)

    with get_connection(db_path) as conn:
        result = route_message(conn, "asdkjfh random gibberish")

    assert "didn't understand" in result.lower()
    assert "How many enquiries today?" in result  # includes example questions


def test_route_message_never_returns_none(db_path, monkeypatch):
    monkeypatch.setattr(intent_module, "answer_open_ended_question", lambda conn, text: None)

    with get_connection(db_path) as conn:
        for text in ["", "   ", "gibberish", "How many are open?"]:
            assert isinstance(route_message(conn, text), str)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
