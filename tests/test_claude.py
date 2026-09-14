"""Unit tests for app.ai.claude.

All Claude API calls are mocked -- no network access or CLAUDE_API_KEY
required. These tests only check our own request construction,
response parsing, and validation/error-handling logic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.ai.claude import (
    REPLY_INTENTS,
    classify_reply_intent,
    extract_new_enquiry,
    summarize_enquiry,
)


def fake_text_response(text: str):
    """Build a fake anthropic Message with one plain-text block (no
    JSON schema involved) -- what summarize_enquiry() expects back.
    """
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def fake_response(payload: dict):
    """Build a fake anthropic Message with one text block, mirroring
    what output_config's json_schema format guarantees: content[0] is
    text containing valid JSON.
    """
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def mock_client_returning(response):
    client = MagicMock()
    client.messages.create.return_value = response
    return client


def mock_client_raising(exc: Exception):
    client = MagicMock()
    client.messages.create.side_effect = exc
    return client


# --- extract_new_enquiry -------------------------------------------------


def test_extract_new_enquiry_happy_path():
    payload = {
        "is_enquiry": True,
        "customer_name": "ABC Industries",
        "customer_email": "purchase@abc.com",
        "product": "Schneider MCCB 250A",
        "quantity": 20,
    }
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = extract_new_enquiry("MCCB Requirement", "We need 20 units of MCCB 250A.")

    assert result is not None
    assert result.is_enquiry is True
    assert result.customer_name == "ABC Industries"
    assert result.customer_email == "purchase@abc.com"
    assert result.product == "Schneider MCCB 250A"
    assert result.quantity == 20


def test_extract_new_enquiry_not_an_enquiry_with_nulls():
    payload = {
        "is_enquiry": False,
        "customer_name": None,
        "customer_email": None,
        "product": None,
        "quantity": None,
    }
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = extract_new_enquiry("Re: Newsletter", "Check out our latest plugins!")

    assert result is not None
    assert result.is_enquiry is False
    assert result.customer_name is None
    assert result.quantity is None


def test_extract_new_enquiry_returns_none_on_missing_field():
    # Missing "quantity" entirely -- must not be treated as valid.
    payload = {
        "is_enquiry": True,
        "customer_name": "ABC Industries",
        "customer_email": "purchase@abc.com",
        "product": "MCCB",
    }
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = extract_new_enquiry("subject", "body")

    assert result is None


def test_extract_new_enquiry_returns_none_on_wrong_type():
    payload = {
        "is_enquiry": "yes",  # should be a bool, not a string
        "customer_name": "ABC Industries",
        "customer_email": "purchase@abc.com",
        "product": "MCCB",
        "quantity": 20,
    }
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = extract_new_enquiry("subject", "body")

    assert result is None


def test_extract_new_enquiry_returns_none_on_malformed_json():
    bad_response = SimpleNamespace(content=[SimpleNamespace(type="text", text="{not valid json")])
    with patch("app.ai.claude._client", return_value=mock_client_returning(bad_response)):
        result = extract_new_enquiry("subject", "body")

    assert result is None


def test_extract_new_enquiry_returns_none_on_empty_response():
    empty_response = SimpleNamespace(content=[])
    with patch("app.ai.claude._client", return_value=mock_client_returning(empty_response)):
        result = extract_new_enquiry("subject", "body")

    assert result is None


# --- classify_reply_intent ------------------------------------------------


@pytest.mark.parametrize("intent", REPLY_INTENTS)
def test_classify_reply_intent_accepts_every_controlled_intent(intent):
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response({"intent": intent}))):
        result = classify_reply_intent("some message body")

    assert result == intent


def test_classify_reply_intent_rejects_invented_intent():
    # Even if Claude (or a bug) returns something outside the controlled
    # set, we must never pass it through as a real status.
    with patch(
        "app.ai.claude._client",
        return_value=mock_client_returning(fake_response({"intent": "MAYBE_INTERESTED"})),
    ):
        result = classify_reply_intent("some message body")

    assert result is None


# --- API error handling (shared by both functions via _call_claude) ------


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _fake_httpx_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_fake_request())


def test_extract_new_enquiry_returns_none_on_rate_limit():
    import anthropic

    exc = anthropic.RateLimitError("rate limited", response=_fake_httpx_response(429), body=None)
    with patch("app.ai.claude._client", return_value=mock_client_raising(exc)):
        result = extract_new_enquiry("subject", "body")

    assert result is None


def test_extract_new_enquiry_returns_none_on_timeout():
    import anthropic

    exc = anthropic.APITimeoutError(request=_fake_request())
    with patch("app.ai.claude._client", return_value=mock_client_raising(exc)):
        result = extract_new_enquiry("subject", "body")

    assert result is None


def test_extract_new_enquiry_returns_none_on_connection_error():
    import anthropic

    exc = anthropic.APIConnectionError(request=_fake_request())
    with patch("app.ai.claude._client", return_value=mock_client_raising(exc)):
        result = extract_new_enquiry("subject", "body")

    assert result is None


def test_extract_new_enquiry_returns_none_on_api_status_error():
    import anthropic

    exc = anthropic.APIStatusError("server error", response=_fake_httpx_response(500), body=None)
    with patch("app.ai.claude._client", return_value=mock_client_raising(exc)):
        result = extract_new_enquiry("subject", "body")

    assert result is None


# --- summarize_enquiry ---------------------------------------------------


_SAMPLE_THREAD = [
    {"sender": "purchase@abc.com", "body": "We need 20 MCCBs.", "received_at": "2026-09-01T09:00:00+00:00"},
    {"sender": "sales@regencyelectricals.com", "body": "Quote attached.", "received_at": "2026-09-01T14:00:00+00:00"},
]


def test_summarize_enquiry_returns_claude_text():
    response = fake_text_response("ABC Industries requested 20 MCCBs; a quotation has been sent.")
    with patch("app.ai.claude._client", return_value=mock_client_returning(response)):
        result = summarize_enquiry(
            customer_name="ABC Industries", product="MCCB", quantity=20, status="QUOTATION_SENT",
            thread_messages=_SAMPLE_THREAD,
        )

    assert result == "ABC Industries requested 20 MCCBs; a quotation has been sent."


def test_summarize_enquiry_strips_whitespace():
    response = fake_text_response("  a summary with padding  \n")
    with patch("app.ai.claude._client", return_value=mock_client_returning(response)):
        result = summarize_enquiry(
            customer_name="ABC", product="MCCB", quantity=1, status="NEW", thread_messages=_SAMPLE_THREAD
        )

    assert result == "a summary with padding"


def test_summarize_enquiry_returns_none_with_no_thread_messages():
    assert summarize_enquiry(customer_name="ABC", product="MCCB", quantity=1, status="NEW", thread_messages=[]) is None


def test_summarize_enquiry_returns_none_on_empty_response():
    empty_response = SimpleNamespace(content=[])
    with patch("app.ai.claude._client", return_value=mock_client_returning(empty_response)):
        result = summarize_enquiry(
            customer_name="ABC", product="MCCB", quantity=1, status="NEW", thread_messages=_SAMPLE_THREAD
        )

    assert result is None


def test_summarize_enquiry_returns_none_on_rate_limit():
    import anthropic

    exc = anthropic.RateLimitError("rate limited", response=_fake_httpx_response(429), body=None)
    with patch("app.ai.claude._client", return_value=mock_client_raising(exc)):
        result = summarize_enquiry(
            customer_name="ABC", product="MCCB", quantity=1, status="NEW", thread_messages=_SAMPLE_THREAD
        )

    assert result is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
