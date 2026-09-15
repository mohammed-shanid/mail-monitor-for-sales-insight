"""Unit tests for app.ai.analyze -- cache/Claude/thread_state
orchestration. Claude is mocked (patch app.ai.claude._client); the DB
is a real temp-file report connection so cache hits/misses are
genuinely exercised. SPEC.md §20.2 cases 38-40.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.ai.analyze import analyze_window
from app.ai.prompts import MESSAGE_VERDICT_PROMPT_VERSION
from app.database.db import get_report_connection
from app.database.queries import get_ai_verdict_row, upsert_report_email
from app.enquiry.models import ReportStatus

MAILBOX = "u.ruma@regencyelectricals.com"
T0 = 1_000_000
HOUR = 3_600_000


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _verdict_payload(**overrides):
    payload = {
        "is_enquiry": True, "counterparty_type": "customer", "customer_name": "ABC Industries",
        "company": "ABC Industries", "product": "MCCB 250A", "requirement": "20 units",
        "quantity": "20 nos", "brands": ["Schneider"], "quotation_signal": "rfq_received",
        "closure_signal": False, "closure_evidence": None, "closure_kind": None,
        "urgency": False, "urgency_evidence": None, "confidence": "high",
    }
    payload.update(overrides)
    return payload


def _fake_response(payload):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def _seed_message(conn, msg_id, thread_id, sender, received_at, body="We need 20 units."):
    upsert_report_email(
        conn, gmail_message_id=msg_id, gmail_thread_id=thread_id, sender=sender,
        sender_domain=sender.split("@")[-1].rstrip(">") if "@" in sender else None,
        recipient=MAILBOX, subject="MCCB Requirement", body=body, received_at=received_at,
        direction="outbound" if sender == MAILBOX else "inbound",
        is_auto_reply=False, has_attachments=False, ingested_at=received_at,
    )


def test_analyze_window_calls_claude_and_caches_verdict(db_path):
    client = MagicMock()
    client.messages.create.return_value = _fake_response(_verdict_payload())

    with get_report_connection(db_path) as conn:
        _seed_message(conn, "m1", "t1", "purchase@abc.com", T0)

        with patch("app.ai.claude._client", return_value=client):
            result = analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="claude-sonnet-4-6", max_retries=1, reprocess=False, now_ms=T0 + HOUR,
            )

        # Verdict was cached in the DB.
        cached_row = get_ai_verdict_row(conn, "m1", MESSAGE_VERDICT_PROMPT_VERSION)

    assert result.cached_count == 0
    assert result.analyzed_count == 1
    assert "t1" in result.states
    assert result.states["t1"].status == ReportStatus.PENDING
    assert cached_row is not None
    assert client.messages.create.call_count == 1


def test_analyze_window_second_run_uses_cache_not_claude(db_path):
    client = MagicMock()
    client.messages.create.return_value = _fake_response(_verdict_payload())

    with get_report_connection(db_path) as conn:
        _seed_message(conn, "m1", "t1", "purchase@abc.com", T0)

        with patch("app.ai.claude._client", return_value=client):
            analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=False, now_ms=T0 + HOUR,
            )

        # Second run: cache should serve it -- ZERO additional API calls.
        with patch("app.ai.claude._client", return_value=client):
            result = analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=False, now_ms=T0 + 2 * HOUR,
            )

    assert result.cached_count == 1
    assert result.analyzed_count == 0
    assert client.messages.create.call_count == 1  # still just the first run's call


def test_reprocess_bypasses_cache(db_path):
    client = MagicMock()
    client.messages.create.return_value = _fake_response(_verdict_payload())

    with get_report_connection(db_path) as conn:
        _seed_message(conn, "m1", "t1", "purchase@abc.com", T0)

        with patch("app.ai.claude._client", return_value=client):
            analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=False, now_ms=T0 + HOUR,
            )

        with patch("app.ai.claude._client", return_value=client):
            result = analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=True, now_ms=T0 + 2 * HOUR,
            )

    assert result.cached_count == 0
    assert result.analyzed_count == 1  # re-analyzed despite existing cache
    assert client.messages.create.call_count == 2


def test_internal_sender_message_never_calls_claude(db_path):
    client = MagicMock()
    client.messages.create.return_value = _fake_response(_verdict_payload())

    with get_report_connection(db_path) as conn:
        _seed_message(conn, "m1", "t1", "priya@regencyelectricals.com", T0)  # internal colleague

        with patch("app.ai.claude._client", return_value=client):
            result = analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=False, now_ms=T0 + HOUR,
            )

    assert client.messages.create.call_count == 0
    assert result.cached_count == 0
    assert result.analyzed_count == 0
    assert "t1" not in result.states  # internal-first thread is never an enquiry


def test_failed_classification_recorded_not_silently_dropped(db_path):
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("boom")  # non-anthropic error -> propagates as None via _call...

    # Use a genuinely malformed response instead, which _request_with_retry
    # handles gracefully (returns the response, validation fails -> None).
    client.messages.create.side_effect = None
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")])

    with get_report_connection(db_path) as conn:
        _seed_message(conn, "m1", "t1", "purchase@abc.com", T0)

        with patch("app.ai.claude._client", return_value=client):
            result = analyze_window(
                conn, ["t1"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=False, now_ms=T0 + HOUR,
            )

    assert result.failed_message_ids == ["m1"]
    assert result.analyzed_count == 1  # attempted, just unsuccessful
    assert "t1" not in result.states  # no verdict -> not a confirmed enquiry


def test_analyze_window_computes_state_for_multiple_threads(db_path):
    client = MagicMock()
    client.messages.create.return_value = _fake_response(_verdict_payload())

    with get_report_connection(db_path) as conn:
        _seed_message(conn, "m1", "t1", "purchase@abc.com", T0)
        _seed_message(conn, "m2", "t2", "purchase@xyz.com", T0)

        with patch("app.ai.claude._client", return_value=client):
            result = analyze_window(
                conn, ["t1", "t2"], window_end_ms=T0 + HOUR,
                mailbox=MAILBOX, employee_aliases=[], internal_domains=["regencyelectricals.com"],
                model="m", max_retries=1, reprocess=False, now_ms=T0 + HOUR,
            )

    assert set(result.states.keys()) == {"t1", "t2"}
    assert result.analyzed_count == 2
