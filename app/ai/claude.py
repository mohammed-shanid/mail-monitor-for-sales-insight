"""Claude API integration: turning email text into structured, validated
data, and answering a founder's open-ended question about one specific
enquiry. This is the ONLY module in the project that calls Claude.

Three jobs:
  - extract_new_enquiry()   -- brand-new thread: is this an enquiry, and
                                who/what is it about?
  - classify_reply_intent() -- existing thread: what is the latest
                                message saying? (one of a fixed,
                                controlled set of intents -- Claude can
                                never invent a new status string)
  - summarize_enquiry()     -- founder asked an open-ended question
                                about a specific enquiry (project spec
                                section 24); summarizes real thread
                                history, never invents details.

Claude is never used for counting, filtering, sorting, or date-range
logic -- see app/database/queries.py (Phase 3/5) for that.

Every response is constrained with output_config's JSON-schema format
(the API guarantees the first content block is valid JSON matching the
schema) and then re-validated in Python before a caller can use it --
belt and suspenders, per the project rule to never blindly trust model
output. On any failure (malformed JSON, missing/wrong-typed fields, API
error, timeout, rate limit, empty response) these functions log and
return None rather than raising, so a sync run can skip one bad message
instead of crashing.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import anthropic

from app.ai.prompts import MESSAGE_VERDICT_SCHEMA, MESSAGE_VERDICT_SYSTEM_PROMPT, SUMMARY_SYSTEM_PROMPT
from app.config import CLAUDE_API_KEY
from app.enquiry.models import ClosureKind, QuotationSignal, Verdict

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# Fixed, controlled set of reply intents (project spec section 11: "Keep
# these controlled. Do not let Claude invent arbitrary status strings.").
# app/enquiry/status.py (Phase 3/4) maps each of these to a status change.
REPLY_INTENTS = [
    "ACCEPTED",
    "REJECTED",
    "NEEDS_INFORMATION",
    "NEGOTIATING",
    "FOLLOW_UP",
    "GENERAL_REPLY",
]

_NEW_ENQUIRY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_enquiry": {"type": "boolean"},
        "customer_name": {"type": ["string", "null"]},
        "customer_email": {"type": ["string", "null"]},
        "product": {"type": ["string", "null"]},
        "quantity": {"type": ["integer", "null"]},
    },
    "required": ["is_enquiry", "customer_name", "customer_email", "product", "quantity"],
    "additionalProperties": False,
}

_INTENT_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": REPLY_INTENTS}},
    "required": ["intent"],
    "additionalProperties": False,
}

_NEW_ENQUIRY_SYSTEM_PROMPT = (
    "You read incoming emails to a company's sales enquiry mailbox and "
    "determine whether each one is a genuine customer enquiry (a request "
    "for a quote, product availability, or similar commercial interest) "
    "as opposed to spam, newsletters, internal mail, security alerts, or "
    "other automated notifications. When it is a genuine enquiry, extract "
    "the customer's name, their email address, the product/item they are "
    "asking about, and the quantity if one is stated. Use null for any "
    "field you cannot determine from the text -- never guess or invent "
    "a value."
)

_INTENT_SYSTEM_PROMPT = (
    "You classify the latest message in an ongoing sales-enquiry email "
    "thread between a company and a customer. Choose exactly one intent "
    "from the allowed list that best matches what the message is "
    "communicating:\n"
    "- ACCEPTED: the customer has agreed to proceed / confirmed the order\n"
    "- REJECTED: the customer has declined or is not proceeding\n"
    "- NEEDS_INFORMATION: the message asks a question, or more info is "
    "needed before proceeding\n"
    "- NEGOTIATING: discussing price, terms, quantity, or specifications\n"
    "- FOLLOW_UP: a check-in or reminder with no new decision\n"
    "- GENERAL_REPLY: anything else (acknowledgements, small talk, "
    "unclear intent)"
)


@dataclass
class EnquiryExtraction:
    """Validated result of extract_new_enquiry()."""

    is_enquiry: bool
    customer_name: Optional[str]
    customer_email: Optional[str]
    product: Optional[str]
    quantity: Optional[int]


def _client() -> anthropic.Anthropic:
    # Constructed lazily (never at import time) so importing this module,
    # or unit testing it with a mocked _call_claude, never requires
    # CLAUDE_API_KEY to be set.
    return anthropic.Anthropic(api_key=CLAUDE_API_KEY)


def _request(
    *, system: str, user_content: str, max_tokens: int, schema: Optional[dict] = None
):
    """Shared Claude call + error handling. Returns the raw SDK response,
    or None on any failure -- logged, never raised, so a sync run or a
    founder's question always degrades gracefully instead of crashing.

    Low effort is intentional throughout this module: bounded
    classification/extraction/summarization, not open-ended agentic work.

    Deliberately NO explicit `temperature` here: a live run against the
    real API (Stage 4) discovered that this model rejects `temperature`
    when combined with `output_config`'s `effort` key (HTTP 400:
    "`temperature` is deprecated for this model"). An earlier attempt
    to default `temperature=0` here for every caller passed every
    mocked test (mocks don't inspect call kwargs) but would have broken
    the bot's real Claude calls in production -- exactly the kind of
    thing only a real API call catches. SPEC.md's temperature=0
    requirement (Appendix A #8) is satisfied for the report path by
    `_request_with_retry` below instead, which uses temperature without
    `effort` (not a SPEC requirement, just this module's original
    latency/cost knob) -- see that function's docstring.
    """
    output_config = {"effort": "low"}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": schema}

    try:
        return _client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            output_config=output_config,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.RateLimitError as exc:
        logger.warning("Claude API rate limited: %s", exc)
    except anthropic.APITimeoutError as exc:
        logger.warning("Claude API request timed out: %s", exc)
    except anthropic.APIConnectionError as exc:
        logger.warning("Claude API connection error: %s", exc)
    except anthropic.APIStatusError as exc:
        logger.error("Claude API error (status %s): %s", exc.status_code, exc.message)
    except anthropic.AnthropicError as exc:
        logger.error("Unexpected Claude SDK error: %s", exc)
    return None


def _response_text(response) -> Optional[str]:
    """First text block's raw content, or None if the response has no
    usable text -- logged as an empty-response failure either way.
    """
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks or not text_blocks[0].strip():
        logger.error("Claude returned an empty response")
        return None
    return text_blocks[0]


def _call_claude(
    *, system: str, user_content: str, schema: dict, max_tokens: int = 1024
) -> Optional[dict]:
    """Call Claude with a JSON-schema-constrained response and return
    the parsed dict, or None on any failure (API error, empty response,
    malformed JSON, non-object JSON).
    """
    response = _request(system=system, user_content=user_content, max_tokens=max_tokens, schema=schema)
    if response is None:
        return None

    text = _response_text(response)
    if text is None:
        return None

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Claude returned malformed JSON: %s", exc)
        return None

    if not isinstance(parsed, dict):
        logger.error("Claude JSON response was not an object: %r", parsed)
        return None

    return parsed


def _call_claude_text(*, system: str, user_content: str, max_tokens: int = 1024) -> Optional[str]:
    """Call Claude for a free-form text answer (no JSON schema) and
    return the stripped text, or None on any failure.
    """
    response = _request(system=system, user_content=user_content, max_tokens=max_tokens)
    if response is None:
        return None

    text = _response_text(response)
    return text.strip() if text is not None else None


def _validate_enquiry_extraction(data: dict) -> Optional[EnquiryExtraction]:
    """Defense in depth: even with output_config enforcing the schema,
    never trust model output blindly (project rule) -- re-check shape
    before it can reach the database.
    """
    required = ("is_enquiry", "customer_name", "customer_email", "product", "quantity")
    if not all(key in data for key in required):
        logger.error("Claude enquiry extraction missing required field(s): %s", list(data.keys()))
        return None

    if not isinstance(data["is_enquiry"], bool):
        logger.error("Claude enquiry extraction: is_enquiry is not a bool: %r", data["is_enquiry"])
        return None

    quantity = data["quantity"]
    if quantity is not None and not isinstance(quantity, int):
        logger.error("Claude enquiry extraction: quantity is not an int/null: %r", quantity)
        return None

    for field in ("customer_name", "customer_email", "product"):
        if data[field] is not None and not isinstance(data[field], str):
            logger.error("Claude enquiry extraction: %s is not a string/null: %r", field, data[field])
            return None

    return EnquiryExtraction(
        is_enquiry=data["is_enquiry"],
        customer_name=data["customer_name"],
        customer_email=data["customer_email"],
        product=data["product"],
        quantity=quantity,
    )


def extract_new_enquiry(subject: str, body: str) -> Optional[EnquiryExtraction]:
    """Given a brand-new email thread's subject + body, determine
    whether it's a genuine customer enquiry and, if so, extract the
    customer/product details.

    Returns None if Claude couldn't be reached or returned something
    that fails validation -- callers should treat that as "try again
    later" (e.g. leave the message unprocessed for the next sync), not
    as "this is not an enquiry".
    """
    user_content = f"Subject: {subject}\n\n{body}".strip()

    data = _call_claude(
        system=_NEW_ENQUIRY_SYSTEM_PROMPT,
        user_content=user_content,
        schema=_NEW_ENQUIRY_SCHEMA,
    )
    if data is None:
        return None

    result = _validate_enquiry_extraction(data)
    if result is not None:
        logger.info("Claude extraction successful: is_enquiry=%s", result.is_enquiry)
    else:
        logger.error("Claude extraction failed validation")
    return result


def classify_reply_intent(message_body: str) -> Optional[str]:
    """Classify the latest message on an EXISTING enquiry thread into
    one of REPLY_INTENTS.

    Returns None on any failure -- callers should leave the enquiry's
    status unchanged rather than guess at an intent.
    """
    data = _call_claude(
        system=_INTENT_SYSTEM_PROMPT,
        user_content=message_body.strip(),
        schema=_INTENT_SCHEMA,
        max_tokens=256,
    )
    if data is None:
        return None

    intent = data.get("intent")
    if intent not in REPLY_INTENTS:
        logger.error("Claude returned an intent outside the controlled set: %r", intent)
        return None

    logger.info("Claude intent classification successful: %s", intent)
    return intent


_SUMMARY_SYSTEM_PROMPT = (
    "You answer a company founder's question about ONE specific sales "
    "enquiry, using ONLY the email thread history provided below. Write "
    "2-4 short, concrete sentences in plain business language -- no "
    "greetings, no headers, no bullet points. State what the customer "
    "wants, what has happened so far, and what (if anything) is "
    "currently waiting on a reply. Never invent details that are not in "
    "the provided messages -- if something is unclear or missing, say so "
    "plainly instead of guessing."
)


def summarize_enquiry(
    *,
    customer_name: Optional[str],
    product: Optional[str],
    quantity: Optional[int],
    status: str,
    thread_messages: List[dict],
) -> Optional[str]:
    """Summarize one enquiry's real email thread for a founder's
    open-ended question (project spec section 24), e.g. "What happened
    with ABC Industries?".

    `thread_messages` is the enquiry's own emails, oldest first, each a
    dict with "sender", "body", "received_at" -- see
    app.database.queries.get_emails_by_thread(). Returns None if there's
    nothing to summarize or the Claude call fails; callers should show a
    generic "couldn't summarize right now" reply rather than guessing.
    """
    if not thread_messages:
        logger.error("summarize_enquiry called with no thread messages")
        return None

    thread_text = "\n\n".join(
        f"[{m['received_at']}] From: {m['sender']}\n{m['body']}" for m in thread_messages
    )
    user_content = (
        f"Customer: {customer_name or 'Unknown'}\n"
        f"Product: {product or 'Not specified'}\n"
        f"Quantity: {quantity if quantity is not None else 'Not specified'}\n"
        f"Current status: {status}\n\n"
        f"Email thread (oldest to newest):\n{thread_text}"
    )

    summary = _call_claude_text(system=_SUMMARY_SYSTEM_PROMPT, user_content=user_content, max_tokens=512)
    if summary is not None:
        logger.info("Claude enquiry summarization successful")
    return summary


# =============================================================================
# Report path (SPEC.md §15), added Stage 3. Everything above is unchanged
# and still used by the bot exclusively (extract_new_enquiry,
# classify_reply_intent, summarize_enquiry, and their prompts/schemas).
# =============================================================================

_RETRYABLE_STATUS_THRESHOLD = 500


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return True
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= _RETRYABLE_STATUS_THRESHOLD:
        return True
    return False


def _request_with_retry(
    *,
    system: str,
    user_content: str,
    max_tokens: int,
    schema: Optional[dict] = None,
    max_retries: int = 3,
    model: str = MODEL,
):
    """Like _request(), but retries with exponential backoff (1s, 2s,
    4s, ...) on RateLimitError/APITimeoutError/APIConnectionError/5xx
    APIStatusError, up to `max_retries` additional attempts (SPEC.md
    §15.5, `AI_MAX_RETRIES`). A non-retryable failure (4xx, malformed
    response, any other AnthropicError) returns None immediately, same
    as _request(). Not built on top of _request() itself -- _request()
    already swallows every exception into a bare None, which makes it
    impossible to tell a retryable failure from a permanent one from
    the outside. Used only by the Stage 3 functions below; the bot's
    three functions above call the original _request() and are
    unaffected by any of this.

    temperature=0 always (never a parameter -- this project's Claude
    calls are deterministic everywhere, no exceptions). Deliberately no
    `effort` key in output_config here, unlike _request() above -- a
    live run against the real API found the two mutually exclusive for
    this model ("`temperature` is deprecated for this model" when both
    are set). `effort` is not a SPEC requirement (SPEC.md never
    mentions it); `temperature=0` is (Appendix A #8), so this keeps
    that and drops the other. `output_config` is omitted entirely when
    there is no schema, rather than sent empty.
    """
    request_kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    if schema is not None:
        request_kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}

    attempt = 0
    while True:
        try:
            return _client().messages.create(**request_kwargs)
        except anthropic.AnthropicError as exc:
            retryable = _is_retryable(exc)
            if retryable and attempt < max_retries:
                delay = 2**attempt
                logger.warning(
                    "Claude API error (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, delay, exc,
                )
                time.sleep(delay)
                attempt += 1
                continue
            logger.error(
                "Claude API call failed (retryable=%s, attempts=%d): %s", retryable, attempt + 1, exc
            )
            return None


_VERDICT_REQUIRED_FIELDS = (
    "is_enquiry", "counterparty_type", "customer_name", "company", "product",
    "requirement", "quantity", "brands", "quotation_signal", "closure_signal",
    "closure_evidence", "closure_kind", "urgency", "urgency_evidence", "confidence",
)
_VERDICT_STRING_OR_NULL_FIELDS = (
    "counterparty_type", "customer_name", "company", "product", "requirement",
    "quantity", "quotation_signal", "closure_evidence", "closure_kind",
    "urgency_evidence", "confidence",
)
_VERDICT_BOOL_OR_NULL_FIELDS = ("is_enquiry", "closure_signal", "urgency")
_COUNTERPARTY_TYPES = {"customer", "supplier", "unknown"}


def _validate_verdict(data: dict) -> Optional[Verdict]:
    """Defense in depth: even with output_config enforcing the schema,
    never trust model output blindly -- re-check shape and controlled
    enums before it can reach the database (SPEC.md §15.2).
    """
    if not all(key in data for key in _VERDICT_REQUIRED_FIELDS):
        logger.error("Claude message verdict missing required field(s): %s", list(data.keys()))
        return None

    for field in _VERDICT_BOOL_OR_NULL_FIELDS:
        if data[field] is not None and not isinstance(data[field], bool):
            logger.error("Verdict %s is not a bool/null: %r", field, data[field])
            return None

    for field in _VERDICT_STRING_OR_NULL_FIELDS:
        if data[field] is not None and not isinstance(data[field], str):
            logger.error("Verdict %s is not a string/null: %r", field, data[field])
            return None

    brands = data.get("brands")
    if not isinstance(brands, list) or not all(isinstance(b, str) for b in brands):
        logger.error("Verdict brands is not a list of strings: %r", brands)
        return None

    if data["quotation_signal"] is not None and data["quotation_signal"] not in QuotationSignal.ALL:
        logger.error("Verdict quotation_signal outside the controlled set: %r", data["quotation_signal"])
        return None
    if data["closure_kind"] is not None and data["closure_kind"] not in ClosureKind.ALL:
        logger.error("Verdict closure_kind outside the controlled set: %r", data["closure_kind"])
        return None
    if data["counterparty_type"] is not None and data["counterparty_type"] not in _COUNTERPARTY_TYPES:
        logger.error("Verdict counterparty_type outside the controlled set: %r", data["counterparty_type"])
        return None

    return Verdict(
        is_enquiry=data["is_enquiry"],
        counterparty_type=data["counterparty_type"],
        customer_name=data["customer_name"],
        company=data["company"],
        product=data["product"],
        requirement=data["requirement"],
        quantity=data["quantity"],
        brands=list(brands),
        quotation_signal=data["quotation_signal"],
        closure_signal=data["closure_signal"],
        closure_evidence=data["closure_evidence"],
        closure_kind=data["closure_kind"],
        urgency=data["urgency"],
        urgency_evidence=data["urgency_evidence"],
        confidence=data["confidence"],
    )


def classify_message(
    *,
    target_subject: str,
    target_body: str,
    context_messages: List[dict],
    model: str = MODEL,
    max_retries: int = 3,
) -> Optional[Verdict]:
    """Classify ONE message (SPEC.md §15.3), given the preceding thread
    as context. `context_messages` is oldest-first, each a dict with
    "sender" and "body" (already cleaned) -- the target message itself
    is NOT included in context_messages; it is passed separately via
    target_subject/target_body, so the cache key (gmail_message_id,
    prompt_version) always corresponds to exactly what was judged.

    Returns None on any API failure (after exhausting retries) or a
    validation failure -- callers should treat that as "try again
    later" (leave the message unanalysed for this run), never as "this
    is not an enquiry" (SPEC.md §15.5: never fabricate a field to fill
    a gap left by a failure).
    """
    if context_messages:
        context_text = "\n\n".join(
            f"[{m.get('sender', '')}]\n{m.get('body', '')}" for m in context_messages
        )
    else:
        context_text = "(no earlier messages in this thread)"

    user_content = (
        f"Thread context (earlier messages, oldest first):\n{context_text}\n\n"
        f"--- TARGET MESSAGE (classify this one) ---\n"
        f"Subject: {target_subject}\n\n{target_body}"
    )

    response = _request_with_retry(
        system=MESSAGE_VERDICT_SYSTEM_PROMPT,
        user_content=user_content,
        max_tokens=1024,
        schema=MESSAGE_VERDICT_SCHEMA,
        max_retries=max_retries,
        model=model,
    )
    if response is None:
        return None

    text = _response_text(response)
    if text is None:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Claude returned malformed JSON for message verdict: %s", exc)
        return None
    if not isinstance(data, dict):
        logger.error("Claude message-verdict JSON response was not an object: %r", data)
        return None

    verdict = _validate_verdict(data)
    if verdict is not None:
        logger.info("Claude message classification successful: is_enquiry=%s", verdict.is_enquiry)
    else:
        logger.error("Claude message classification failed validation")
    return verdict


def generate_summary(metrics: dict, *, model: str = MODEL, max_retries: int = 3) -> Optional[str]:
    """SPEC.md §15.4: the summary call receives ONLY the computed
    metrics object -- no raw email bodies, ever. The post-generation
    numeral guard (asserting every number in the summary is one
    Python already computed) lives in app.reporting.summary (Stage 4),
    not here -- this function's only job is the API call itself.
    """
    user_content = json.dumps(metrics, sort_keys=True, default=str)

    response = _request_with_retry(
        system=SUMMARY_SYSTEM_PROMPT,
        user_content=user_content,
        max_tokens=512,
        schema=None,
        max_retries=max_retries,
        model=model,
    )
    if response is None:
        return None

    text = _response_text(response)
    if text is not None:
        logger.info("Claude summary generation successful")
    return text.strip() if text is not None else None
