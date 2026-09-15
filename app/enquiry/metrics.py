"""All counting (SPEC.md §9, §10, §11), added Stage 4. No AI, no I/O --
EnquiryStates + per-message Verdicts + a Window in, a Metrics object
out (SPEC.md §4.3, CLAUDE.md: "analysis/metrics.py must be importable
and testable with zero network and zero DB").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from app.enquiry.brands import UNKNOWN_BRAND, normalise
from app.enquiry.models import EnquiryState, QuotationSignal, ReportEmail, ReportStatus, Verdict
from app.timewindow import Window


@dataclass(frozen=True)
class BrandCount:
    brand: str
    count: int


@dataclass(frozen=True)
class PendingItem:
    customer_name: str
    product: str
    brands: List[str]
    quantity: str
    received_at_ms: int
    waiting_ms: int


@dataclass(frozen=True)
class PriorityItem:
    customer_name: str
    evidence: str
    waiting_ms: int


@dataclass(frozen=True)
class Metrics:
    window_start_ms: int
    window_end_ms: int

    received: int
    attended: int  # SPEC.md §11.2: not-waiting-on-counterparty, received cohort (attended OR closed)
    pending: int
    closed: int  # independent, all cohorts, closed_at inside window

    rfq_received: int
    quotations_sent: int
    quotation_responses: int
    supplier_quotations_received: int

    brand_counts: List[BrandCount]  # sorted by count desc, then name -- may sum > received (§10.3)
    pending_items: List[PendingItem]  # sorted by waiting_ms desc (longest-waiting first)
    priority_items: List[PriorityItem]  # sorted by waiting_ms desc; every item has non-empty evidence

    messages_unanalyzed: int = 0  # SPEC.md §15.5 caveat count


def _in_window(ms: int, window: Window) -> bool:
    return window.start_ms <= ms <= window.end_ms


def compute_metrics(
    states: Dict[str, EnquiryState],
    messages: Dict[str, ReportEmail],
    message_verdicts: Dict[str, Verdict],
    window: Window,
    *,
    messages_unanalyzed: int = 0,
) -> Metrics:
    """`states` is every genuine enquiry's as-of-`window.end_ms` state
    (app.ai.analyze.AnalysisResult.states, one entry per touched
    thread -- not limited to threads received inside the window;
    Closed intentionally looks across all of them, SPEC.md §11.2).
    `messages`/`message_verdicts` are the same run's full per-message
    data, for the per-message quotation metrics.
    """
    received_states = [s for s in states.values() if _in_window(s.received_at, window)]
    received = len(received_states)

    # SPEC.md §11.2 (amended): Attended = not waiting on the
    # counterparty as of window_end, for the received-in-window
    # cohort -- this is what makes Received = Attended + Pending hold
    # by construction (every ReportStatus value is exactly one of
    # PENDING or {ATTENDED, CLOSED}).
    attended = sum(1 for s in received_states if s.status in (ReportStatus.ATTENDED, ReportStatus.CLOSED))
    pending = sum(1 for s in received_states if s.status == ReportStatus.PENDING)

    # Closed: independent of the received cohort -- may reference
    # enquiries received before the window (SPEC.md §11.2).
    closed = sum(
        1 for s in states.values() if s.closed_at is not None and _in_window(s.closed_at, window)
    )

    in_window_verdicts = [
        v for mid, v in message_verdicts.items()
        if mid in messages and _in_window(messages[mid].received_at, window)
    ]
    rfq_received = sum(1 for v in in_window_verdicts if v.quotation_signal == QuotationSignal.RFQ_RECEIVED)
    quotations_sent = sum(1 for v in in_window_verdicts if v.quotation_signal == QuotationSignal.QUOTATION_SENT)
    quotation_responses = sum(1 for v in in_window_verdicts if v.quotation_signal == QuotationSignal.QUOTATION_RESPONSE)
    supplier_quotations_received = sum(
        1 for v in in_window_verdicts if v.quotation_signal == QuotationSignal.SUPPLIER_QUOTATION
    )

    # Brand counts (SPEC.md §10.3, locked): one count per DISTINCT
    # brand an enquiry mentions -- sum may exceed `received`. An
    # enquiry with no brands mentioned counts once under "Unknown".
    brand_totals: Dict[str, int] = {}
    for state in received_states:
        normalised = {normalise(b) for b in state.brands} if state.brands else {UNKNOWN_BRAND}
        for brand in normalised:
            brand_totals[brand] = brand_totals.get(brand, 0) + 1
    brand_counts = sorted(
        (BrandCount(brand=b, count=c) for b, c in brand_totals.items()),
        key=lambda bc: (-bc.count, bc.brand),
    )

    pending_items = sorted(
        (
            PendingItem(
                customer_name=s.customer_name or "Unknown customer",
                product=s.product or "Not specified",
                brands=[normalise(b) for b in s.brands],
                quantity=s.quantity or "",
                received_at_ms=s.received_at,
                waiting_ms=max(window.end_ms - s.last_activity_at, 0),
            )
            for s in received_states
            if s.status == ReportStatus.PENDING
        ),
        key=lambda p: -p.waiting_ms,
    )

    # SPEC.md §11.2 amendment / PHASE0_DECISIONS.md Q15: received-in-
    # window OR pending-as-of-window_end, AND flagged priority, AND
    # carries evidence (an item with no evidence is not listed).
    priority_candidates = [
        s for s in states.values()
        if s.is_priority and s.priority_evidence and (_in_window(s.received_at, window) or s.status == ReportStatus.PENDING)
    ]
    priority_items = sorted(
        (
            PriorityItem(
                customer_name=s.customer_name or "Unknown customer",
                evidence=s.priority_evidence,
                waiting_ms=max(window.end_ms - s.last_activity_at, 0),
            )
            for s in priority_candidates
        ),
        key=lambda p: -p.waiting_ms,
    )

    return Metrics(
        window_start_ms=window.start_ms,
        window_end_ms=window.end_ms,
        received=received,
        attended=attended,
        pending=pending,
        closed=closed,
        rfq_received=rfq_received,
        quotations_sent=quotations_sent,
        quotation_responses=quotation_responses,
        supplier_quotations_received=supplier_quotations_received,
        brand_counts=brand_counts,
        pending_items=pending_items,
        priority_items=priority_items,
        messages_unanalyzed=messages_unanalyzed,
    )
