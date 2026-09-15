"""Enquiry state computation (SPEC.md §7, §8), added Stage 3.

    compute_state(messages, verdicts, as_of_ms, ...) -> EnquiryState | None

A pure function: messages + verdicts + a cutoff in, an EnquiryState (or
None, if the thread is not a genuine enquiry) out. No I/O, no network,
no AI calls -- testable with zero DB and zero network (SPEC.md §4.3,
CLAUDE.md). State is always recomputed from the full thread, never
mutated in place (SPEC.md §8.1) -- callers must not carry a `processed`
flag that skips a thread wholesale.

Verdicts are Claude's per-message judgements (already computed and
cached, Stage 3's AI layer); this module applies the deterministic
state-transition rules on top of them (SPEC.md §15.1: "Python owns
every number [and every state transition]. Claude owns every
judgement.").
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.enquiry.models import AddressClass, CounterpartyType, EnquiryState, LastSender, ReportEmail, ReportStatus, Verdict
from app.enquiry.status import classify_address, extract_email_address

# Enquiry-level fields taken from the anchor verdict (the earliest
# inbound is_enquiry=True message) and, if still null, filled from
# later messages' verdicts -- never overwriting a non-null value
# (PHASE0_DECISIONS.md Q7).
_FILLABLE_FIELDS = ("customer_name", "company", "product", "requirement", "quantity", "counterparty_type")


def _sender_class(message: ReportEmail, *, mailbox: str, employee_aliases: List[str], internal_domains: List[str]) -> str:
    return classify_address(
        message.sender, mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains
    )


def compute_state(
    messages: List[ReportEmail],
    verdicts: Dict[str, Verdict],
    as_of_ms: int,
    *,
    mailbox: str,
    employee_aliases: List[str],
    internal_domains: List[str],
) -> Optional[EnquiryState]:
    """`messages` is the FULL hydrated thread, oldest first (including
    messages after `as_of_ms` -- they are filtered out here, not by
    the caller, so this function's contract matches SPEC.md §8.1
    exactly: "Only messages with received_at <= as_of are considered").
    `verdicts` maps gmail_message_id -> Verdict for every message that
    was actually classified (a message from an AddressClass.INTERNAL
    sender is never sent to Claude at all -- see the report pipeline
    -- so it simply has no entry here).

    Returns None when the thread is not a genuine enquiry as of
    `as_of_ms`: internal (SPEC.md §6.2), a supplier thread (SPEC.md
    §7), an outbound-first thread with no inbound demand yet (SPEC.md
    §7), or no message has ever been confidently judged
    `is_enquiry=True`.
    """
    if not messages:
        return None

    relevant = [m for m in messages if m.received_at <= as_of_ms]
    if not relevant:
        return None

    # SPEC.md §6.2: a thread whose first message is from an internal
    # colleague is never an enquiry -- checked against the thread's
    # TRUE first message (full history), not just the as-of-filtered
    # slice, since this is a property of the thread itself.
    first_class = _sender_class(
        messages[0], mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains
    )
    if first_class == AddressClass.INTERNAL:
        return None

    # --- Aggregate enquiry-level fields from the anchor verdict -------------
    # (PHASE0_DECISIONS.md Q7): earliest relevant message whose sender
    # is the true counterparty (AddressClass.EXTERNAL -- SPEC.md §7:
    # "created only from an inbound message") AND whose verdict has
    # is_enquiry=True AND is not itself a supplier (SPEC.md §7 amendment:
    # supplier threads are never enquiries).
    anchor_message: Optional[ReportEmail] = None
    anchor_verdict: Optional[Verdict] = None
    brands_seen: List[str] = []
    brands_set = set()

    for message in relevant:
        verdict = verdicts.get(message.gmail_message_id)
        if verdict is None:
            continue
        for brand in verdict.brands:
            if brand not in brands_set:
                brands_set.add(brand)
                brands_seen.append(brand)
        if anchor_verdict is not None:
            continue
        if verdict.is_enquiry is not True:
            continue
        if verdict.counterparty_type == CounterpartyType.SUPPLIER:
            continue
        sender_class = _sender_class(
            message, mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains
        )
        if sender_class != AddressClass.EXTERNAL:
            continue
        anchor_message = message
        anchor_verdict = verdict

    if anchor_message is None or anchor_verdict is None:
        return None  # never confidently judged a genuine customer enquiry

    aggregated = {field: getattr(anchor_verdict, field) for field in _FILLABLE_FIELDS}
    anchor_pos = relevant.index(anchor_message)
    for message in relevant[anchor_pos + 1:]:
        verdict = verdicts.get(message.gmail_message_id)
        if verdict is None:
            continue
        for field in _FILLABLE_FIELDS:
            if aggregated[field] is None:
                value = getattr(verdict, field)
                if value is not None:
                    aggregated[field] = value

    # --- last_sender: most recent RELEVANT (non-auto-reply) message ---------
    # (SPEC.md §8.2). An internal colleague's reply satisfies attended
    # the same way an employee's does (PHASE0_DECISIONS.md Q10).
    last_sender = LastSender.CUSTOMER
    for message in reversed(relevant):
        if message.is_auto_reply:
            continue
        sender_class = _sender_class(
            message, mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains
        )
        last_sender = (
            LastSender.EMPLOYEE if sender_class in (AddressClass.EMPLOYEE, AddressClass.INTERNAL) else LastSender.CUSTOMER
        )
        break

    # --- Closure (SPEC.md §8.2-§8.3) -----------------------------------------
    # The latest relevant message whose verdict carries closure_signal.
    # Terminal UNLESS a later genuine counterparty (external) message
    # exists after it -- SPEC.md §8.3: "A closed enquiry that receives
    # a new inbound message reverts to pending."
    closure_message: Optional[ReportEmail] = None
    closure_verdict: Optional[Verdict] = None
    for message in relevant:
        verdict = verdicts.get(message.gmail_message_id)
        if verdict and verdict.closure_signal:
            closure_message = message
            closure_verdict = verdict

    is_closed = False
    if closure_message is not None:
        later = [m for m in relevant if m.received_at > closure_message.received_at and not m.is_auto_reply]
        later_external = [
            m for m in later
            if _sender_class(m, mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains)
            == AddressClass.EXTERNAL
        ]
        is_closed = not later_external

    if is_closed:
        status = ReportStatus.CLOSED
    elif last_sender == LastSender.CUSTOMER:
        status = ReportStatus.PENDING
    else:
        status = ReportStatus.ATTENDED

    # --- Priority (PHASE0_DECISIONS.md Q15) ----------------------------------
    # Any relevant message flagged urgent; the latest such evidence wins.
    is_priority = False
    priority_evidence: Optional[str] = None
    for message in relevant:
        verdict = verdicts.get(message.gmail_message_id)
        if verdict and verdict.urgency:
            is_priority = True
            priority_evidence = verdict.urgency_evidence

    customer_email = extract_email_address(anchor_message.sender)

    return EnquiryState(
        gmail_thread_id=anchor_message.gmail_thread_id,
        status=status,
        last_sender=last_sender,
        customer_name=aggregated["customer_name"],
        company=aggregated["company"],
        customer_email=customer_email,
        counterparty_type=aggregated["counterparty_type"],
        subject=anchor_message.subject,
        product=aggregated["product"],
        requirement=aggregated["requirement"],
        quantity=aggregated["quantity"],
        brands=brands_seen,
        is_priority=is_priority,
        priority_evidence=priority_evidence,
        received_at=anchor_message.received_at,
        last_activity_at=relevant[-1].received_at,
        closed_at=closure_message.received_at if is_closed else None,
        closure_evidence=closure_verdict.closure_evidence if is_closed else None,
        closure_kind=closure_verdict.closure_kind if is_closed else None,
    )
