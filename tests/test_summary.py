"""Unit tests for app.reporting.summary.generate_summary_text -- SPEC.md
§20.2 case 41, PHASE0_DECISIONS.md Q8. app.ai.claude.generate_summary
is monkeypatched (no network).
"""

from __future__ import annotations

from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.enquiry.metrics import Metrics
from app.reporting.summary import generate_summary_text
from app.timewindow import Window

IST = ZoneInfo("Asia/Kolkata")
WINDOW = Window(start_ms=1789237800000, end_ms=1789324199999, mode="date", tz="Asia/Kolkata")
# 13 Sep 2026 00:00:00.000 IST -> 13 Sep 2026 23:59:59.999 IST


def _metrics(**overrides) -> Metrics:
    base = dict(
        window_start_ms=WINDOW.start_ms, window_end_ms=WINDOW.end_ms,
        received=24, attended=19, pending=5, closed=8,
        rfq_received=11, quotations_sent=7, quotation_responses=0, supplier_quotations_received=0,
        brand_counts=[], pending_items=[], priority_items=[], messages_unanalyzed=0,
    )
    base.update(overrides)
    return Metrics(**base)


def test_valid_summary_using_only_metrics_numbers_passes_through():
    with patch("app.reporting.summary.generate_summary", return_value="24 enquiries came in; 19 were attended."):
        result = generate_summary_text(_metrics(), WINDOW, IST)
    assert result == "24 enquiries came in; 19 were attended."


def test_summary_with_zero_numerals_is_valid():
    with patch("app.reporting.summary.generate_summary", return_value="Business as usual today, nothing urgent."):
        result = generate_summary_text(_metrics(), WINDOW, IST)
    assert result == "Business as usual today, nothing urgent."


def test_summary_with_date_numerals_is_valid():
    # "13 September" -- day-of-month, not a metrics number, but allowed.
    with patch("app.reporting.summary.generate_summary", return_value="On 13 September, 24 enquiries came in."):
        result = generate_summary_text(_metrics(), WINDOW, IST)
    assert result == "On 13 September, 24 enquiries came in."


def test_summary_with_fabricated_number_is_rejected():
    with patch("app.reporting.summary.generate_summary", return_value="Revenue grew by 42% this month."):
        result = generate_summary_text(_metrics(), WINDOW, IST)
    # Template fallback, not the fabricated "42".
    assert "42" not in result
    assert "24 enquiries were received" in result


def test_summary_none_from_claude_uses_template():
    with patch("app.reporting.summary.generate_summary", return_value=None):
        result = generate_summary_text(_metrics(), WINDOW, IST)
    assert "24 enquiries were received" in result
    assert "19 attended" in result
    assert "5 still pending" in result


def test_template_mentions_closed_when_nonzero():
    with patch("app.reporting.summary.generate_summary", return_value=None):
        result = generate_summary_text(_metrics(closed=8), WINDOW, IST)
    assert "8 enquiries were closed" in result


def test_template_zero_enquiries_is_valid_sentence():
    metrics = _metrics(received=0, attended=0, pending=0, closed=0, rfq_received=0, quotations_sent=0)
    with patch("app.reporting.summary.generate_summary", return_value=None):
        result = generate_summary_text(metrics, WINDOW, IST)
    assert result == "0 enquiries were received."


def test_summary_calls_claude_with_metrics_payload_only():
    captured = {}

    def fake_generate_summary(payload):
        captured["payload"] = payload
        return "Summary."

    with patch("app.reporting.summary.generate_summary", side_effect=fake_generate_summary):
        generate_summary_text(_metrics(), WINDOW, IST)

    assert captured["payload"]["received"] == 24
    assert captured["payload"]["attended"] == 19
    assert "body" not in captured["payload"]  # never raw email content
