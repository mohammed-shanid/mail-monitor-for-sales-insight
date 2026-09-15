"""The only module that converts between IST (or whatever
`REPORT_TIMEZONE` is configured to) and UTC (SPEC.md §4.3, §12).

`resolve_window()` takes CLI args and an injected clock and returns a
`Window` of epoch-ms-UTC bounds -- pure function, zero I/O, zero
`datetime.now()` calls, so tests are deterministic (SPEC.md §20.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import app.config as config

_STRICT_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_MS_PER_DAY = 24 * 60 * 60 * 1000


class WindowError(ValueError):
    """Raised on any invalid CLI date/window combination (SPEC.md §12).
    report.py maps this to exit code 2, with a usage example printed.
    """


@dataclass(frozen=True)
class Window:
    start_ms: int  # inclusive, epoch ms UTC
    end_ms: int  # inclusive, epoch ms UTC
    mode: str  # yesterday | today | week | date | range
    tz: str  # e.g. "Asia/Kolkata"


def _parse_strict_date(raw: str) -> date:
    """Strict `DD/MM/YYYY` only. No ISO fallback, no single-digit day or
    month, real calendar validation (rejects `29/02` on a non-leap year,
    a day or month out of range) -- SPEC.md §12, §20.2 case 6.
    """
    match = _STRICT_DATE_RE.match(raw)
    if not match:
        raise WindowError(f"Invalid date {raw!r}: expected DD/MM/YYYY, e.g. 10/09/2026.")
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise WindowError(f"Invalid date {raw!r}: not a real calendar date.")


def _day_start_ms(day: date, tz: ZoneInfo) -> int:
    """Epoch ms (UTC) for 00:00:00.000 of `day` in `tz`."""
    dt = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def _day_end_ms(day: date, tz: ZoneInfo) -> int:
    """Epoch ms (UTC) for 23:59:59.999 of `day` in `tz` -- one ms before
    the next day's start, so there is no float/microsecond rounding.
    """
    return _day_start_ms(day + timedelta(days=1), tz) - 1


def _monday_of(day: date) -> date:
    """Monday of the calendar week containing `day` (ISO weekday,
    Monday == 0) -- derived from the date itself, never system locale.
    """
    return day - timedelta(days=day.weekday())


def _max_window_days() -> int:
    # Read live off app.config (not bound at import time) so tests can
    # monkeypatch app.config.MAX_WINDOW_DAYS and see it take effect.
    return int(config.MAX_WINDOW_DAYS)


def _check_window_length(start_ms: int, natural_end_ms: int) -> None:
    span_ms = natural_end_ms - start_ms + 1
    limit_ms = _max_window_days() * _MS_PER_DAY
    if span_ms > limit_ms:
        raise WindowError(
            f"Window spans more than MAX_WINDOW_DAYS ({_max_window_days()} days)."
        )


def _check_start_not_future(start_ms: int, now_ms: int, label: str) -> None:
    if start_ms > now_ms:
        raise WindowError(f"{label} is in the future.")


def resolve_window(args, now: datetime, tz: ZoneInfo) -> Window:
    """CLI args + an injected clock -> a `Window`.

    `now` must be a tz-aware datetime (any zone; converted internally to
    `tz` to determine "today"). Raises `WindowError` on any SPEC.md §12
    validation failure -- malformed date, reversed range, an unpaired
    `--from`/`--to`, more than one mode flag, a future start, or a
    window longer than `MAX_WINDOW_DAYS`.
    """
    if now.tzinfo is None:
        raise ValueError("resolve_window() requires a tz-aware `now`")

    now_ms = int(now.timestamp() * 1000)
    today = now.astimezone(tz).date()

    today_flag = bool(getattr(args, "today", False))
    week_flag = bool(getattr(args, "week", False))
    date_flag = getattr(args, "date", None)
    from_flag = getattr(args, "from_date", None)
    to_flag = getattr(args, "to_date", None)

    modes_used = []
    if today_flag:
        modes_used.append("today")
    if week_flag:
        modes_used.append("week")
    if date_flag:
        modes_used.append("date")
    if from_flag or to_flag:
        modes_used.append("range")

    if len(modes_used) > 1:
        raise WindowError(
            "Only one reporting mode may be used at a time "
            "(--today, --week, --date, or --from/--to)."
        )

    if bool(from_flag) != bool(to_flag):
        raise WindowError("--from requires --to, and --to requires --from.")

    mode = modes_used[0] if modes_used else "yesterday"

    if mode == "yesterday":
        day = today - timedelta(days=1)
        start_ms = _day_start_ms(day, tz)
        natural_end_ms = _day_end_ms(day, tz)

    elif mode == "today":
        start_ms = _day_start_ms(today, tz)
        natural_end_ms = now_ms

    elif mode == "week":
        start_ms = _day_start_ms(_monday_of(today), tz)
        natural_end_ms = now_ms

    elif mode == "date":
        day = _parse_strict_date(date_flag)
        start_ms = _day_start_ms(day, tz)
        natural_end_ms = _day_end_ms(day, tz)
        _check_start_not_future(start_ms, now_ms, "--date")

    else:  # mode == "range"
        from_day = _parse_strict_date(from_flag)
        to_day = _parse_strict_date(to_flag)
        if from_day > to_day:
            raise WindowError("--from must not be later than --to.")
        start_ms = _day_start_ms(from_day, tz)
        natural_end_ms = _day_end_ms(to_day, tz)
        _check_start_not_future(start_ms, now_ms, "--from")

    _check_window_length(start_ms, natural_end_ms)
    end_ms = min(natural_end_ms, now_ms)

    return Window(start_ms=start_ms, end_ms=end_ms, mode=mode, tz=str(tz))


def format_period_header(window: Window, tz: ZoneInfo) -> str:
    """SPEC.md §21.3's "Report period" header lines, so `report.py` does
    no date formatting of its own.
    """
    utc = ZoneInfo("UTC")
    start_dt = datetime.fromtimestamp(window.start_ms / 1000, tz=utc).astimezone(tz)
    end_dt = datetime.fromtimestamp(window.end_ms / 1000, tz=utc).astimezone(tz)
    tz_abbr = start_dt.strftime("%Z")

    if start_dt.date() == end_dt.date():
        date_line = f"{start_dt.day} {start_dt.strftime('%B %Y')}"
    else:
        date_line = (
            f"{start_dt.day} {start_dt.strftime('%B %Y')} → "
            f"{end_dt.day} {end_dt.strftime('%B %Y')}"
        )

    time_line = f"{start_dt:%H:%M} {tz_abbr} → {end_dt:%H:%M} {tz_abbr}"

    return f"Report period:\n{date_line}\n{time_line}"
