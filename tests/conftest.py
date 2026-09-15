"""Shared fixtures.

`db_path` mirrors the fixture already duplicated in five bot test files
(test_database.py, test_gmail_sync.py, test_open_ended.py,
test_founder_queries.py, test_intent.py) -- identical behaviour, so any
of those files could switch to this one later without changing
anything. They are left as-is for now (Stage 1 does not touch bot
files); a local fixture of the same name in a test file always shadows
this one, so there is no conflict either way.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def frozen_now():
    """A single fixed instant (Monday 14 Sep 2026, 10:00 IST, as UTC),
    for tests that just need *a* deterministic clock and don't care
    which one.
    """
    return datetime(2026, 9, 14, 10, 0, 0, tzinfo=IST).astimezone(timezone.utc)


@pytest.fixture
def make_ist_now():
    """Factory: build a tz-aware UTC datetime from IST wall-clock
    components, for tests that need a *specific* instant (a particular
    weekday, a particular time of day at a window boundary).

        now = make_ist_now(2026, 9, 16, 0, 0, 30)  # Wed 00:00:30 IST
    """

    def _make(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
        return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)

    return _make


@pytest.fixture
def tmp_env(monkeypatch):
    """Set environment variables for one test, then reload app.config so
    its module-level constants reflect them -- app.config reads env vars
    once at import time, so a bare monkeypatch.setenv() after the fact
    has no effect on already-bound constants without this.

    Usage:
        def test_x(tmp_env):
            config = tmp_env({"REPORT_MAILBOX": "u.ruma@regencyelectricals.com"})
            ...

    A key mapped to None simulates "unset" by setting it to an empty
    string, NOT by deleting it from the environment. Several of these
    keys have real values in the developer's actual `.env` file
    (CLAUDE_API_KEY, TELEGRAM_BOT_TOKEN, GOOGLE_CREDENTIALS_FILE, ...);
    app.config calls load_dotenv() on every reload, and load_dotenv()'s
    default override=False only skips a key that is already *present*
    in os.environ -- a deleted key is, from its point of view, simply
    unset, so it would be immediately restored from the real .env file,
    silently defeating the test. Setting it to "" keeps it "present"
    (dotenv leaves it alone) while still reading as unset everywhere in
    app.config, which treats "" and absent identically (required-var
    and legacy-fallback checks are both truthy checks, never `is not
    None`). Reloads app.config again after the test so later tests see
    a config built from the real environment, not one left over from
    this test.
    """
    import app.config as config_module

    def _apply(env: dict) -> "config_module":
        for key, value in env.items():
            monkeypatch.setenv(key, "" if value is None else value)
        importlib.reload(config_module)
        return config_module

    yield _apply

    importlib.reload(config_module)
