"""Unit tests for app.telegram.bot -- the long-polling loop and update
handling. Telegram I/O and the database connection are all mocked; no
network access and no real SQLite file involved.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

import app.telegram.bot as bot_module
from app.telegram.client import TelegramError


class _StopLoop(Exception):
    """Sentinel used to break run_polling_loop's infinite loop in tests."""


@contextmanager
def _fake_connection():
    yield "FAKE_CONN"


@pytest.fixture(autouse=True)
def fake_db(monkeypatch):
    monkeypatch.setattr(bot_module, "get_connection", lambda: _fake_connection())


# --- _handle_update ---------------------------------------------------


def test_handle_update_routes_text_and_sends_reply(monkeypatch):
    seen = {}

    monkeypatch.setattr(bot_module, "route_message", lambda conn, text: f"echo: {text}")
    monkeypatch.setattr(
        bot_module, "send_message", lambda chat_id, text: seen.update(chat_id=chat_id, text=text)
    )

    update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "How many enquiries today?"}}
    bot_module._handle_update(update)

    assert seen == {"chat_id": 42, "text": "echo: How many enquiries today?"}


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": 1},  # no "message" at all (e.g. an edited_message update)
        {"update_id": 2, "message": {"chat": {"id": 1}}},  # message with no text (photo, sticker, ...)
    ],
)
def test_handle_update_ignores_non_text_updates(update, monkeypatch):
    calls = []
    monkeypatch.setattr(bot_module, "route_message", lambda conn, text: calls.append(text))
    monkeypatch.setattr(bot_module, "send_message", lambda chat_id, text: calls.append((chat_id, text)))

    bot_module._handle_update(update)

    assert calls == []


# --- run_polling_loop ---------------------------------------------------


def test_run_polling_loop_processes_updates_and_advances_offset(monkeypatch):
    handled = []
    updates_batch = [{"update_id": 5, "message": {"chat": {"id": 1}, "text": "hi"}}]
    call_count = {"n": 0}

    def fake_get_updates(offset=None, timeout=30):
        call_count["n"] += 1
        if call_count["n"] == 1:
            assert offset is None
            return updates_batch
        assert offset == 6  # advanced past update_id 5
        raise _StopLoop()

    monkeypatch.setattr(bot_module, "get_updates", fake_get_updates)
    monkeypatch.setattr(bot_module, "_handle_update", lambda update: handled.append(update))

    with pytest.raises(_StopLoop):
        bot_module.run_polling_loop(poll_timeout=1)

    assert handled == updates_batch


def test_run_polling_loop_recovers_from_telegram_error_without_crashing(monkeypatch):
    call_count = {"n": 0}

    def fake_get_updates(offset=None, timeout=30):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise TelegramError("simulated outage")
        raise _StopLoop()

    monkeypatch.setattr(bot_module, "get_updates", fake_get_updates)
    monkeypatch.setattr(bot_module.time, "sleep", lambda seconds: None)  # don't actually sleep in tests

    with pytest.raises(_StopLoop):
        bot_module.run_polling_loop(poll_timeout=1)

    assert call_count["n"] == 2


def test_run_polling_loop_skips_one_bad_update_without_aborting(monkeypatch):
    updates_batch = [
        {"update_id": 1, "message": {"chat": {"id": 1}, "text": "bad"}},
        {"update_id": 2, "message": {"chat": {"id": 2}, "text": "good"}},
    ]
    handled = []

    def fake_get_updates(offset=None, timeout=30):
        if not handled:
            return updates_batch
        raise _StopLoop()

    def flaky_handle_update(update):
        if update["update_id"] == 1:
            raise RuntimeError("simulated bug")
        handled.append(update)

    monkeypatch.setattr(bot_module, "get_updates", fake_get_updates)
    monkeypatch.setattr(bot_module, "_handle_update", flaky_handle_update)

    with pytest.raises(_StopLoop):
        bot_module.run_polling_loop(poll_timeout=1)

    assert handled == [updates_batch[1]]  # second update still processed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
