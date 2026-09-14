"""Unit tests for app.router.formatter -- pure string formatting, no
database or network involved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.enquiry.models import Enquiry
from app.router.formatter import (
    format_count,
    format_elapsed,
    format_enquiry_list,
    format_today_summary,
)

FIXED_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


def make_enquiry(**overrides) -> Enquiry:
    defaults = dict(
        id=1,
        gmail_thread_id="thread-1",
        customer_name="ABC Industries",
        customer_email="purchase@abc.com",
        subject="MCCB Requirement",
        product="MCCB 250A",
        quantity=20,
        status="NEW",
        last_sender="customer",
        received_at="2026-09-12T09:00:00+00:00",
        last_activity_at="2026-09-12T09:00:00+00:00",
        closed_at=None,
    )
    defaults.update(overrides)
    return Enquiry(**defaults)


# --- format_elapsed ----------------------------------------------------


def test_format_elapsed_minutes():
    since = "2026-09-12T11:45:00+00:00"
    assert format_elapsed(since, now=FIXED_NOW) == "15m"


def test_format_elapsed_hours():
    since = "2026-09-12T09:00:00+00:00"
    assert format_elapsed(since, now=FIXED_NOW) == "3h"


def test_format_elapsed_days_and_hours():
    since = "2026-09-10T08:00:00+00:00"
    assert format_elapsed(since, now=FIXED_NOW) == "2d 4h"


def test_format_elapsed_exact_days_omits_zero_hours():
    since = "2026-09-10T12:00:00+00:00"
    assert format_elapsed(since, now=FIXED_NOW) == "2d"


def test_format_elapsed_never_negative_for_future_timestamp():
    since = "2026-09-12T13:00:00+00:00"  # "in the future" relative to now
    assert format_elapsed(since, now=FIXED_NOW) == "0m"


# --- format_count / format_today_summary ---------------------------------


def test_format_count():
    assert format_count("Open enquiries", 8) == "Open enquiries: 8"


def test_format_today_summary():
    summary = {"received": 12, "closed": 4, "open": 8, "unanswered": 3}
    text = format_today_summary(summary)
    assert "Today's Enquiries" in text
    assert "Received: 12" in text
    assert "Closed: 4" in text
    assert "Open: 8" in text
    assert "Unanswered: 3" in text


# --- format_enquiry_list ---------------------------------------------------


def test_format_enquiry_list_empty():
    text = format_enquiry_list("Pending Enquiries", "⏳", [])
    assert "Pending Enquiries" in text
    assert "None right now." in text


def test_format_enquiry_list_includes_customer_product_and_time():
    # Note: no timezone conversion happens here -- the stored UTC time
    # is formatted as-is (see the module's UTC caveat).
    enquiry = make_enquiry(customer_name="ABC Industries", product="MCCB 250A", received_at="2026-09-12T09:12:00+00:00")
    text = format_enquiry_list("Pending Enquiries", "⏳", [enquiry])

    assert "1. ABC Industries" in text
    assert "Product: MCCB 250A" in text
    assert "Received: 09:12 AM" in text


def test_format_enquiry_list_falls_back_for_missing_fields():
    enquiry = make_enquiry(customer_name=None, product=None)
    text = format_enquiry_list("Pending Enquiries", "⏳", [enquiry])

    assert "Unknown customer" in text
    assert "Not specified" in text


def test_format_enquiry_list_numbers_multiple_entries():
    enquiries = [make_enquiry(customer_name="ABC Industries"), make_enquiry(customer_name="XYZ Electricals")]
    text = format_enquiry_list("Pending Enquiries", "⏳", enquiries)

    assert "1. ABC Industries" in text
    assert "2. XYZ Electricals" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
