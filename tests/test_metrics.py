"""Unit tests for app.enquiry.metrics.compute_metrics -- SPEC.md §20.2
cases 30-36. Pure function: no DB, no network, no AI.
"""

from __future__ import annotations

from app.enquiry.metrics import compute_metrics
from app.enquiry.models import EnquiryState, ReportEmail, ReportStatus, Verdict
from app.timewindow import Window

WINDOW = Window(start_ms=1_000_000, end_ms=1_000_000 + 86_400_000 - 1, mode="date", tz="Asia/Kolkata")


def state(thread_id, *, status, received_at=None, brands=None, closed_at=None, is_priority=False,
          priority_evidence=None, last_activity_at=None, customer_name="ABC Industries", quantity="20 nos"):
    received_at = received_at if received_at is not None else WINDOW.start_ms + 1000
    return EnquiryState(
        gmail_thread_id=thread_id, status=status, last_sender="customer",
        customer_name=customer_name, company="ABC Industries", customer_email="a@abc.com",
        counterparty_type="customer", subject="s", product="MCCB 250A", requirement="20 units",
        quantity=quantity, brands=brands or [], is_priority=is_priority, priority_evidence=priority_evidence,
        received_at=received_at, last_activity_at=last_activity_at or received_at,
        closed_at=closed_at, closure_evidence=None, closure_kind=None,
    )


def rmsg(mid, thread_id, received_at):
    return ReportEmail(
        id=None, gmail_message_id=mid, gmail_thread_id=thread_id, sender="a@abc.com",
        sender_domain="abc.com", recipient="u.ruma@regencyelectricals.com", subject="s", body="b",
        received_at=received_at, direction="inbound", is_auto_reply=False, has_attachments=False,
        ingested_at=received_at, processed=False,
    )


def verdict(**overrides):
    base = dict(
        is_enquiry=True, counterparty_type="customer", customer_name="ABC", company="ABC",
        product="p", requirement="r", quantity="q", brands=[], quotation_signal=None,
        closure_signal=False, closure_evidence=None, closure_kind=None,
        urgency=False, urgency_evidence=None, confidence="high",
    )
    base.update(overrides)
    return Verdict(**base)


# --- 32. Invariant: Received == Attended + Pending ----------------------------


def test_invariant_received_equals_attended_plus_pending():
    states = {
        "t1": state("t1", status=ReportStatus.PENDING),
        "t2": state("t2", status=ReportStatus.ATTENDED),
        "t3": state("t3", status=ReportStatus.CLOSED),
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)

    assert metrics.received == 3
    assert metrics.attended == 2  # ATTENDED + CLOSED
    assert metrics.pending == 1
    assert metrics.received == metrics.attended + metrics.pending


def test_invariant_holds_with_zero_enquiries():
    metrics = compute_metrics({}, {}, {}, WINDOW)
    assert metrics.received == 0
    assert metrics.received == metrics.attended + metrics.pending


# --- 30. Four quotation types counted separately ------------------------------


def test_four_quotation_types_counted_separately():
    states = {"t1": state("t1", status=ReportStatus.PENDING)}
    messages = {
        "m1": rmsg("m1", "t1", WINDOW.start_ms + 1000),
        "m2": rmsg("m2", "t1", WINDOW.start_ms + 2000),
        "m3": rmsg("m3", "t1", WINDOW.start_ms + 3000),
        "m4": rmsg("m4", "t1", WINDOW.start_ms + 4000),
        "m5": rmsg("m5", "t1", WINDOW.start_ms + 5000),  # no quotation signal
    }
    verdicts = {
        "m1": verdict(quotation_signal="rfq_received"),
        "m2": verdict(quotation_signal="quotation_sent"),
        "m3": verdict(quotation_signal="quotation_response"),
        "m4": verdict(quotation_signal="supplier_quotation"),
        "m5": verdict(quotation_signal=None),
    }
    metrics = compute_metrics(states, messages, verdicts, WINDOW)

    assert metrics.rfq_received == 1
    assert metrics.quotations_sent == 1
    assert metrics.quotation_responses == 1
    assert metrics.supplier_quotations_received == 1


def test_quotation_metrics_only_count_messages_inside_window():
    states = {}
    messages = {
        "m1": rmsg("m1", "t1", WINDOW.start_ms - 100_000),  # before window
        "m2": rmsg("m2", "t1", WINDOW.start_ms + 1000),  # inside
    }
    verdicts = {"m1": verdict(quotation_signal="rfq_received"), "m2": verdict(quotation_signal="rfq_received")}
    metrics = compute_metrics(states, messages, verdicts, WINDOW)
    assert metrics.rfq_received == 1


# --- 27/28. Brand counts, multi-brand under each ------------------------------


def test_brand_counts_multi_brand_enquiry_counted_under_each():
    states = {
        "t1": state("t1", status=ReportStatus.PENDING, brands=["Schneider", "Siemens"]),
        "t2": state("t2", status=ReportStatus.ATTENDED, brands=["Schneider"]),
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)

    counts = {bc.brand: bc.count for bc in metrics.brand_counts}
    assert counts["Schneider"] == 2
    assert counts["Siemens"] == 1
    # Sum exceeds total enquiry count (2) -- correct and intentional (§10.3).
    assert sum(counts.values()) > metrics.received


def test_brand_counts_sorted_descending():
    states = {
        "t1": state("t1", status=ReportStatus.PENDING, brands=["Siemens"]),
        "t2": state("t2", status=ReportStatus.PENDING, brands=["Schneider"]),
        "t3": state("t3", status=ReportStatus.PENDING, brands=["Schneider"]),
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert metrics.brand_counts[0].brand == "Schneider"
    assert metrics.brand_counts[0].count == 2


# --- 29. Unknown brand surfaces, not hidden -----------------------------------


def test_enquiry_with_no_brand_counts_as_unknown():
    states = {"t1": state("t1", status=ReportStatus.PENDING, brands=[])}
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert any(bc.brand == "Unknown" and bc.count == 1 for bc in metrics.brand_counts)


# --- 34. Waiting duration anchored to window_end ------------------------------


def test_waiting_duration_anchored_to_window_end_not_now():
    states = {
        "t1": state(
            "t1", status=ReportStatus.PENDING,
            received_at=WINDOW.start_ms + 1000, last_activity_at=WINDOW.start_ms + 1000,
        )
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    expected_wait = WINDOW.end_ms - (WINDOW.start_ms + 1000)
    assert metrics.pending_items[0].waiting_ms == expected_wait


def test_pending_items_sorted_longest_waiting_first():
    states = {
        "t1": state("t1", status=ReportStatus.PENDING, last_activity_at=WINDOW.start_ms + 5000),
        "t2": state("t2", status=ReportStatus.PENDING, last_activity_at=WINDOW.start_ms + 1000),
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert [p.waiting_ms for p in metrics.pending_items] == sorted(
        (p.waiting_ms for p in metrics.pending_items), reverse=True
    )
    # t2's last activity is earlier -> longer wait -> listed first.
    assert metrics.pending_items[0].waiting_ms > metrics.pending_items[1].waiting_ms


# --- 35. Closed may exceed Received, and is independent -----------------------


def test_closed_can_exceed_received_referencing_earlier_enquiries():
    states = {
        # Received last week, closed today (inside window).
        "t1": state("t1", status=ReportStatus.CLOSED, received_at=WINDOW.start_ms - 1_000_000, closed_at=WINDOW.start_ms + 1000),
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert metrics.received == 0  # not received inside the window
    assert metrics.closed == 1  # but closed inside it


def test_closed_excludes_closures_outside_window():
    states = {"t1": state("t1", status=ReportStatus.CLOSED, closed_at=WINDOW.end_ms + 1_000_000)}
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert metrics.closed == 0


# --- Priority items (Q15) -----------------------------------------------------


def test_priority_item_requires_evidence():
    states = {"t1": state("t1", status=ReportStatus.PENDING, is_priority=True, priority_evidence=None)}
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert metrics.priority_items == []  # no evidence -> not listed, per Q15


def test_priority_item_included_with_evidence():
    states = {
        "t1": state("t1", status=ReportStatus.PENDING, is_priority=True, priority_evidence="need delivery by Friday")
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert len(metrics.priority_items) == 1
    assert metrics.priority_items[0].evidence == "need delivery by Friday"


def test_priority_item_pending_but_received_earlier_still_included():
    # Q15: received-in-window OR pending-as-of-window_end.
    states = {
        "t1": state(
            "t1", status=ReportStatus.PENDING, received_at=WINDOW.start_ms - 1_000_000,
            is_priority=True, priority_evidence="urgent",
        )
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert len(metrics.priority_items) == 1


def test_priority_item_excluded_when_neither_received_nor_pending():
    # Closed, and received before the window -- neither condition holds.
    states = {
        "t1": state(
            "t1", status=ReportStatus.CLOSED, received_at=WINDOW.start_ms - 1_000_000,
            closed_at=WINDOW.start_ms - 500_000, is_priority=True, priority_evidence="urgent",
        )
    }
    metrics = compute_metrics(states, {}, {}, WINDOW)
    assert metrics.priority_items == []


# --- messages_unanalyzed passthrough (§15.5 caveat) ---------------------------


def test_messages_unanalyzed_passthrough():
    metrics = compute_metrics({}, {}, {}, WINDOW, messages_unanalyzed=3)
    assert metrics.messages_unanalyzed == 3
