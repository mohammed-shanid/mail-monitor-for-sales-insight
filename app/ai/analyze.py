"""Per-window Claude analysis orchestration (SPEC.md §13.4, §15),
added Stage 3.

    for each touched thread:
        for each message (oldest first, as context accumulates):
            internal-domain sender?          -> skip, never call Claude
            cached verdict (unless --reprocess)? -> use it
            else                              -> classify_message(), cache it
        compute_state() from the thread's verdicts

Ties app.ai.cache + app.ai.claude + app.enquiry.thread_state together;
report.py calls only this module for the "Analyzing enquiry data" /
"Calculating metrics" stages, never app.ai.claude directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

from app.ai.cache import get_cached_verdict, store_verdict
from app.ai.claude import classify_message
from app.ai.prompts import MESSAGE_VERDICT_PROMPT_VERSION
from app.database.queries import get_report_emails_by_thread
from app.enquiry.models import AddressClass, EnquiryState, ReportEmail, Verdict
from app.enquiry.status import classify_address
from app.enquiry.thread_state import compute_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisResult:
    states: Dict[str, EnquiryState]  # gmail_thread_id -> EnquiryState (only genuine enquiries)
    cached_count: int  # verdicts served from cache
    analyzed_count: int  # Claude calls actually made this run
    failed_message_ids: List[str] = field(default_factory=list)
    # Every touched thread's messages/verdicts, flattened by message id --
    # Stage 4's metrics need per-MESSAGE data (quotation signals are
    # counted per message inside the window, SPEC.md §11.2), not just
    # the per-thread EnquiryState above.
    messages: Dict[str, ReportEmail] = field(default_factory=dict)
    message_verdicts: Dict[str, Verdict] = field(default_factory=dict)


def analyze_thread_messages(
    conn,
    messages: List[ReportEmail],
    *,
    mailbox: str,
    employee_aliases: List[str],
    internal_domains: List[str],
    model: str,
    max_retries: int,
    reprocess: bool,
    now_ms: int,
) -> Dict[str, Verdict]:
    """Verdicts for one thread's full hydrated message list (oldest
    first). A message from an internal-domain sender is never sent to
    Claude at all (SPEC.md §6.2) and has no entry in the result.
    """
    verdicts: Dict[str, Verdict] = {}
    context: List[dict] = []

    for message in messages:
        sender_class = classify_address(
            message.sender, mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains
        )
        if sender_class == AddressClass.INTERNAL:
            continue

        cached = get_cached_verdict(
            conn, message.gmail_message_id, MESSAGE_VERDICT_PROMPT_VERSION, reprocess=reprocess
        )
        if cached is not None:
            verdicts[message.gmail_message_id] = cached
            context.append({"sender": message.sender, "body": message.body or ""})
            continue

        verdict = classify_message(
            target_subject=message.subject or "",
            target_body=message.body or "",
            context_messages=context,
            model=model,
            max_retries=max_retries,
        )
        if verdict is not None:
            verdicts[message.gmail_message_id] = verdict
            store_verdict(
                conn,
                gmail_message_id=message.gmail_message_id,
                prompt_version=MESSAGE_VERDICT_PROMPT_VERSION,
                model=model,
                verdict=verdict,
                created_at=now_ms,
            )
        else:
            logger.warning(
                "Claude classification failed for message %s; leaving unanalysed (SPEC.md §15.5)",
                message.gmail_message_id,
            )

        context.append({"sender": message.sender, "body": message.body or ""})

    return verdicts


def analyze_window(
    conn,
    touched_thread_ids: Iterable[str],
    window_end_ms: int,
    *,
    mailbox: str,
    employee_aliases: List[str],
    internal_domains: List[str],
    model: str,
    max_retries: int,
    reprocess: bool,
    now_ms: int,
) -> AnalysisResult:
    """Analyze every touched thread and compute its as-of state.

    `cached_count`/`analyzed_count` are message counts (SPEC.md §13.4:
    "the Analyzing line counts messages, not threads"). A message whose
    Claude call failed after retries is recorded in
    `failed_message_ids` (SPEC.md §15.5) but still counts toward
    `analyzed_count` -- it WAS attempted this run, just unsuccessfully.
    """
    states: Dict[str, EnquiryState] = {}
    cached_count = 0
    analyzed_count = 0
    failed_message_ids: List[str] = []
    all_messages: Dict[str, ReportEmail] = {}
    all_verdicts: Dict[str, Verdict] = {}

    for thread_id in touched_thread_ids:
        messages = get_report_emails_by_thread(conn, thread_id)
        if not messages:
            continue

        already_cached_ids = {
            m.gmail_message_id
            for m in messages
            if not reprocess
            and get_cached_verdict(conn, m.gmail_message_id, MESSAGE_VERDICT_PROMPT_VERSION) is not None
        }

        verdicts = analyze_thread_messages(
            conn, messages,
            mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains,
            model=model, max_retries=max_retries, reprocess=reprocess, now_ms=now_ms,
        )

        for message in messages:
            all_messages[message.gmail_message_id] = message
            sender_class = classify_address(
                message.sender, mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains
            )
            if sender_class == AddressClass.INTERNAL:
                continue
            if message.gmail_message_id in already_cached_ids:
                cached_count += 1
            else:
                analyzed_count += 1
                if message.gmail_message_id not in verdicts:
                    failed_message_ids.append(message.gmail_message_id)
        all_verdicts.update(verdicts)

        state = compute_state(
            messages, verdicts, window_end_ms,
            mailbox=mailbox, employee_aliases=employee_aliases, internal_domains=internal_domains,
        )
        if state is not None:
            states[thread_id] = state

    return AnalysisResult(
        states=states, cached_count=cached_count, analyzed_count=analyzed_count,
        failed_message_ids=failed_message_ids, messages=all_messages, message_verdicts=all_verdicts,
    )
