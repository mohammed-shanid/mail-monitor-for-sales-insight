"""SPEC.md §20.2 cases 1-10, plus the boundary cases from the Stage 1
prompt that the audit did not enumerate.

`app.timewindow` never reads the system clock -- every test supplies
its own `now` via the `make_ist_now`/`frozen_now` fixtures (conftest.py)
so results are exact and reproducible.

Expected epoch-ms values are computed independently of the module under
test, via plain UTC-offset arithmetic (`ist_to_epoch_ms` below) -- IST
has a fixed +05:30 offset with no DST, so this is a safe, simple oracle
that does not just re-run the implementation's own logic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.config as config
from app.timewindow import Window, WindowError, format_period_header, resolve_window

IST = ZoneInfo("Asia/Kolkata")
IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_to_epoch_ms(year, month, day, hour=0, minute=0, second=0, ms=0):
    """Independent oracle: IST wall-clock -> epoch ms UTC, via plain
    fixed-offset arithmetic (no zoneinfo, no reuse of the module's own
    helpers).
    """
    mislabeled_utc = datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=timezone.utc)
    actual_utc = mislabeled_utc - IST_OFFSET
    return int(actual_utc.timestamp() * 1000)


def make_args(**overrides):
    defaults = dict(today=False, week=False, date=None, from_date=None, to_date=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def assert_ist_offset(window: Window, tz=IST):
    """Both bounds convert to a +05:30 UTC offset in `tz`."""
    for ms, label in ((window.start_ms, "start"), (window.end_ms, "end")):
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)
        assert dt.utcoffset() == IST_OFFSET, f"{label} offset was {dt.utcoffset()}, expected +05:30"


# --- 1. Default -> yesterday full day IST -----------------------------------


def test_default_mode_is_yesterday_full_day(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)  # Monday
    window = resolve_window(make_args(), now=now, tz=IST)

    assert window.mode == "yesterday"
    assert window.start_ms == ist_to_epoch_ms(2026, 9, 13, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 13, 23, 59, 59, 999)
    assert_ist_offset(window)


# --- 2. --today -> midnight to injected now, excludes future messages -------


def test_today_mode_ends_at_now_not_end_of_day(make_ist_now):
    now = make_ist_now(2026, 9, 14, 14, 32, 7)
    window = resolve_window(make_args(today=True), now=now, tz=IST)

    assert window.mode == "today"
    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 14, 14, 32, 7, 0)
    # Not clamped to the full day -- a message at 20:00 IST today would
    # be outside this window.
    assert window.end_ms < ist_to_epoch_ms(2026, 9, 14, 23, 59, 59, 999)


def test_today_mode_at_00_00_30_is_a_30_second_window(make_ist_now):
    now = make_ist_now(2026, 9, 14, 0, 0, 30)
    window = resolve_window(make_args(today=True), now=now, tz=IST)

    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 30, 0)
    assert window.end_ms - window.start_ms == 30_000
    assert window.end_ms > window.start_ms  # not empty


# --- 3. --week -> Monday midnight IST, on a Wednesday and on a Monday -------


def test_week_mode_verified_on_a_wednesday(make_ist_now):
    # 16 Sep 2026 is a Wednesday; Monday of that week is 14 Sep.
    now = make_ist_now(2026, 9, 16, 11, 0, 0)
    window = resolve_window(make_args(week=True), now=now, tz=IST)

    assert window.mode == "week"
    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 16, 11, 0, 0, 0)


def test_week_mode_verified_on_a_monday(make_ist_now):
    now = make_ist_now(2026, 9, 14, 9, 0, 0)  # Monday itself
    window = resolve_window(make_args(week=True), now=now, tz=IST)

    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 14, 9, 0, 0, 0)


def test_week_mode_at_monday_00_05_starts_that_morning_not_previous_monday(make_ist_now):
    now = make_ist_now(2026, 9, 14, 0, 5, 0)  # Monday, just after midnight
    window = resolve_window(make_args(week=True), now=now, tz=IST)

    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 14, 0, 5, 0, 0)
    # Not the previous Monday (7 Sep).
    assert window.start_ms != ist_to_epoch_ms(2026, 9, 7, 0, 0, 0, 0)


# --- 4. --date -> correct single full day -----------------------------------


def test_date_mode_full_past_day(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    window = resolve_window(make_args(date="10/09/2026"), now=now, tz=IST)

    assert window.mode == "date"
    assert window.start_ms == ist_to_epoch_ms(2026, 9, 10, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 10, 23, 59, 59, 999)


def test_date_mode_today_clamps_end_to_now_not_end_of_day(make_ist_now):
    now = make_ist_now(2026, 9, 14, 13, 0, 0)
    window = resolve_window(make_args(date="14/09/2026"), now=now, tz=IST)

    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 14, 13, 0, 0, 0)
    assert window.end_ms < ist_to_epoch_ms(2026, 9, 14, 23, 59, 59, 999)


def test_date_mode_tomorrow_is_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(make_args(date="15/09/2026"), now=now, tz=IST)


# --- 5. --from/--to -> both bounds inclusive --------------------------------


def test_range_mode_both_bounds_inclusive(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    window = resolve_window(
        make_args(from_date="10/09/2026", to_date="12/09/2026"), now=now, tz=IST
    )

    assert window.mode == "range"
    assert window.start_ms == ist_to_epoch_ms(2026, 9, 10, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 12, 23, 59, 59, 999)


def test_range_mode_single_day_from_equals_to(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    window = resolve_window(
        make_args(from_date="10/09/2026", to_date="10/09/2026"), now=now, tz=IST
    )

    assert window.start_ms == ist_to_epoch_ms(2026, 9, 10, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 10, 23, 59, 59, 999)


def test_range_mode_from_today_to_tomorrow_clamps_end_to_now(make_ist_now):
    now = make_ist_now(2026, 9, 14, 16, 0, 0)  # today == 14 Sep
    window = resolve_window(
        make_args(from_date="14/09/2026", to_date="15/09/2026"), now=now, tz=IST
    )

    # Start valid (today, not future) -- end clamped to now, not the
    # natural 23:59:59.999 of the (future) --to date.
    assert window.start_ms == ist_to_epoch_ms(2026, 9, 14, 0, 0, 0, 0)
    assert window.end_ms == ist_to_epoch_ms(2026, 9, 14, 16, 0, 0, 0)


# --- 6. Invalid date format rejected -----------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-10",  # ISO, wrong order/separator
        "9/10/2026",  # single-digit day, no leading zero
        "32/01/2026",  # day out of range
        "13/13/2026",  # month out of range
        "29/02/2027",  # not a leap year
    ],
)
def test_invalid_date_formats_rejected(make_ist_now, raw):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(make_args(date=raw), now=now, tz=IST)


def test_leap_day_2028_is_valid(make_ist_now):
    now = make_ist_now(2028, 3, 1, 10, 0, 0)
    window = resolve_window(make_args(date="29/02/2028"), now=now, tz=IST)
    assert window.start_ms == ist_to_epoch_ms(2028, 2, 29, 0, 0, 0, 0)


# --- 7. Reversed range rejected, never swapped ------------------------------


def test_reversed_range_rejected_not_swapped(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(
            make_args(from_date="12/09/2026", to_date="10/09/2026"), now=now, tz=IST
        )


def test_from_without_to_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(make_args(from_date="10/09/2026"), now=now, tz=IST)


def test_to_without_from_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(make_args(to_date="10/09/2026"), now=now, tz=IST)


# --- 8. Mode flags combined -> rejected --------------------------------------


def test_today_and_week_combined_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(make_args(today=True, week=True), now=now, tz=IST)


def test_date_with_from_combined_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(
            make_args(date="10/09/2026", from_date="10/09/2026", to_date="12/09/2026"),
            now=now,
            tz=IST,
        )


# --- 9. Window exceeding MAX_WINDOW_DAYS -------------------------------------


def test_window_exceeding_max_window_days_rejected(make_ist_now, monkeypatch):
    monkeypatch.setattr(config, "MAX_WINDOW_DAYS", "5")
    now = make_ist_now(2026, 9, 30, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(
            make_args(from_date="01/09/2026", to_date="10/09/2026"),  # 10 days
            now=now,
            tz=IST,
        )


def test_window_within_max_window_days_allowed(make_ist_now, monkeypatch):
    monkeypatch.setattr(config, "MAX_WINDOW_DAYS", "5")
    now = make_ist_now(2026, 9, 30, 10, 0, 0)
    window = resolve_window(
        make_args(from_date="01/09/2026", to_date="05/09/2026"),  # 5 days
        now=now,
        tz=IST,
    )
    assert window.mode == "range"


def test_default_max_window_days_is_92(monkeypatch):
    monkeypatch.delenv("MAX_WINDOW_DAYS", raising=False)
    # Not reloading config here -- just confirming the module constant
    # already loaded matches SPEC.md's documented default.
    assert config.MAX_WINDOW_DAYS in ("92", 92)


# --- 10. DST-free IST boundary correctness -----------------------------------


def test_day_boundaries_are_exact_ist_offset_at_00_00_00_and_23_59_59_999(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    window = resolve_window(make_args(date="10/09/2026"), now=now, tz=IST)

    start_dt = datetime.fromtimestamp(window.start_ms / 1000, tz=timezone.utc).astimezone(IST)
    end_dt = datetime.fromtimestamp(window.end_ms / 1000, tz=timezone.utc).astimezone(IST)

    assert (start_dt.hour, start_dt.minute, start_dt.second, start_dt.microsecond) == (0, 0, 0, 0)
    assert (end_dt.hour, end_dt.minute, end_dt.second, end_dt.microsecond) == (23, 59, 59, 999000)
    assert start_dt.utcoffset() == IST_OFFSET
    assert end_dt.utcoffset() == IST_OFFSET


def test_ist_offset_asserted_at_both_bounds_in_every_mode(make_ist_now):
    now = make_ist_now(2026, 9, 16, 11, 0, 0)  # Wednesday

    for args in (
        make_args(),
        make_args(today=True),
        make_args(week=True),
        make_args(date="10/09/2026"),
        make_args(from_date="10/09/2026", to_date="12/09/2026"),
    ):
        window = resolve_window(args, now=now, tz=IST)
        assert_ist_offset(window)


# --- Future start rejected (amendment 13) -----------------------------------


def test_future_start_date_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(make_args(date="20/09/2026"), now=now, tz=IST)


def test_future_range_start_rejected(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    with pytest.raises(WindowError):
        resolve_window(
            make_args(from_date="20/09/2026", to_date="25/09/2026"), now=now, tz=IST
        )


# --- resolve_window() requires a tz-aware `now` -----------------------------


def test_naive_now_is_rejected():
    naive_now = datetime(2026, 9, 14, 10, 0, 0)
    with pytest.raises(ValueError):
        resolve_window(make_args(), now=naive_now, tz=IST)


# --- format_period_header() ---------------------------------------------------


def test_format_period_header_single_day(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    window = resolve_window(make_args(date="13/09/2026"), now=now, tz=IST)
    header = format_period_header(window, IST)

    assert header == "Report period:\n13 September 2026\n00:00 IST → 23:59 IST"


def test_format_period_header_multi_day_range(make_ist_now):
    now = make_ist_now(2026, 9, 14, 10, 0, 0)
    window = resolve_window(
        make_args(from_date="10/09/2026", to_date="12/09/2026"), now=now, tz=IST
    )
    header = format_period_header(window, IST)

    assert "10 September 2026 → 12 September 2026" in header
    assert "00:00 IST → 23:59 IST" in header
