"""Claude-generated prose summary, numeral-guarded (SPEC.md §15.4,
amended by PHASE0_DECISIONS.md Q8), added Stage 4.

The summary call receives ONLY the computed metrics payload -- never
raw email bodies (SPEC.md §15.1, §15.4). A post-generation guard
rejects any summary containing a number Python did not already
compute; on rejection (or any API failure), a deterministic template
built only from the same metrics is used instead. An LLM that invents
a number must never reach the founder (SPEC.md Appendix A #9).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Set
from zoneinfo import ZoneInfo

from app.ai.claude import generate_summary
from app.enquiry.metrics import Metrics
from app.timewindow import Window

logger = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")
_INTEGER_RE = re.compile(r"\d+")


def _metrics_payload(metrics: Metrics) -> Dict[str, Any]:
    """The exact JSON-able object sent to Claude."""
    return {
        "received": metrics.received,
        "attended": metrics.attended,
        "pending": metrics.pending,
        "closed": metrics.closed,
        "rfq_received": metrics.rfq_received,
        "quotations_sent": metrics.quotations_sent,
        "quotation_responses": metrics.quotation_responses,
        "supplier_quotations_received": metrics.supplier_quotations_received,
        "brands": [{"brand": bc.brand, "count": bc.count} for bc in metrics.brand_counts],
        "pending_count": len(metrics.pending_items),
        "priority_count": len(metrics.priority_items),
    }


def _extract_integers(text: str) -> List[int]:
    return [int(match) for match in _INTEGER_RE.findall(text)]


def _collect_integers(value: Any, into: Set[int]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int):
        into.add(value)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_integers(v, into)
    elif isinstance(value, list):
        for v in value:
            _collect_integers(v, into)


def _allowed_integers(payload: dict, window: Window, tz: ZoneInfo) -> Set[int]:
    """Every integer in the metrics payload, plus the window's day/
    month/year and each bound's hour/minute (PHASE0_DECISIONS.md Q8) --
    a summary is allowed to say "13 September" or "as of 23:59" without
    that being treated as a fabricated number.
    """
    allowed: Set[int] = set()
    _collect_integers(payload, allowed)

    for ms in (window.start_ms, window.end_ms):
        dt = datetime.fromtimestamp(ms / 1000, tz=_UTC).astimezone(tz)
        allowed.update({dt.day, dt.month, dt.year, dt.hour, dt.minute})

    return allowed


def _template_summary(metrics: Metrics) -> str:
    """A deterministic fallback built only from numbers Python already
    computed -- used when Claude fails or its summary fails the
    numeral guard.
    """
    sentence = f"{metrics.received} enquiries were received"
    if metrics.received:
        sentence += f", {metrics.attended} attended and {metrics.pending} still pending"
    text = sentence + "."
    if metrics.closed:
        text += f" {metrics.closed} enquiries were closed during this period."
    if metrics.priority_items:
        text += f" {len(metrics.priority_items)} enquiry(ies) are flagged urgent."
    return text


def generate_summary_text(metrics: Metrics, window: Window, tz: ZoneInfo) -> str:
    """SPEC.md §15.4 end to end: call Claude with only the metrics
    payload, validate every numeral it returns against the allowed
    set, and fall back to a deterministic template on any failure or
    guard violation. A summary containing zero numerals is valid.
    """
    payload = _metrics_payload(metrics)
    raw = generate_summary(payload)

    if raw is not None:
        allowed = _allowed_integers(payload, window, tz)
        numerals = _extract_integers(raw)
        if all(n in allowed for n in numerals):
            return raw
        logger.error(
            "Summary contained an unsupported numeral (found %s, allowed %s); using template fallback",
            numerals, sorted(allowed),
        )

    return _template_summary(metrics)
