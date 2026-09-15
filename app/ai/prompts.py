"""Versioned prompt constants for the report path (SPEC.md §15.2),
added Stage 3.

Every verdict is cached keyed on `(gmail_message_id, prompt_version)`
(SPEC.md §14.2, §15.2, §18.2) -- bumping a version constant here
naturally invalidates the cache for every message, without needing a
migration; `--reprocess` bypasses the cache regardless of version.
"""

from __future__ import annotations

MESSAGE_VERDICT_PROMPT_VERSION = "message_verdict_v1"

MESSAGE_VERDICT_SYSTEM_PROMPT = """\
You classify ONE message ("the target message") from an ongoing email thread \
between a company's sales mailbox and a counterparty. You are given the \
target message plus, for context only, the thread's earlier messages \
(oldest first). Judge only the target message; the context exists solely so \
you understand what it is replying to.

A genuine enquiry is a product enquiry, requirement, RFQ, quotation or \
pricing request, availability check, quantity requirement, or purchase-\
related request. Never an enquiry: spam, newsletters, OTPs and verification \
codes, automated notifications, delivery/no-reply system mail, marketing \
blasts, personal correspondence, internal administration, calendar invites, \
out-of-office replies.

The counterparty is either a customer (buying) or a supplier (selling to \
the company, e.g. sending their own quotation or price list) -- never guess \
between the two; return "unknown" if the thread content does not make it \
clear. Never classify a counterparty as "internal" -- that determination is \
made separately, outside this call, from the sender's email domain.

quotation_signal is exactly one of: "rfq_received" (the target message is \
an inbound request for a quote/price/rate), "quotation_sent" (an outbound \
message containing or attaching a quotation), "quotation_response" (an \
inbound reaction to a previously received quotation -- accept, negotiate, \
query, or reject), "supplier_quotation" (an inbound quotation from a \
supplier), or null if the target message is none of these.

closure_signal is true only when the target message itself contains \
positive evidence that the deal is settled: the counterparty accepts a \
quotation, confirms an order or issues a PO, explicitly instructs to \
proceed, explicitly withdraws or rejects the requirement, states it is no \
longer needed, or the employee explicitly records closure or order \
booking. An ordinary reply, a quotation being sent, the counterparty saying \
"thanks", or "we will check and get back to you" are all explicitly NOT \
closure -- closure_signal must be false for these. When closure_signal is \
true, set closure_kind to exactly one of "won" (order confirmed/proceeding), \
"lost" (rejected/declined), "withdrawn" (counterparty says no longer \
needed), or "unknown" if the kind of closure is unclear; when \
closure_signal is false, closure_kind must be null. closure_evidence is a \
short quote or paraphrase from the target message supporting the signal, \
or null.

urgency is true only when the target message itself states or clearly \
implies a time-sensitive need (a stated deadline, "urgent", "ASAP", "need \
this today/by <date>"). urgency_evidence is a short quote or paraphrase \
supporting it, or null.

brands is a list of manufacturer/brand names mentioned in the target \
message (e.g. "Schneider", "Siemens", "ABB", "L&T", "Legrand") -- extract \
exactly what is written; do not normalise spelling or correct it, that \
happens later. An empty list if none are mentioned.

quantity is copied as written (e.g. "20 nos", "2 lots", "250 units") -- \
never converted to a bare number, never invented if not stated.

Every field is nullable. Return null for anything you cannot determine \
confidently from the target message (with the thread context) -- null is \
always preferable to a guess. Never invent a customer name, company, \
product, quantity, or any other field not actually present in the text.
"""

def _nullable_enum(values):
    """A nullable, enum-constrained string field. NOT `{"type":
    ["string","null"], "enum": [...values..., None]}` -- a live run
    against the real API rejected that combination outright (HTTP 400:
    "Enum value 'customer' does not match declared type"). Anthropic's
    structured-output schema validator does not accept an array `type`
    combined with a mixed-type `enum`; `anyOf` with a separate null
    branch is the portable form that works.
    """
    return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}


MESSAGE_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_enquiry": {"type": ["boolean", "null"]},
        "counterparty_type": _nullable_enum(["customer", "supplier", "unknown"]),
        "customer_name": {"type": ["string", "null"]},
        "company": {"type": ["string", "null"]},
        "product": {"type": ["string", "null"]},
        "requirement": {"type": ["string", "null"]},
        "quantity": {"type": ["string", "null"]},
        "brands": {"type": "array", "items": {"type": "string"}},
        "quotation_signal": _nullable_enum(
            ["rfq_received", "quotation_sent", "quotation_response", "supplier_quotation"]
        ),
        "closure_signal": {"type": ["boolean", "null"]},
        "closure_evidence": {"type": ["string", "null"]},
        "closure_kind": _nullable_enum(["won", "lost", "withdrawn", "unknown"]),
        "urgency": {"type": ["boolean", "null"]},
        "urgency_evidence": {"type": ["string", "null"]},
        "confidence": _nullable_enum(["high", "medium", "low"]),
    },
    "required": [
        "is_enquiry", "counterparty_type", "customer_name", "company", "product",
        "requirement", "quantity", "brands", "quotation_signal", "closure_signal",
        "closure_evidence", "closure_kind", "urgency", "urgency_evidence", "confidence",
    ],
    "additionalProperties": False,
}


SUMMARY_PROMPT_VERSION = "summary_v1"

SUMMARY_SYSTEM_PROMPT = """\
You write a short management summary for a non-technical founder, from \
ONLY the structured metrics object given to you below -- you are never \
shown any raw email. Write 2-4 short sentences of plain business English: \
no greetings, no headers, no bullet points. Use ONLY numbers that already \
appear in the metrics object; never calculate, estimate, or invent a \
number that is not already present in your input. If a metric is absent \
from the input, do not mention it or guess at it.
"""
