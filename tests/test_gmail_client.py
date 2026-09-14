"""Unit tests for app.gmail.client.

These exercise only the pure parsing functions and error-handling
paths using fixture data / mocks -- no real Gmail credentials or
network access required.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from app.gmail.client import (
    EmailMessage,
    extract_body,
    get_raw_message,
    list_message_ids,
    parse_message,
)


def b64(text: str) -> str:
    """URL-safe base64 encode, matching how Gmail encodes body data."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def make_http_error(status: int = 500) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp, b"boom")


# --- extract_body -----------------------------------------------------


def test_extract_body_plain_text_only():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": b64("Hello, we need 20 MCCBs.")},
    }
    assert extract_body(payload) == "Hello, we need 20 MCCBs."


def test_extract_body_html_only_is_stripped():
    html = "<html><body><p>Hello <b>there</b></p><script>track()</script></body></html>"
    payload = {
        "mimeType": "text/html",
        "body": {"data": b64(html)},
    }
    text = extract_body(payload)
    assert "Hello" in text
    assert "there" in text
    assert "track()" not in text
    assert "<" not in text


def test_extract_body_multipart_alternative_prefers_plain_text():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64("Plain version")}},
            {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
        ],
    }
    assert extract_body(payload) == "Plain version"


def test_extract_body_multipart_mixed_with_attachment():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64("Please see attached PO.")}},
                    {"mimeType": "text/html", "body": {"data": b64("<p>Please see attached PO.</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "po.pdf",
                "body": {"attachmentId": "abc123", "size": 4096},
            },
        ],
    }
    assert extract_body(payload) == "Please see attached PO."


def test_extract_body_empty_payload_returns_empty_string():
    assert extract_body({}) == ""


def test_extract_body_truncates_oversized_content(monkeypatch):
    import app.gmail.client as client_module

    monkeypatch.setattr(client_module, "MAX_BODY_CHARS", 10)
    payload = {"mimeType": "text/plain", "body": {"data": b64("x" * 100)}}
    text = client_module.extract_body(payload)
    assert text.startswith("x" * 10)
    assert "truncated" in text


def test_extract_body_handles_malformed_base64_gracefully():
    payload = {"mimeType": "text/plain", "body": {"data": "not-valid-base64!!!"}}
    # Should not raise -- just return whatever it can decode (possibly empty).
    assert isinstance(extract_body(payload), str)


# --- parse_message ------------------------------------------------------


def test_parse_message_extracts_headers_and_ids():
    raw = {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {
            "headers": [
                {"name": "From", "value": "ABC Industries <purchase@abc.com>"},
                {"name": "To", "value": "enquiry@regencyelectrical.com"},
                {"name": "Subject", "value": "MCCB Requirement"},
                {"name": "Date", "value": "Mon, 1 Sep 2025 09:12:00 +0530"},
            ],
            "mimeType": "text/plain",
            "body": {"data": b64("We need 20 Schneider MCCB 250A units.")},
        },
    }

    msg = parse_message(raw)

    assert msg == EmailMessage(
        gmail_message_id="msg-1",
        gmail_thread_id="thread-1",
        sender="ABC Industries <purchase@abc.com>",
        recipient="enquiry@regencyelectrical.com",
        subject="MCCB Requirement",
        received_at="Mon, 1 Sep 2025 09:12:00 +0530",
        body="We need 20 Schneider MCCB 250A units.",
    )


def test_parse_message_header_lookup_is_case_insensitive():
    raw = {
        "id": "msg-2",
        "threadId": "thread-2",
        "payload": {
            "headers": [{"name": "subject", "value": "lowercase header name"}],
        },
    }
    assert parse_message(raw).subject == "lowercase header name"


def test_parse_message_missing_headers_default_to_empty_string():
    raw = {"id": "msg-3", "threadId": "thread-3", "payload": {}}
    msg = parse_message(raw)
    assert msg.sender == ""
    assert msg.recipient == ""
    assert msg.subject == ""
    assert msg.received_at is None
    assert msg.body == ""


# --- Gmail API error handling (mocked service) ---------------------------


def test_list_message_ids_returns_empty_list_on_http_error():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
        make_http_error()
    )
    assert list_message_ids(service) == []


def test_list_message_ids_returns_ids_from_response():
    service = MagicMock()
    execute = service.users.return_value.messages.return_value.list.return_value.execute
    execute.return_value = {"messages": [{"id": "a"}, {"id": "b"}]}
    assert list_message_ids(service) == ["a", "b"]


def test_get_raw_message_returns_none_on_http_error():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
        make_http_error(404)
    )
    assert get_raw_message(service, "missing-id") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
