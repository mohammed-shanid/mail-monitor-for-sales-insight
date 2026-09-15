"""Unit tests for app.ai.claude.

All Claude API calls are mocked -- no network access or CLAUDE_API_KEY
required. These tests only check our own request construction,
response parsing, and validation/error-handling logic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import anthropic
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


# =============================================================================
# Report path (SPEC.md §15), added Stage 3. Everything above is unchanged
# and still exercises extract_new_enquiry/classify_reply_intent/
# summarize_enquiry and the bot's own prompts exclusively.
# =============================================================================

import app.ai.claude as claude_module
from app.ai.claude import classify_message, generate_summary
from app.enquiry.models import Verdict


def _full_verdict_payload(**overrides) -> dict:
    payload = {
        "is_enquiry": True,
        "counterparty_type": "customer",
        "customer_name": "Ramesh Kumar",
        "company": "ABC Industries",
        "product": "MCCB 250A",
        "requirement": "Price and availability for 20 units",
        "quantity": "20 nos",
        "brands": ["Schneider"],
        "quotation_signal": "rfq_received",
        "closure_signal": False,
        "closure_evidence": None,
        "closure_kind": None,
        "urgency": False,
        "urgency_evidence": None,
        "confidence": "high",
    }
    payload.update(overrides)
    return payload


# --- classify_message ---------------------------------------------------


def test_classify_message_happy_path():
    payload = _full_verdict_payload()
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = classify_message(
            target_subject="MCCB Requirement",
            target_body="We need 20 Schneider MCCB 250A units, please quote.",
            context_messages=[],
        )

    assert isinstance(result, Verdict)
    assert result.is_enquiry is True
    assert result.customer_name == "Ramesh Kumar"
    assert result.brands == ["Schneider"]
    assert result.quotation_signal == "rfq_received"


def test_classify_message_every_field_null_is_valid():
    payload = _full_verdict_payload(
        is_enquiry=None, counterparty_type=None, customer_name=None, company=None,
        product=None, requirement=None, quantity=None, quotation_signal=None,
        closure_signal=None, urgency=None, confidence=None, brands=[],
    )
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = classify_message(target_subject="s", target_body="b", context_messages=[])

    assert result.is_enquiry is None
    assert result.customer_name is None
    assert result.brands == []


def test_classify_message_includes_context_in_request():
    payload = _full_verdict_payload()
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return fake_response(payload)

    client = MagicMock()
    client.messages.create.side_effect = fake_create

    with patch("app.ai.claude._client", return_value=client):
        classify_message(
            target_subject="Re: MCCB Requirement",
            target_body="Yes please proceed with the order.",
            context_messages=[{"sender": "purchase@abc.com", "body": "We need 20 MCCB units."}],
        )

    user_message = captured["messages"][0]["content"]
    assert "We need 20 MCCB units." in user_message
    assert "Yes please proceed with the order." in user_message
    assert "TARGET MESSAGE" in user_message


def test_classify_message_passes_temperature_zero():
    payload = _full_verdict_payload()
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return fake_response(payload)

    client = MagicMock()
    client.messages.create.side_effect = fake_create

    with patch("app.ai.claude._client", return_value=client):
        classify_message(target_subject="s", target_body="b", context_messages=[])

    assert captured["temperature"] == 0


def test_classify_message_rejects_quotation_signal_outside_enum():
    payload = _full_verdict_payload(quotation_signal="made_up_signal")
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = classify_message(target_subject="s", target_body="b", context_messages=[])
    assert result is None


def test_classify_message_rejects_closure_kind_outside_enum():
    payload = _full_verdict_payload(closure_signal=True, closure_kind="made_up_kind")
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = classify_message(target_subject="s", target_body="b", context_messages=[])
    assert result is None


def test_classify_message_rejects_missing_field():
    payload = _full_verdict_payload()
    del payload["closure_kind"]
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = classify_message(target_subject="s", target_body="b", context_messages=[])
    assert result is None


def test_classify_message_rejects_non_list_brands():
    payload = _full_verdict_payload(brands="Schneider")  # should be a list
    with patch("app.ai.claude._client", return_value=mock_client_returning(fake_response(payload))):
        result = classify_message(target_subject="s", target_body="b", context_messages=[])
    assert result is None


def test_classify_message_returns_none_on_malformed_json():
    bad_response = SimpleNamespace(content=[SimpleNamespace(type="text", text="not json")])
    with patch("app.ai.claude._client", return_value=mock_client_returning(bad_response)):
        result = classify_message(target_subject="s", target_body="b", context_messages=[])
    assert result is None


# --- retry behaviour (_request_with_retry) -----------------------------------


def test_classify_message_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(claude_module.time, "sleep", lambda seconds: None)  # no real waiting in tests

    exc = anthropic.RateLimitError("rate limited", response=_fake_httpx_response(429), body=None)
    payload = _full_verdict_payload()
    client = MagicMock()
    client.messages.create.side_effect = [exc, fake_response(payload)]

    with patch("app.ai.claude._client", return_value=client):
        result = classify_message(target_subject="s", target_body="b", context_messages=[], max_retries=3)

    assert result is not None
    assert client.messages.create.call_count == 2


def test_classify_message_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(claude_module.time, "sleep", lambda seconds: None)

    exc = anthropic.RateLimitError("rate limited", response=_fake_httpx_response(429), body=None)
    client = MagicMock()
    client.messages.create.side_effect = exc  # always raises

    with patch("app.ai.claude._client", return_value=client):
        result = classify_message(target_subject="s", target_body="b", context_messages=[], max_retries=2)

    assert result is None
    assert client.messages.create.call_count == 3  # initial attempt + 2 retries


def test_classify_message_does_not_retry_on_non_retryable_status_error(monkeypatch):
    monkeypatch.setattr(claude_module.time, "sleep", lambda seconds: (_ for _ in ()).throw(AssertionError("should not sleep")))

    exc = anthropic.APIStatusError("bad request", response=_fake_httpx_response(400), body=None)
    client = MagicMock()
    client.messages.create.side_effect = exc

    with patch("app.ai.claude._client", return_value=client):
        result = classify_message(target_subject="s", target_body="b", context_messages=[], max_retries=3)

    assert result is None
    assert client.messages.create.call_count == 1  # no retry attempted


def test_classify_message_retries_on_5xx_status_error(monkeypatch):
    monkeypatch.setattr(claude_module.time, "sleep", lambda seconds: None)

    exc = anthropic.APIStatusError("server error", response=_fake_httpx_response(503), body=None)
    payload = _full_verdict_payload()
    client = MagicMock()
    client.messages.create.side_effect = [exc, fake_response(payload)]

    with patch("app.ai.claude._client", return_value=client):
        result = classify_message(target_subject="s", target_body="b", context_messages=[], max_retries=3)

    assert result is not None
    assert client.messages.create.call_count == 2


# --- generate_summary ---------------------------------------------------


def test_generate_summary_happy_path():
    with patch(
        "app.ai.claude._client",
        return_value=mock_client_returning(fake_text_response("24 enquiries came in, 19 were attended.")),
    ):
        result = generate_summary({"received": 24, "attended": 19})

    assert result == "24 enquiries came in, 19 were attended."


def test_generate_summary_sends_only_metrics_json_no_raw_email():
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return fake_text_response("Summary text.")

    client = MagicMock()
    client.messages.create.side_effect = fake_create

    with patch("app.ai.claude._client", return_value=client):
        generate_summary({"received": 24, "attended": 19, "pending": 5})

    user_message = captured["messages"][0]["content"]
    assert "24" in user_message
    assert "19" in user_message
    # Never any email-shaped content -- just the JSON metrics payload.
    assert user_message.startswith("{")
    assert captured["temperature"] == 0


def test_generate_summary_returns_none_on_empty_response():
    empty_response = SimpleNamespace(content=[])
    with patch("app.ai.claude._client", return_value=mock_client_returning(empty_response)):
        result = generate_summary({"received": 1})
    assert result is None


def test_generate_summary_returns_none_on_rate_limit_after_retries(monkeypatch):
    monkeypatch.setattr(claude_module.time, "sleep", lambda seconds: None)
    exc = anthropic.RateLimitError("rate limited", response=_fake_httpx_response(429), body=None)
    client = MagicMock()
    client.messages.create.side_effect = exc

    with patch("app.ai.claude._client", return_value=client):
        result = generate_summary({"received": 1}, max_retries=1)

    assert result is None
    assert client.messages.create.call_count == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
