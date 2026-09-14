"""Unit tests for app.enquiry.status -- pure business-rule functions,
no Gmail/Claude/SQLite involved.
"""

from __future__ import annotations

import pytest

from app.enquiry.status import (
    classify_sender,
    extract_email_address,
    is_quotation_message,
    next_status_for_message,
)
from app.enquiry.models import LastSender, Status


# --- extract_email_address / classify_sender -------------------------------


@pytest.mark.parametrize(
    "header, expected",
    [
        ("Sales <sales@regencyelectricals.com>", "sales@regencyelectricals.com"),
        ("purchase@abc.com", "purchase@abc.com"),
        ("", None),
        ("Not an email at all", None),
    ],
)
def test_extract_email_address(header, expected):
    assert extract_email_address(header) == expected


def test_classify_sender_matches_company_domain_as_employee():
    result = classify_sender("Sales <sales@regencyelectricals.com>", "regencyelectricals.com")
    assert result == LastSender.EMPLOYEE


def test_classify_sender_is_case_insensitive():
    result = classify_sender("Sales <SALES@RegencyElectricals.COM>", "regencyelectricals.com")
    assert result == LastSender.EMPLOYEE


def test_classify_sender_other_domain_is_customer():
    result = classify_sender("ABC Industries <purchase@abc.com>", "regencyelectricals.com")
    assert result == LastSender.CUSTOMER


def test_classify_sender_unparseable_address_is_other():
    assert classify_sender("not an email", "regencyelectricals.com") == LastSender.OTHER
    assert classify_sender("", "regencyelectricals.com") == LastSender.OTHER


def test_classify_sender_treats_everyone_as_customer_when_domain_unset():
    # COMPANY_EMAIL_DOMAIN missing/blank -- fail safe, not fail guessing.
    result = classify_sender("Sales <sales@regencyelectricals.com>", "")
    assert result == LastSender.CUSTOMER


# --- is_quotation_message ---------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Please find our quote attached for your requirement.",
        "PFA the quotation for MCCB 250A.",
        "Our Price Quote for the items requested is enclosed.",
    ],
)
def test_is_quotation_message_true_cases(body):
    assert is_quotation_message(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "Thanks for your order, we've received it.",
        "Could you confirm the delivery address?",
        "",
    ],
)
def test_is_quotation_message_false_cases(body):
    assert is_quotation_message(body) is False


# --- next_status_for_message -------------------------------------------------


def test_closed_is_terminal_regardless_of_new_message():
    result = next_status_for_message(
        sender_type=LastSender.CUSTOMER,
        intent="FOLLOW_UP",
        message_body="just checking in",
        current_status=Status.CLOSED,
    )
    assert result == Status.CLOSED


def test_employee_message_without_quotation_keywords_is_replied():
    result = next_status_for_message(
        sender_type=LastSender.EMPLOYEE,
        intent=None,
        message_body="Thanks for your order, we've received it.",
        current_status=Status.NEW,
    )
    assert result == Status.REPLIED


def test_employee_message_with_quotation_keywords_is_quotation_sent():
    result = next_status_for_message(
        sender_type=LastSender.EMPLOYEE,
        intent=None,
        message_body="Please find our quote attached.",
        current_status=Status.NEW,
    )
    assert result == Status.QUOTATION_SENT


def test_employee_message_never_closes_even_with_accepted_intent():
    # project spec section 16: employee messages should never
    # automatically close an enquiry, regardless of intent.
    result = next_status_for_message(
        sender_type=LastSender.EMPLOYEE,
        intent="ACCEPTED",
        message_body="Confirmed, order placed.",
        current_status=Status.NEW,
    )
    assert result != Status.CLOSED


@pytest.mark.parametrize(
    "intent, expected",
    [
        ("ACCEPTED", Status.CLOSED),
        ("REJECTED", Status.CLOSED),
        ("NEEDS_INFORMATION", Status.CUSTOMER_REPLIED),
        ("NEGOTIATING", Status.CUSTOMER_REPLIED),
        ("FOLLOW_UP", Status.CUSTOMER_REPLIED),
        ("GENERAL_REPLY", Status.CUSTOMER_REPLIED),
    ],
)
def test_customer_intent_maps_to_expected_status(intent, expected):
    result = next_status_for_message(
        sender_type=LastSender.CUSTOMER,
        intent=intent,
        message_body="irrelevant",
        current_status=Status.NEW,
    )
    assert result == expected


def test_customer_general_reply_does_not_close_on_thanks():
    # project spec section 16: "thanks" must not trigger CLOSED.
    result = next_status_for_message(
        sender_type=LastSender.CUSTOMER,
        intent="GENERAL_REPLY",
        message_body="Thanks!",
        current_status=Status.QUOTATION_SENT,
    )
    assert result == Status.CUSTOMER_REPLIED


def test_customer_message_with_failed_classification_leaves_status_unchanged():
    result = next_status_for_message(
        sender_type=LastSender.CUSTOMER,
        intent=None,
        message_body="irrelevant",
        current_status=Status.QUOTATION_SENT,
    )
    assert result == Status.QUOTATION_SENT


def test_other_sender_never_changes_status():
    result = next_status_for_message(
        sender_type=LastSender.OTHER,
        intent="ACCEPTED",
        message_body="irrelevant",
        current_status=Status.NEW,
    )
    assert result == Status.NEW


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
