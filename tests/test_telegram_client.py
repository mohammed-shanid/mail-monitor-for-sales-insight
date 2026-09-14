"""Unit tests for app.telegram.client. No real network access -- every
test mocks urllib.request.urlopen or unsets the bot token.
"""

from __future__ import annotations

import json
import socket
import urllib.error
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
