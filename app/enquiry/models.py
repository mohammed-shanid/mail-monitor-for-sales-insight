"""Data-model constants and dataclasses shared by the database and
enquiry-processing layers.

Kept deliberately small: two closed sets of string constants (project
spec sections 14 & 15) plus dataclasses for passing rows around with
real attributes instead of raw sqlite3.Row/dict access scattered
everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


class Status:
    """Enquiry lifecycle status -- a small, closed set (project spec
    section 15). Python (app/enquiry/status.py, Phase 4) decides
    transitions; Claude only classifies intent, never a status
    directly. IGNORED is for messages Claude determined are not
    genuine enquiries.
    """

    NEW = "NEW"
    REPLIED = "REPLIED"
    QUOTATION_SENT = "QUOTATION_SENT"
    CUSTOMER_REPLIED = "CUSTOMER_REPLIED"
    CLOSED = "CLOSED"
    IGNORED = "IGNORED"

    ALL = {NEW, REPLIED, QUOTATION_SENT, CUSTOMER_REPLIED, CLOSED, IGNORED}


class LastSender:
    """Who sent the most recent message on an enquiry thread.

    Tracked separately from `status` -- project spec sections 14/21:
    an enquiry is "waiting on us" when last_sender == CUSTOMER,
    regardless of what `status` says.
    """

    CUSTOMER = "customer"
    EMPLOYEE = "employee"
    OTHER = "other"

    ALL = {CUSTOMER, EMPLOYEE, OTHER}


@dataclass
class Email:
    """One row of the `emails` table."""

    id: Optional[int]
    gmail_message_id: str
    gmail_thread_id: str
    sender: str
    recipient: str
    subject: str
    body: str
    received_at: Optional[str]
    processed: bool


@dataclass
class Enquiry:
    """One row of the `enquiries` table -- one Gmail thread."""

    id: Optional[int]
    gmail_thread_id: str
    customer_name: Optional[str]
    customer_email: Optional[str]
    subject: str
    product: Optional[str]
    quantity: Optional[int]
    status: str
    last_sender: str
    received_at: str
    last_activity_at: str
    closed_at: Optional[str]


# =============================================================================
# Report path (SPEC.md), added Stage 2. The bot classes/dataclasses above are
# untouched; these are new, separate names for the report schema (§14.2),
# which uses a different status vocabulary and epoch-ms timestamps.
# =============================================================================


class Direction:
    """A single message's direction (SPEC.md §6.1) -- derived
    deterministically from the sender's address, never by AI.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"

    ALL = {INBOUND, OUTBOUND}


class AddressClass:
    """Where a message's sender/recipient address falls (SPEC.md §0, §6.1,
    §6.2): the mailbox owner or an alias, an internal-domain colleague, or
    everyone else. Distinct from `Direction` -- an internal colleague is
    not the employee, but is also not a counterparty.
    """

    EMPLOYEE = "employee"
    INTERNAL = "internal"
    EXTERNAL = "external"

    ALL = {EMPLOYEE, INTERNAL, EXTERNAL}


class ReportStatus:
    """Enquiry as-of status for the report path (SPEC.md §8.3) -- three
    states only. Distinct from the bot's `Status` above (different
    vocabulary entirely; see PHASE0_DECISIONS.md Q5).
    """

    PENDING = "pending"
    ATTENDED = "attended"
    CLOSED = "closed"

    ALL = {PENDING, ATTENDED, CLOSED}


@dataclass
class ReportEmail:
    """One row of the report path's `emails` table (SPEC.md §14.2) --
    epoch-ms timestamps, sender_domain/direction/is_auto_reply/
    has_attachments captured at ingestion. Distinct from the bot's
    `Email` dataclass above (different columns, different table
    instance -- see app.database.db.get_report_connection).
    """

    id: Optional[int]
    gmail_message_id: str
    gmail_thread_id: str
    sender: str
    sender_domain: Optional[str]
    recipient: Optional[str]
    subject: Optional[str]
    body: Optional[str]
    received_at: int  # epoch ms UTC, from internalDate
    direction: str  # inbound | outbound
    is_auto_reply: bool
    has_attachments: bool
    ingested_at: int
    processed: bool


class QuotationSignal:
    """SPEC.md §9, §15.3 -- the four quotation concepts, as a single
    per-message Claude signal. Python counts; Claude only ever picks
    one of these per message (or null).
    """

    RFQ_RECEIVED = "rfq_received"
    QUOTATION_SENT = "quotation_sent"
    QUOTATION_RESPONSE = "quotation_response"
    SUPPLIER_QUOTATION = "supplier_quotation"

    ALL = {RFQ_RECEIVED, QUOTATION_SENT, QUOTATION_RESPONSE, SUPPLIER_QUOTATION}


class ClosureKind:
    """SPEC.md §8.2, §15.3 -- set only when closure_signal is true."""

    WON = "won"
    LOST = "lost"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"

    ALL = {WON, LOST, WITHDRAWN, UNKNOWN}


class CounterpartyType:
    """SPEC.md §6.2. `internal` is never actually returned by Claude --
    it is decided deterministically from AddressClass before Claude is
    ever called (a thread whose sender is AddressClass.INTERNAL skips
    classification entirely). Kept here anyway as a documented,
    recognised value distinct from the ones Claude actually returns
    (customer | supplier | unknown).
    """

    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    INTERNAL = "internal"
    UNKNOWN = "unknown"

    ALL = {CUSTOMER, SUPPLIER, INTERNAL, UNKNOWN}


@dataclass
class Verdict:
    """One message's Claude classification (SPEC.md §15.3), added
    Stage 3. Every field is nullable -- `null` is always preferable to
    a guess (SPEC.md §15.3); a field the model is unsure about comes
    back None and renders as absent, never a plausible invention.
    """

    is_enquiry: Optional[bool]
    counterparty_type: Optional[str]
    customer_name: Optional[str]
    company: Optional[str]
    product: Optional[str]
    requirement: Optional[str]
    quantity: Optional[str]  # TEXT: "250 nos", "2 lots" are real inputs
    brands: List[str]
    quotation_signal: Optional[str]
    closure_signal: Optional[bool]
    closure_evidence: Optional[str]
    closure_kind: Optional[str]
    urgency: Optional[bool]
    urgency_evidence: Optional[str]
    confidence: Optional[str]


@dataclass
class EnquiryState:
    """The computed as-of state of one genuine enquiry thread (SPEC.md
    §8), added Stage 3. A pure function of the thread's messages +
    verdicts + an `as_of` cutoff (app.enquiry.thread_state.
    compute_state) -- never mutated, always recomputed from scratch
    (SPEC.md §8.1). `app.enquiry.thread_state.compute_state` returns
    None instead of this when the thread is not a genuine enquiry as
    of the cutoff (internal, supplier, outbound-first with no inbound
    demand yet, or never confidently judged is_enquiry=True).
    """

    gmail_thread_id: str
    status: str  # ReportStatus: pending | attended | closed
    last_sender: str  # LastSender: customer | employee
    customer_name: Optional[str]
    company: Optional[str]
    customer_email: Optional[str]
    counterparty_type: Optional[str]
    subject: Optional[str]
    product: Optional[str]
    requirement: Optional[str]
    quantity: Optional[str]
    brands: List[str]
    is_priority: bool
    priority_evidence: Optional[str]
    received_at: int  # the anchor message's received_at (epoch ms)
    last_activity_at: int
    closed_at: Optional[int]
    closure_evidence: Optional[str]
    closure_kind: Optional[str]
