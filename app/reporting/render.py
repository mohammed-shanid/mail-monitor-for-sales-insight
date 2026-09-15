"""Structured metrics -> report text (SPEC.md §21.3), added Stage 4.

Channel-agnostic plain text -- chunking for Telegram's message limit
happens in the delivery layer (app.telegram.client), never here
(SPEC.md §4.3, §16.2). All timezone conversion for display happens
here and in app.timewindow -- nowhere else (SPEC.md §4.3).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from zoneinfo import ZoneInfo

from app.enquiry.metrics import Metrics, PendingItem, PriorityItem
from app.timewindow import Window

_SEP = "━━━━━━━━━━━━━━━━━━"
_UTC = ZoneInfo("UTC")


def _to_local(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=_UTC).astimezone(tz)


def _format_duration(ms: int) -> str:
    """"5h 18m" / "3d 4h" / "12m" -- SPEC.md §21.3's worked example
    style. Never negative (a late-arriving clock skew clamps to 0m).
    """
    total_minutes = max(ms // 60_000, 0)
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _format_header(window: Window, tz: ZoneInfo) -> List[str]:
    start_dt = _to_local(window.start_ms, tz)
    end_dt = _to_local(window.end_ms, tz)
    tz_abbr = start_dt.strftime("%Z")

    if start_dt.date() == end_dt.date():
        date_line = f"{start_dt.day} {start_dt.strftime('%B %Y')}"
    else:
        date_line = f"{start_dt.day} {start_dt.strftime('%B %Y')} → {end_dt.day} {end_dt.strftime('%B %Y')}"

    return [
        "📊 REGENCY ENQUIRY REPORT",
        f"📅 {date_line}",
        f"⏰ {start_dt:%H:%M} {tz_abbr} → {end_dt:%H:%M} {tz_abbr}",
    ]


def _format_summary_section(metrics: Metrics) -> List[str]:
    # Always renders (SPEC.md §21.3 rule), even at zero (SPEC.md §17:
    # "Empty window | Not an error. Send a valid report showing zeros").
    return [
        _SEP,
        "📩 ENQUIRY SUMMARY",
        _SEP,
        "",
        f"Received: {metrics.received}",
        f"Attended: {metrics.attended}",
        f"Pending: {metrics.pending}",
        f"Closed: {metrics.closed}  (includes enquiries received earlier)",
    ]


def _format_quotation_section(metrics: Metrics) -> List[str]:
    values = (
        metrics.rfq_received, metrics.quotations_sent,
        metrics.quotation_responses, metrics.supplier_quotations_received,
    )
    if not any(values):
        return []
    return [
        "",
        _SEP,
        "📋 QUOTATION ACTIVITY",
        _SEP,
        "",
        f"RFQ / quotation requests received: {metrics.rfq_received}",
        f"Quotations sent: {metrics.quotations_sent}",
        f"Quotation responses: {metrics.quotation_responses}",
        f"Supplier quotations received: {metrics.supplier_quotations_received}",
    ]


def _format_brands_section(metrics: Metrics) -> List[str]:
    if not metrics.brand_counts:
        return []
    lines = ["", _SEP, "🏷️ TOP BRANDS", _SEP, ""]
    for i, bc in enumerate(metrics.brand_counts, start=1):
        lines.append(f"{i}. {bc.brand} — {bc.count}")
    lines.append("")
    lines.append("Multi-brand enquiries are counted under each brand.")
    return lines


def _format_pending_detail_line(item: PendingItem) -> str:
    segments = []
    if item.product and item.product != "Not specified":
        segments.append(item.product)
    if item.brands:
        segments.append(", ".join(item.brands))
    if item.quantity:
        segments.append(item.quantity)
    return " | ".join(segments) if segments else "Details not specified"


def _format_pending_section(metrics: Metrics, tz: ZoneInfo, display_limit: int) -> List[str]:
    if not metrics.pending_items:
        return []
    lines = ["", _SEP, "⚠️ PENDING ENQUIRIES", _SEP, ""]
    shown = metrics.pending_items[:display_limit]
    for i, item in enumerate(shown, start=1):
        received_dt = _to_local(item.received_at_ms, tz)
        lines.append(f"{i}. {item.customer_name}")
        lines.append(f"   {_format_pending_detail_line(item)}")
        lines.append(f"   Received: {received_dt.strftime('%-I:%M %p')}")
        lines.append(f"   Waiting: {_format_duration(item.waiting_ms)}")
        lines.append("")
    remaining = len(metrics.pending_items) - len(shown)
    if remaining > 0:
        lines.append(f"…and {remaining} more")
        lines.append("")
    return lines[:-1] if lines and lines[-1] == "" else lines


def _format_priority_section(metrics: Metrics, display_limit: int) -> List[str]:
    if not metrics.priority_items:
        return []
    lines = ["", _SEP, "🔥 IMPORTANT", _SEP, ""]
    shown = metrics.priority_items[:display_limit]
    for i, item in enumerate(shown, start=1):
        lines.append(f"{i}. {item.customer_name} — customer states requirement is urgent")
        lines.append(f'   "{item.evidence}"')
    remaining = len(metrics.priority_items) - len(shown)
    if remaining > 0:
        lines.append(f"…and {remaining} more")
    return lines


def _format_caveat_section(metrics: Metrics) -> List[str]:
    if metrics.messages_unanalyzed <= 0:
        return []
    return [
        "",
        f"⚠️ {metrics.messages_unanalyzed} message(s) could not be analysed and are not reflected above.",
    ]


def _format_summary_prose_section(summary_text: str) -> List[str]:
    return ["", _SEP, "🧠 MANAGEMENT SUMMARY", _SEP, "", summary_text]


def render_report(
    metrics: Metrics,
    window: Window,
    tz: ZoneInfo,
    summary_text: str,
    *,
    pending_display_limit: int = 10,
    priority_display_limit: int = 5,
) -> str:
    """SPEC.md §21.3: channel-agnostic report text. A section with no
    data is omitted rather than shown empty, except ENQUIRY SUMMARY,
    which always renders (even at all zeros).
    """
    lines: List[str] = []
    lines.extend(_format_header(window, tz))
    lines.append("")
    lines.extend(_format_summary_section(metrics))
    lines.extend(_format_quotation_section(metrics))
    lines.extend(_format_brands_section(metrics))
    lines.extend(_format_pending_section(metrics, tz, pending_display_limit))
    lines.extend(_format_priority_section(metrics, priority_display_limit))
    lines.extend(_format_caveat_section(metrics))
    lines.extend(_format_summary_prose_section(summary_text))

    return "\n".join(lines).rstrip() + "\n"
