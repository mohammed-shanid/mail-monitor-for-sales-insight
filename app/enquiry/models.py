"""Data-model constants and dataclasses shared by the database and
enquiry-processing layers.

Kept deliberately small: two closed sets of string constants (project
spec sections 14 & 15) plus dataclasses for passing rows around with
real attributes instead of raw sqlite3.Row/dict access scattered
everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


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
