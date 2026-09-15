"""Verdict cache read/write (SPEC.md §15.2, §18.2), added Stage 3.

Thin wrapper over app.database.queries' ai_verdicts functions: JSON
serialise/deserialise plus conversion to/from the Verdict dataclass.
Re-runs read from cache; `--reprocess` bypasses it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from app.database.queries import get_ai_verdict_row, upsert_ai_verdict
from app.enquiry.models import Verdict

_VERDICT_FIELDS = (
    "is_enquiry", "counterparty_type", "customer_name", "company", "product",
    "requirement", "quantity", "brands", "quotation_signal", "closure_signal",
    "closure_evidence", "closure_kind", "urgency", "urgency_evidence", "confidence",
)


def _verdict_from_dict(data: dict) -> Verdict:
    return Verdict(
        is_enquiry=data.get("is_enquiry"),
        counterparty_type=data.get("counterparty_type"),
        customer_name=data.get("customer_name"),
        company=data.get("company"),
        product=data.get("product"),
        requirement=data.get("requirement"),
        quantity=data.get("quantity"),
        brands=list(data.get("brands") or []),
        quotation_signal=data.get("quotation_signal"),
        closure_signal=data.get("closure_signal"),
        closure_evidence=data.get("closure_evidence"),
        closure_kind=data.get("closure_kind"),
        urgency=data.get("urgency"),
        urgency_evidence=data.get("urgency_evidence"),
        confidence=data.get("confidence"),
    )


def _verdict_to_dict(verdict: Verdict) -> dict:
    return {
        "is_enquiry": verdict.is_enquiry,
        "counterparty_type": verdict.counterparty_type,
        "customer_name": verdict.customer_name,
        "company": verdict.company,
        "product": verdict.product,
        "requirement": verdict.requirement,
        "quantity": verdict.quantity,
        "brands": list(verdict.brands),
        "quotation_signal": verdict.quotation_signal,
        "closure_signal": verdict.closure_signal,
        "closure_evidence": verdict.closure_evidence,
        "closure_kind": verdict.closure_kind,
        "urgency": verdict.urgency,
        "urgency_evidence": verdict.urgency_evidence,
        "confidence": verdict.confidence,
    }


def get_cached_verdict(
    conn: sqlite3.Connection,
    gmail_message_id: str,
    prompt_version: str,
    *,
    reprocess: bool = False,
) -> Optional[Verdict]:
    """A cached verdict, or None on a cache miss, a corrupt cache row,
    OR when `reprocess` is True (SPEC.md §13.2 `--reprocess`: "Ignore
    the AI verdict cache, re-analyse all messages in window").
    """
    if reprocess:
        return None

    row = get_ai_verdict_row(conn, gmail_message_id, prompt_version)
    if row is None:
        return None

    try:
        data = json.loads(row["verdict_json"])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    return _verdict_from_dict(data)


def store_verdict(
    conn: sqlite3.Connection,
    *,
    gmail_message_id: str,
    prompt_version: str,
    model: str,
    verdict: Verdict,
    created_at: int,
) -> None:
    """Cache one message's verdict, keyed on (gmail_message_id,
    prompt_version) -- the same message is never paid for twice under
    the same prompt version (SPEC.md §18.2).
    """
    upsert_ai_verdict(
        conn,
        gmail_message_id=gmail_message_id,
        prompt_version=prompt_version,
        model=model,
        verdict_json=json.dumps(_verdict_to_dict(verdict)),
        created_at=created_at,
    )
