"""Unit tests for app.telegram.client. No real network access -- every
test mocks urllib.request.urlopen or unsets the bot token.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
from io import BytesIO
from unittest.mock import patch

import pytest

import app.telegram.client as client_module
from app.telegram.client import TelegramError, get_updates, send_message


class FakeResponse:
    """Minimal stand-in for the object urllib.request.urlopen() returns
    when used as a context manager -- just enough for json.load()."""

    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")


def test_send_message_returns_result_on_success():
    with patch("app.telegram.client.urllib.request.urlopen", return_value=FakeResponse({"ok": True, "result": {"message_id": 1}})):
        result = send_message(123, "hello")
    assert result == {"message_id": 1}


def test_get_updates_returns_result_list():
    payload = {"ok": True, "result": [{"update_id": 1, "message": {"text": "hi"}}]}
    with patch("app.telegram.client.urllib.request.urlopen", return_value=FakeResponse(payload)):
        result = get_updates(offset=5)
    assert result == payload["result"]


def test_get_updates_passes_offset_and_timeout_params():
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return FakeResponse({"ok": True, "result": []})

    with patch("app.telegram.client.urllib.request.urlopen", side_effect=fake_urlopen):
        get_updates(offset=42, timeout=20)

    assert "offset=42" in captured["data"].decode()
    assert "timeout=20" in captured["data"].decode()
    assert captured["timeout"] == 30  # padded past the long-poll timeout


def test_call_raises_when_token_missing(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "")
    with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
        send_message(1, "hi")


def test_call_raises_on_telegram_api_error():
    payload = {"ok": False, "description": "chat not found"}
    with patch("app.telegram.client.urllib.request.urlopen", return_value=FakeResponse(payload)):
        with pytest.raises(TelegramError, match="chat not found"):
            send_message(999999, "hi")


def test_call_raises_on_network_error():
    with patch("app.telegram.client.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        with pytest.raises(TelegramError, match="Network error"):
            send_message(1, "hi")


def test_call_raises_telegram_error_on_bare_socket_timeout():
    """Regression test: a long-poll request that simply times out with
    no new messages (the normal, expected case) raises a bare
    socket.timeout, NOT urllib.error.URLError -- this must be caught
    and turned into a TelegramError, not crash the caller (this
    actually crashed the whole bot process in a live test run before
    the OSError fix in app.telegram.client._call).
    """
    with patch("app.telegram.client.urllib.request.urlopen", side_effect=socket.timeout("timed out")):
        with pytest.raises(TelegramError, match="Network error"):
            get_updates()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# =============================================================================
# Report path (SPEC.md §16), added Stage 4. `_call`/`get_updates`/
# `send_message` above are unchanged and still exercise the bot's own
# functions exclusively.
# =============================================================================

import app.telegram.client as client_module
from app.telegram.client import DeliveryResult, chunk_text, send


# --- chunk_text ---------------------------------------------------------


def test_chunk_text_returns_single_chunk_when_under_limit():
    assert chunk_text("short text", 100) == ["short text"]


def test_chunk_text_splits_at_blank_line_boundaries():
    text = "Section A\ncontent" + "\n\n" + "Section B\ncontent"
    chunks = chunk_text(text, limit=len("Section A\ncontent"))
    assert len(chunks) == 2
    assert chunks[0] == "Section A\ncontent"
    assert chunks[1] == "Section B\ncontent"
    # Never split mid-block.
    assert "Section A" not in chunks[1]


def test_chunk_text_never_exceeds_limit_for_normal_input():
    blocks = [f"Block {i}\nsome content here" for i in range(50)]
    text = "\n\n".join(blocks)
    chunks = chunk_text(text, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert "\n\n".join(chunks) == text or all(b in "".join(chunks) for b in blocks)


def test_chunk_text_hard_splits_a_single_oversized_block():
    text = "x" * 500
    chunks = chunk_text(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


# --- send() ---------------------------------------------------------------


class FakeHTTPResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_send_single_part_success(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "3")

    monkeypatch.setattr(
        client_module.urllib.request, "urlopen",
        lambda *a, **k: FakeHTTPResponse({"ok": True, "result": {"message_id": 1}}),
    )

    result = send("Report text")

    assert result == DeliveryResult(success=True, parts_sent=1, parts_total=1)


def test_send_missing_chat_id_fails_without_network_call(monkeypatch):
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "")

    result = send("Report text")

    assert result.success is False
    assert "TELEGRAM_CHAT_ID" in result.error


def test_send_missing_bot_token_fails_cleanly(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "3")

    result = send("Report text")

    assert result.success is False
    assert "TELEGRAM_BOT_TOKEN" in result.error


def test_send_chunks_long_report_into_multiple_parts(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "50")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "3")

    sent_bodies = []

    def fake_urlopen(request, timeout=None):
        body = urllib.parse.parse_qs(request.data.decode())["text"][0]
        sent_bodies.append(body)
        return FakeHTTPResponse({"ok": True, "result": {}})

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    long_text = "\n\n".join(f"Section {i}\nsome content" for i in range(10))
    result = send(long_text)

    assert result.success is True
    assert result.parts_total > 1
    assert "(Part 1/" in sent_bodies[0]


def test_send_retries_on_429_honouring_retry_after(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "3")
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    call_count = {"n": 0}

    def fake_urlopen(request, timeout=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise urllib.error.HTTPError(
                "url", 429, "Too Many Requests",
                hdrs=None,
                fp=BytesIO(json.dumps({"ok": False, "parameters": {"retry_after": 1}}).encode()),
            )
        return FakeHTTPResponse({"ok": True, "result": {}})

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    result = send("short text")

    assert result.success is True
    assert call_count["n"] == 2


def test_send_gives_up_after_max_retries_reports_partial_failure(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "1")
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "url", 500, "Server Error", hdrs=None, fp=BytesIO(json.dumps({"ok": False}).encode())
        )

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    result = send("short text")

    assert result.success is False
    assert result.parts_sent == 0
    assert result.error is not None


def test_send_never_claims_success_without_api_confirmation(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "0")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "url", 400, "Bad Request", hdrs=None, fp=BytesIO(json.dumps({"ok": False, "description": "bad"}).encode())
        )

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    result = send("short text")

    assert result.success is False  # never True unless the API confirmed


# --- correct use of TELEGRAM_CHAT_ID ----------------------------------------


def test_send_uses_the_configured_chat_id_in_the_request(monkeypatch):
    """The chat_id actually sent to Telegram must be exactly
    config.TELEGRAM_CHAT_ID -- not the bot's own updates, not a
    hardcoded value, not something invented.
    """
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "987654321")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "3")

    captured_params = {}

    def fake_urlopen(request, timeout=None):
        captured_params.update(urllib.parse.parse_qs(request.data.decode()))
        return FakeHTTPResponse({"ok": True, "result": {}})

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    result = send("Report text")

    assert result.success is True
    assert captured_params["chat_id"] == ["987654321"]


def test_send_uses_chat_id_for_every_chunk_of_a_multi_part_send(monkeypatch):
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "555")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "50")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "3")

    seen_chat_ids = []

    def fake_urlopen(request, timeout=None):
        params = urllib.parse.parse_qs(request.data.decode())
        seen_chat_ids.append(params["chat_id"][0])
        return FakeHTTPResponse({"ok": True, "result": {}})

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    long_text = "\n\n".join(f"Section {i}\nsome content" for i in range(10))
    result = send(long_text)

    assert result.success is True
    assert len(seen_chat_ids) > 1
    assert all(chat_id == "555" for chat_id in seen_chat_ids)


# --- secrets never appear in logs/errors ------------------------------------


def test_send_error_never_contains_the_bot_token_on_api_failure(monkeypatch):
    real_looking_token = "123456789:AAFakeRealisticLookingBotTokenValueXYZ"
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", real_looking_token)
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "0")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "url", 401, "Unauthorized",
            hdrs=None,
            fp=BytesIO(json.dumps({"ok": False, "description": "Unauthorized"}).encode()),
        )

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    result = send("short text")

    assert result.success is False
    assert real_looking_token not in result.error


def test_send_error_never_contains_the_bot_token_on_network_failure(monkeypatch, caplog):
    real_looking_token = "123456789:AAFakeRealisticLookingBotTokenValueXYZ"
    monkeypatch.setattr(client_module, "TELEGRAM_BOT_TOKEN", real_looking_token)
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(client_module.config, "TELEGRAM_CHUNK_CHARS", "3800")
    monkeypatch.setattr(client_module.config, "TELEGRAM_MAX_RETRIES", "0")

    def fake_urlopen(request, timeout=None):
        # A raw network-level failure -- the request URL (with the
        # embedded token) exists on `request`, but must never be
        # echoed back through the exception's own string form.
        raise OSError("Connection refused")

    monkeypatch.setattr(client_module.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level("ERROR"):
        result = send("short text")

    assert result.success is False
    assert real_looking_token not in result.error
    assert real_looking_token not in caplog.text
