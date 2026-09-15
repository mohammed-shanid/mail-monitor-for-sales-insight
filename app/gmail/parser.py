"""Quoted-reply and signature stripping (SPEC.md §5.4), added Stage 2.

Separate from app.gmail.client.extract_body: extract_body turns Gmail's
MIME structure into plain text (and, for an HTML body, already drops a
`gmail_quote` div at the HTML-parsing stage -- see that module). This
module operates on the resulting plain text, for the two things that
survive as plain text regardless of MIME type: a plain-text quote
header ("On <date>, <person> wrote:") followed by lines re-quoting the
previous message, and a trailing signature block.

Without this, Claude re-extracts stale brands/quantities from quoted
history on every reply in a thread, corrupting the data and inflating
token cost (SPEC.md §5.4) -- e.g. a 5-message thread's 5th message
would otherwise carry all 4 previous messages' text too.

Pure functions, no I/O, no network -- easy to unit test with plain
strings.
"""

from __future__ import annotations

import re

# Gmail (and most clients) render a plain-text reply's quote header as
# "On <anything>, <anything> wrote:" on its own line, immediately
# followed by the quoted message. Matched case-sensitively on "On" at
# start-of-line (a real sentence starting mid-body with "on ..." would
# be lowercase and/or not end in "wrote:") -- deliberately conservative
# to avoid truncating a genuine reply that happens to mention "wrote".
_QUOTE_HEADER_RE = re.compile(r"^On .{0,200}? wrote:\s*$", re.MULTILINE)

# The de facto standard plain-text signature delimiter (RFC-ish
# convention: two hyphens, one space, nothing else on the line).
_SIGNATURE_DELIM_RE = re.compile(r"^-- ?$", re.MULTILINE)


def strip_quoted_text(body: str) -> str:
    """Cut the body at the first "On ... wrote:" quote header (and
    everything after it), and drop any remaining line that still
    starts with the traditional ">" quote prefix (some clients, or a
    reply-within-a-reply, use ">" without a "wrote:" header at all).
    """
    match = _QUOTE_HEADER_RE.search(body)
    if match:
        body = body[: match.start()]

    lines = [line for line in body.split("\n") if not line.lstrip().startswith(">")]
    return "\n".join(lines)


def strip_signature(body: str) -> str:
    """Cut the body at the first `-- ` signature delimiter line."""
    match = _SIGNATURE_DELIM_RE.search(body)
    if match:
        return body[: match.start()]
    return body


def clean_body(body: str) -> str:
    """Quote-stripped, signature-stripped, whitespace-trimmed body --
    what actually gets stored and sent to Claude (SPEC.md §5.4)."""
    return strip_signature(strip_quoted_text(body)).strip()
