"""Business rules for enquiry status transitions.

Claude only classifies *what a message means* (see app.ai.claude); this
module is where that gets turned into an actual status change, plus the
deterministic sender classification the rules depend on. Kept as pure
functions (data in, status out) so the rules are easy to read, test,
and audit independently of Claude, Gmail, or SQLite.

Design decisions beyond the literal project spec (the spec leaves a few
cases open -- documenting the choices made here):
  - REJECTED closes the enquiry, same as ACCEPTED -- the outcome is
    known either way, there's nothing left to track.
  - GENERAL_REPLY never closes (project spec section 16: "Do not
    automatically set CLOSED because a customer says 'thanks'") -- it
    becomes CUSTOMER_REPLIED like any other non-terminal customer reply.
  - An employee message never closes an enquiry on its own, regardless
    of what Claude's intent classification says (project spec section
    16) -- only a customer's ACCEPTED/REJECTED can close it.
  - QUOTATION_SENT is detected deterministically (keyword match on an
    employee's own message), not via Claude -- consistent with "don't
    use AI for what's deterministic" (project spec section 17).
  - Sender type OTHER (e.g. cc'd third parties, delivery-failure
    bounces) never changes status -- too ambiguous to act on
    automatically.
  - CLOSED is terminal for V1: a new message on an already-closed
    thread does not automatically reopen it.
"""

from __future__ import annotations

from email.utils import parseaddr
from typing import Optional

from app.enquiry.models import LastSender, Status

# Deliberately simple keyword match -- good enough to distinguish "here
# is your quote" from an ordinary reply, without spending a Claude call
# on it (project spec section 17: don't use AI for deterministic work).
_QUOTATION_KEYWORDS = (
    "quotation",
    "quote attached",
    "please find our quote",
    "our quote",
    "price quote",
    "attached quotation",
)

# Customer intent -> status, applied only when the enquiry isn't
# already CLOSED and the message is from the customer.
_CUSTOMER_INTENT_STATUS = {
    "ACCEPTED": Status.CLOSED,
    "REJECTED": Status.CLOSED,
    "NEEDS_INFORMATION": Status.CUSTOMER_REPLIED,
    "NEGOTIATING": Status.CUSTOMER_REPLIED,
    "FOLLOW_UP": Status.CUSTOMER_REPLIED,
    "GENERAL_REPLY": Status.CUSTOMER_REPLIED,
}


def extract_email_address(header_value: str) -> Optional[str]:
    """Pull the bare address out of a header value like
    '"Sales" <sales@company.com>'. Returns None if there isn't one --
    parseaddr() can return a bare non-address word (e.g. the first word
    of a sentence) when given text with no real address, so this
    requires an "@" before trusting the result.
    """
    _, address = parseaddr(header_value or "")
    return address if address and "@" in address else None


def classify_sender(from_header: str, company_domain: str) -> str:
    """customer vs employee vs other, based on the sender's email
    domain vs. the company's own domain (COMPANY_EMAIL_DOMAIN).

    An "employee" message is one sent from the company's own domain
    (e.g. sales@<company_domain> replying on the shared enquiry
    thread) -- matches what a real reply from the mailbox looks like.
    """
    address = extract_email_address(from_header)
    if not address or "@" not in address:
        return LastSender.OTHER

    domain = address.rsplit("@", 1)[-1].lower()
    if company_domain and domain == company_domain.strip().lower():
        return LastSender.EMPLOYEE
    return LastSender.CUSTOMER


def is_quotation_message(body: str) -> bool:
    """True if an employee's message looks like it's sending a
    quotation. See module docstring for why this is a keyword check
    rather than a Claude call.
    """
    lowered = (body or "").lower()
    return any(keyword in lowered for keyword in _QUOTATION_KEYWORDS)


def next_status_for_message(
    *,
    sender_type: str,
    intent: Optional[str],
    message_body: str,
    current_status: str,
) -> str:
    """Decide the new `status` for an enquiry given its latest message.

    `intent` is Claude's classification of the message (see
    app.ai.claude.classify_reply_intent) -- may be None if the Claude
    call failed, in which case the status is left unchanged (fail
    safe: an unclassifiable message should not silently close or
    reopen anything).
    """
    if current_status == Status.CLOSED:
        return current_status

    if sender_type == LastSender.EMPLOYEE:
        return Status.QUOTATION_SENT if is_quotation_message(message_body) else Status.REPLIED

    if sender_type == LastSender.CUSTOMER:
        if intent is None:
            return current_status
        return _CUSTOMER_INTENT_STATUS.get(intent, current_status)

    # OTHER: too ambiguous to act on -- leave status unchanged.
    return current_status
