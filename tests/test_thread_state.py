"""Unit tests for app.enquiry.thread_state.compute_state -- SPEC.md
§20.2 cases 17-26. Pure function: no DB, no network, no AI calls --
messages and verdicts are built directly as dataclasses.
"""

from __future__ import annotations

from app.enquiry.models import ReportEmail, ReportStatus, Verdict
from app.enquiry.thread_state import compute_state

MAILBOX = "u.ruma@regencyelectricals.com"
ALIASES = []
INTERNAL_DOMAINS = ["regencyelectricals.com"]

T0 = 1_000_000  # base instant, epoch ms
HOUR = 3_600_000


def msg(
    msg_id, thread_id, *, sender, received_at, direction="inbound", is_auto_reply=False,
    subject="MCCB Requirement", body="body",
) -> ReportEmail:
    return ReportEmail(
        id=None, gmail_message_id=msg_id, gmail_thread_id=thread_id, sender=sender,
        sender_domain=sender.split("@")[-1].rstrip(">") if "@" in sender else None,
        recipient=MAILBOX, subject=subject, body=body, received_at=received_at,
        direction=direction, is_auto_reply=is_auto_reply, has_attachments=False,
        ingested_at=received_at, processed=False,
    )


def verdict(**overrides) -> Verdict:
    base = dict(
        is_enquiry=True, counterparty_type="customer", customer_name="ABC Industries",
        company="ABC Industries", product="MCCB 250A", requirement="20 units needed",
        quantity="20 nos", brands=["Schneider"], quotation_signal="rfq_received",
        closure_signal=False, closure_evidence=None, closure_kind=None,
        urgency=False, urgency_evidence=None, confidence="high",
    )
    base.update(overrides)
    return Verdict(**base)


def _state(messages, verdicts, as_of_ms):
    return compute_state(
        messages, verdicts, as_of_ms,
        mailbox=MAILBOX, employee_aliases=ALIASES, internal_domains=INTERNAL_DOMAINS,
    )


# --- 17. New enquiry detected from inbound message ---------------------------


def test_new_enquiry_detected_from_inbound_message():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    state = _state([m1], {"m1": verdict()}, T0 + HOUR)

    assert state is not None
    assert state.gmail_thread_id == "t1"
    assert state.customer_name == "ABC Industries"
    assert state.status == ReportStatus.PENDING


# --- 18. Existing thread updates, creates no second enquiry (determinism) ---


def test_recompute_is_deterministic_across_two_calls():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    verdicts = {"m1": verdict()}

    first = _state([m1], verdicts, T0 + HOUR)
    second = _state([m1], verdicts, T0 + HOUR)

    assert first == second  # same inputs, same output -- no hidden state


# --- 19. Non-enquiry filtering ------------------------------------------------


def test_never_is_enquiry_true_returns_none():
    # e.g. a newsletter/OTP/no-reply message Claude correctly judged
    # is_enquiry=False.
    m1 = msg("m1", "t1", sender="noreply@newsletter.com", received_at=T0)
    state = _state([m1], {"m1": verdict(is_enquiry=False, counterparty_type=None)}, T0 + HOUR)
    assert state is None


def test_internal_first_message_thread_is_never_an_enquiry():
    m1 = msg("m1", "t1", sender="priya@regencyelectricals.com", received_at=T0)
    # Even if somehow classified is_enquiry=True, an internal-first thread
    # must never surface -- this is checked before the verdict is even
    # consulted for anchor selection.
    state = _state([m1], {"m1": verdict()}, T0 + HOUR)
    assert state is None


def test_supplier_thread_is_never_an_enquiry():
    m1 = msg("m1", "t1", sender="sales@supplier.com", received_at=T0)
    state = _state([m1], {"m1": verdict(counterparty_type="supplier")}, T0 + HOUR)
    assert state is None


def test_message_with_no_verdict_at_all_is_skipped_for_anchor():
    # A message Claude was never asked about (or failed) contributes
    # nothing -- must not crash, must not become the anchor.
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    state = _state([m1], {}, T0 + HOUR)
    assert state is None


# --- 20. Outbound-first thread is not an enquiry ------------------------------


def test_outbound_first_thread_is_not_an_enquiry_until_reply():
    m1 = msg("m1", "t1", sender=MAILBOX, received_at=T0, direction="outbound")
    state = _state([m1], {}, T0 + HOUR)
    assert state is None  # no verdict even makes sense for our own outbound message


def test_outbound_first_thread_becomes_enquiry_once_customer_replies():
    m1 = msg("m1", "t1", sender=MAILBOX, received_at=T0, direction="outbound")
    m2 = msg("m2", "t1", sender="purchase@abc.com", received_at=T0 + HOUR)
    state = _state([m1, m2], {"m2": verdict()}, T0 + 2 * HOUR)
    assert state is not None
    assert state.status == ReportStatus.PENDING


# --- 21. Pending detection — customer last ------------------------------------


def test_pending_when_customer_sent_last():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    state = _state([m1], {"m1": verdict()}, T0 + HOUR)
    assert state.status == ReportStatus.PENDING
    assert state.last_sender == "customer"


# --- 22. Attended detection — employee reply after customer ------------------


def test_attended_when_employee_replies_after_customer():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    state = _state([m1, m2], {"m1": verdict()}, T0 + 2 * HOUR)
    assert state.status == ReportStatus.ATTENDED
    assert state.last_sender == "employee"


def test_internal_colleague_reply_also_satisfies_attended():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender="priya@regencyelectricals.com", received_at=T0 + HOUR)
    state = _state([m1, m2], {"m1": verdict()}, T0 + 2 * HOUR)
    assert state.status == ReportStatus.ATTENDED
    assert state.last_sender == "employee"


# --- 23. Closed detection on explicit acceptance ------------------------------


def test_closed_on_explicit_acceptance():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    m3 = msg("m3", "t1", sender="purchase@abc.com", received_at=T0 + 2 * HOUR)
    verdicts = {
        "m1": verdict(),
        "m3": verdict(closure_signal=True, closure_kind="won", closure_evidence="Please proceed with the order."),
    }
    state = _state([m1, m2, m3], verdicts, T0 + 3 * HOUR)
    assert state.status == ReportStatus.CLOSED
    assert state.closure_kind == "won"
    assert state.closed_at == T0 + 2 * HOUR


# --- 24. NOT closed on "thanks" / "we'll check" / quotation-sent-only --------


def test_not_closed_on_thanks():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    m3 = msg("m3", "t1", sender="purchase@abc.com", received_at=T0 + 2 * HOUR)
    verdicts = {"m1": verdict(), "m3": verdict(closure_signal=False, quotation_signal=None)}
    state = _state([m1, m2, m3], verdicts, T0 + 3 * HOUR)
    assert state.status == ReportStatus.PENDING  # customer's "thanks" is last, unattended


def test_not_closed_on_quotation_sent_only():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    verdicts = {"m1": verdict()}
    state = _state([m1, m2], verdicts, T0 + 2 * HOUR)
    assert state.status == ReportStatus.ATTENDED  # not closed -- just attended


def test_not_closed_on_we_will_check():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    m3 = msg("m3", "t1", sender="purchase@abc.com", received_at=T0 + 2 * HOUR)
    verdicts = {"m1": verdict(), "m3": verdict(closure_signal=False)}
    state = _state([m1, m2, m3], verdicts, T0 + 3 * HOUR)
    assert state.status == ReportStatus.PENDING


# --- 25. Closed enquiry reopened by new inbound message -----------------------


def test_closed_enquiry_reopened_by_new_inbound_message():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    m3 = msg("m3", "t1", sender="purchase@abc.com", received_at=T0 + 2 * HOUR)  # accepts
    m4 = msg("m4", "t1", sender="purchase@abc.com", received_at=T0 + 3 * HOUR)  # new demand

    verdicts = {
        "m1": verdict(),
        "m3": verdict(closure_signal=True, closure_kind="won"),
        "m4": verdict(closure_signal=False),
    }
    state = _state([m1, m2, m3, m4], verdicts, T0 + 4 * HOUR)

    assert state.status == ReportStatus.PENDING  # reopened, not closed
    assert state.closed_at is None


def test_as_of_before_reopen_is_still_closed():
    # Historical reproducibility (SPEC.md §11.1): evaluated as of a
    # cutoff BEFORE the reopening message, the thread is still closed.
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    m3 = msg("m3", "t1", sender="purchase@abc.com", received_at=T0 + 2 * HOUR)
    m4 = msg("m4", "t1", sender="purchase@abc.com", received_at=T0 + 3 * HOUR)

    verdicts = {"m1": verdict(), "m3": verdict(closure_signal=True, closure_kind="won"), "m4": verdict(closure_signal=False)}
    state = _state([m1, m2, m3, m4], verdicts, as_of_ms=T0 + 2 * HOUR + 1)

    assert state.status == ReportStatus.CLOSED


# --- 26. Auto-reply does not flip last_sender ---------------------------------


def test_auto_reply_does_not_flip_last_sender():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    m3 = msg(
        "m3", "t1", sender="purchase@abc.com", received_at=T0 + 2 * HOUR,
        is_auto_reply=True, subject="Out of Office",
    )
    state = _state([m1, m2, m3], {"m1": verdict()}, T0 + 3 * HOUR)

    # last RELEVANT (non-auto-reply) message is still the employee's --
    # the auto-reply must not make this look PENDING again.
    assert state.status == ReportStatus.ATTENDED
    assert state.last_sender == "employee"


# --- As-of semantics / historical reproducibility -----------------------------


def test_as_of_ignores_messages_after_the_cutoff():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender=MAILBOX, received_at=T0 + HOUR, direction="outbound")
    state = _state([m1, m2], {"m1": verdict()}, as_of_ms=T0 + 30 * 60_000)  # before m2

    assert state.status == ReportStatus.PENDING  # m2 not yet visible as of this cutoff
    assert state.last_activity_at == T0


# --- Brand union / field-filling from later messages (Q7) --------------------


def test_brands_are_unioned_across_messages():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender="purchase@abc.com", received_at=T0 + HOUR)
    verdicts = {
        "m1": verdict(brands=["Schneider"]),
        "m2": verdict(is_enquiry=None, brands=["Siemens", "Schneider"]),  # dup + new
    }
    state = _state([m1, m2], verdicts, T0 + 2 * HOUR)
    assert state.brands == ["Schneider", "Siemens"]


def test_later_message_fills_null_field_but_never_overwrites():
    m1 = msg("m1", "t1", sender="purchase@abc.com", received_at=T0)
    m2 = msg("m2", "t1", sender="purchase@abc.com", received_at=T0 + HOUR)
    verdicts = {
        "m1": verdict(quantity=None, product="MCCB 250A"),
        "m2": verdict(is_enquiry=None, quantity="20 nos", product="Contactor"),  # would-be overwrite
    }
    state = _state([m1, m2], verdicts, T0 + 2 * HOUR)

    assert state.quantity == "20 nos"  # filled from later message
    assert state.product == "MCCB 250A"  # anchor's non-null value preserved
