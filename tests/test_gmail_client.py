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


# =============================================================================
# Report path (SPEC.md §5), added Stage 2. Everything above is unchanged and
# still exercises the bot's list_message_ids/get_raw_message/fetch_messages.
# =============================================================================

from zoneinfo import ZoneInfo

from app.gmail.client import (
    GmailFetchError,
    build_query,
    get_raw_message_or_raise,
    get_thread,
    list_message_ids_paginated,
)

IST = ZoneInfo("Asia/Kolkata")


# --- EmailMessage / parse_message report-path fields ------------------------


def test_parse_message_extracts_internal_date_ms():
    raw = {
        "id": "msg-1",
        "threadId": "thread-1",
        "internalDate": "1757488320000",
        "payload": {"headers": [], "mimeType": "text/plain", "body": {"data": b64("hi")}},
    }
    assert parse_message(raw).internal_date_ms == 1757488320000


def test_parse_message_missing_internal_date_is_none():
    raw = {"id": "msg-2", "threadId": "thread-2", "payload": {}}
    assert parse_message(raw).internal_date_ms is None


def test_parse_message_malformed_internal_date_is_none_not_raise():
    raw = {"id": "msg-3", "threadId": "thread-3", "internalDate": "not-a-number", "payload": {}}
    assert parse_message(raw).internal_date_ms is None


def test_parse_message_extracts_cc():
    raw = {
        "id": "msg-4",
        "threadId": "thread-4",
        "payload": {
            "headers": [{"name": "Cc", "value": "manager@regencyelectricals.com"}],
        },
    }
    assert parse_message(raw).cc == "manager@regencyelectricals.com"


@pytest.mark.parametrize(
    "headers",
    [
        [{"name": "Auto-Submitted", "value": "auto-replied"}],
        [{"name": "X-Autoreply", "value": "yes"}],
        [{"name": "X-Autorespond", "value": "yes"}],
        [{"name": "Precedence", "value": "bulk"}],
        [{"name": "Precedence", "value": "auto_reply"}],
    ],
)
def test_parse_message_detects_auto_reply_headers(headers):
    raw = {"id": "m", "threadId": "t", "payload": {"headers": headers}}
    assert parse_message(raw).is_auto_reply is True


@pytest.mark.parametrize(
    "subject",
    ["Automatic reply: Out of office", "Out of Office until Monday", "Out of office: back soon"],
)
def test_parse_message_detects_auto_reply_subject_prefix(subject):
    raw = {"id": "m", "threadId": "t", "payload": {"headers": [{"name": "Subject", "value": subject}]}}
    assert parse_message(raw).is_auto_reply is True


def test_parse_message_ordinary_message_is_not_auto_reply():
    raw = {"id": "m", "threadId": "t", "payload": {"headers": [{"name": "Subject", "value": "MCCB Requirement"}]}}
    assert parse_message(raw).is_auto_reply is False


def test_parse_message_extracts_attachment_filename_and_mime_type():
    raw = {
        "id": "m",
        "threadId": "t",
        "payload": {
            "headers": [],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("see attached")}},
                {"mimeType": "application/pdf", "filename": "quote.pdf", "body": {"attachmentId": "a1"}},
            ],
        },
    }
    assert parse_message(raw).attachments == [("quote.pdf", "application/pdf")]


def test_parse_message_no_attachments_is_empty_list():
    raw = {"id": "m", "threadId": "t", "payload": {"headers": [], "mimeType": "text/plain", "body": {"data": b64("hi")}}}
    assert parse_message(raw).attachments == []


def test_extract_body_html_strips_gmail_quote_div():
    html = (
        "<div>New reply text here.</div>"
        '<div class="gmail_quote">On Mon, 1 Sep 2025, ABC wrote:<br>Old quoted content</div>'
    )
    payload = {"mimeType": "text/html", "body": {"data": b64(html)}}
    text = extract_body(payload)
    assert "New reply text here." in text
    assert "Old quoted content" not in text
    assert "On Mon, 1 Sep 2025" not in text


def test_extract_body_html_without_gmail_quote_is_unaffected():
    html = "<div>Hello <b>there</b></div>"
    payload = {"mimeType": "text/html", "body": {"data": b64(html)}}
    text = extract_body(payload)
    assert "Hello" in text
    assert "there" in text


# --- build_query --------------------------------------------------------


def test_build_query_never_restricts_to_inbox():
    # Appendix A non-negotiable #1: SENT mail must be included.
    query = build_query(1757443200000, 1757529599999, IST)
    assert "in:inbox" not in query


def test_build_query_excludes_drafts_and_chats():
    query = build_query(1757443200000, 1757529599999, IST)
    assert "-in:drafts" in query
    assert "-in:chats" in query


def test_build_query_uses_ist_calendar_dates():
    # 10 Sep 2026 00:00 IST -> 10 Sep 2026 23:59:59.999 IST
    start_ms = 1757443200000  # 10 Sep 2025 00:00 UTC (placeholder instant)
    query = build_query(start_ms, start_ms + 86_400_000 - 1, IST)
    assert query.startswith("after:")
    assert " before:" in query


# --- list_message_ids_paginated ---------------------------------------------


def test_list_message_ids_paginated_follows_next_page_token():
    service = MagicMock()
    execute = service.users.return_value.messages.return_value.list.return_value.execute
    execute.side_effect = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "page2"},
        {"messages": [{"id": "c"}]},
    ]
    ids = list_message_ids_paginated(service, "after:2026/09/10")
    assert ids == ["a", "b", "c"]
    assert execute.call_count == 2


def test_list_message_ids_paginated_raises_on_http_error():
    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.side_effect = (
        make_http_error()
    )
    with pytest.raises(GmailFetchError):
        list_message_ids_paginated(service, "after:2026/09/10")


def test_list_message_ids_paginated_respects_limit():
    service = MagicMock()
    execute = service.users.return_value.messages.return_value.list.return_value.execute
    execute.return_value = {"messages": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    ids = list_message_ids_paginated(service, "q", limit=2)
    assert ids == ["a", "b"]


# --- get_raw_message_or_raise / get_thread -----------------------------------


def test_get_raw_message_or_raise_returns_payload():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {"id": "m1"}
    assert get_raw_message_or_raise(service, "m1") == {"id": "m1"}


def test_get_raw_message_or_raise_raises_gmail_fetch_error():
    service = MagicMock()
    service.users.return_value.messages.return_value.get.return_value.execute.side_effect = (
        make_http_error(404)
    )
    with pytest.raises(GmailFetchError):
        get_raw_message_or_raise(service, "missing")


def test_get_thread_returns_payload():
    service = MagicMock()
    service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "id": "t1",
        "messages": [{"id": "m1"}, {"id": "m2"}],
    }
    thread = get_thread(service, "t1")
    assert len(thread["messages"]) == 2


def test_get_thread_raises_gmail_fetch_error():
    service = MagicMock()
    service.users.return_value.threads.return_value.get.return_value.execute.side_effect = (
        make_http_error(500)
    )
    with pytest.raises(GmailFetchError):
        get_thread(service, "t1")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
