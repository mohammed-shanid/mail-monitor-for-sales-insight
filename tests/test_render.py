"""Unit tests for app.reporting.render.render_report -- SPEC.md §21.3
and the display-limit rules in §16.2.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.enquiry.metrics import BrandCount, Metrics, PendingItem, PriorityItem
from app.timewindow import Window

IST = ZoneInfo("Asia/Kolkata")

from app.reporting.render import render_report

WINDOW = Window(start_ms=1789237800000, end_ms=1789324199999, mode="date", tz="Asia/Kolkata")
# 1789237800000 == 13 Sep 2026 00:00:00.000 IST; 1789324199999 == 13 Sep 2026 23:59:59.999 IST


def _empty_metrics(**overrides) -> Metrics:
    base = dict(
        window_start_ms=WINDOW.start_ms, window_end_ms=WINDOW.end_ms,
        received=0, attended=0, pending=0, closed=0,
        rfq_received=0, quotations_sent=0, quotation_responses=0, supplier_quotations_received=0,
        brand_counts=[], pending_items=[], priority_items=[], messages_unanalyzed=0,
    )
    base.update(overrides)
    return Metrics(**base)


def test_header_shows_date_and_ist_times():
    text = render_report(_empty_metrics(), WINDOW, IST, "Summary.")
    assert "📅 13 September 2026" in text
    assert "⏰ 00:00 IST → 23:59 IST" in text


def test_enquiry_summary_always_renders_even_at_zero():
    text = render_report(_empty_metrics(), WINDOW, IST, "No enquiries today.")
    assert "📩 ENQUIRY SUMMARY" in text
    assert "Received: 0" in text
    assert "Attended: 0" in text
    assert "Pending: 0" in text
    assert "Closed: 0  (includes enquiries received earlier)" in text


def test_quotation_section_omitted_when_all_zero():
    text = render_report(_empty_metrics(), WINDOW, IST, "s")
    assert "QUOTATION ACTIVITY" not in text


def test_quotation_section_renders_all_four_labels():
    metrics = _empty_metrics(rfq_received=11, quotations_sent=7, quotation_responses=3, supplier_quotations_received=1)
    text = render_report(metrics, WINDOW, IST, "s")
    assert "RFQ / quotation requests received: 11" in text
    assert "Quotations sent: 7" in text
    assert "Quotation responses: 3" in text
    assert "Supplier quotations received: 1" in text


def test_brands_section_omitted_when_empty():
    text = render_report(_empty_metrics(), WINDOW, IST, "s")
    assert "TOP BRANDS" not in text


def test_brands_section_numbered_with_footnote():
    metrics = _empty_metrics(brand_counts=[
        BrandCount(brand="Schneider", count=8), BrandCount(brand="Siemens", count=5),
    ])
    text = render_report(metrics, WINDOW, IST, "s")
    assert "1. Schneider — 8" in text
    assert "2. Siemens — 5" in text
    assert "Multi-brand enquiries are counted under each brand." in text


def test_pending_section_omitted_when_empty():
    text = render_report(_empty_metrics(), WINDOW, IST, "s")
    assert "PENDING ENQUIRIES" not in text


def test_pending_section_shows_detail_line_and_waiting():
    item = PendingItem(
        customer_name="ABC Industries", product="MCCB 250A", brands=["Schneider"],
        quantity="20 nos", received_at_ms=WINDOW.start_ms + 10 * 3600_000 + 42 * 60_000,
        waiting_ms=5 * 3600_000 + 18 * 60_000,
    )
    metrics = _empty_metrics(pending_items=[item])
    text = render_report(metrics, WINDOW, IST, "s")
    assert "1. ABC Industries" in text
    assert "MCCB 250A | Schneider | 20 nos" in text
    assert "Waiting: 5h 18m" in text


def test_pending_section_respects_display_limit_with_more_line():
    items = [
        PendingItem(customer_name=f"Customer {i}", product="p", brands=[], quantity="",
                    received_at_ms=WINDOW.start_ms, waiting_ms=i * 1000)
        for i in range(15)
    ]
    metrics = _empty_metrics(pending_items=items)
    text = render_report(metrics, WINDOW, IST, "s", pending_display_limit=10)

    assert text.count("Customer") == 10
    assert "…and 5 more" in text


def test_priority_section_omitted_when_empty():
    text = render_report(_empty_metrics(), WINDOW, IST, "s")
    assert "IMPORTANT" not in text


def test_priority_section_shows_evidence_quoted():
    item = PriorityItem(customer_name="ABC Industries", evidence="need delivery by Friday", waiting_ms=0)
    metrics = _empty_metrics(priority_items=[item])
    text = render_report(metrics, WINDOW, IST, "s")
    assert "1. ABC Industries — customer states requirement is urgent" in text
    assert '"need delivery by Friday"' in text


def test_priority_section_respects_display_limit():
    items = [PriorityItem(customer_name=f"C{i}", evidence="urgent", waiting_ms=0) for i in range(7)]
    metrics = _empty_metrics(priority_items=items)
    text = render_report(metrics, WINDOW, IST, "s", priority_display_limit=5)
    assert "…and 2 more" in text


def test_caveat_line_for_unanalyzed_messages():
    metrics = _empty_metrics(messages_unanalyzed=3)
    text = render_report(metrics, WINDOW, IST, "s")
    assert "3 message(s) could not be analysed" in text


def test_no_caveat_line_when_all_analyzed():
    text = render_report(_empty_metrics(messages_unanalyzed=0), WINDOW, IST, "s")
    assert "could not be analysed" not in text


def test_management_summary_section_present():
    text = render_report(_empty_metrics(), WINDOW, IST, "24 enquiries came in today.")
    assert "🧠 MANAGEMENT SUMMARY" in text
    assert "24 enquiries came in today." in text


def test_no_fabricated_monetary_values():
    # Structural check: the renderer itself never introduces a currency
    # symbol anywhere in its own template text.
    metrics = _empty_metrics(received=5, pending=5, pending_items=[
        PendingItem(customer_name="X", product="p", brands=[], quantity="", received_at_ms=WINDOW.start_ms, waiting_ms=0)
    ])
    text = render_report(metrics, WINDOW, IST, "No monetary figures here.")
    for symbol in ("₹", "$", "Rs."):
        assert symbol not in text
