"""Unit tests for app.gmail.parser -- quote/signature stripping.

Pure string functions, no I/O, no network.
"""

from __future__ import annotations

from app.gmail.parser import clean_body, strip_quoted_text, strip_signature


def test_strip_quoted_text_cuts_at_on_wrote_header():
    body = (
        "We need 20 more units, please advise.\n\n"
        "On Mon, 1 Sep 2025 at 09:12, ABC Industries <purchase@abc.com> wrote:\n"
        "> We need 20 Schneider MCCB 250A units.\n"
        "> Please quote."
    )
    result = strip_quoted_text(body)
    assert "We need 20 more units" in result
    assert "wrote:" not in result
    assert "Schneider MCCB" not in result


def test_strip_quoted_text_drops_bare_gt_prefixed_lines_without_wrote_header():
    body = "Sure, that works.\n> original question\n> more quoted text"
    result = strip_quoted_text(body)
    assert "Sure, that works." in result
    assert "> original question" not in result
    assert "more quoted text" not in result


def test_strip_quoted_text_no_quote_marker_is_unchanged():
    body = "Just a plain reply with no quoted history at all."
    assert strip_quoted_text(body) == body


def test_strip_quoted_text_does_not_truncate_a_genuine_sentence_mentioning_wrote():
    # Lowercase "wrote" mid-sentence, not a quote header -- must survive.
    body = "I wrote up the quotation yesterday and sent it over."
    assert strip_quoted_text(body) == body


def test_strip_signature_cuts_at_delimiter():
    body = "Thanks,\nplease proceed.\n-- \nRegency Electricals\nSales Team"
    result = strip_signature(body)
    assert "please proceed." in result
    assert "Regency Electricals" not in result


def test_strip_signature_no_delimiter_is_unchanged():
    body = "No signature block here."
    assert strip_signature(body) == body


def test_clean_body_strips_both_and_trims_whitespace():
    body = (
        "  We need 20 more units.  \n\n"
        "-- \nJohn Doe\n\n"
        "On Mon, 1 Sep 2025, ABC wrote:\n> old stuff"
    )
    # Signature delimiter appears before the quote header in this body --
    # clean_body must still remove both regardless of order.
    result = clean_body(body)
    assert result == "We need 20 more units."


def test_clean_body_quote_before_signature():
    body = (
        "New content.\n\n"
        "On Tue, 2 Sep 2025, XYZ wrote:\n> quoted\n-- \nSignature block"
    )
    result = clean_body(body)
    assert result == "New content."


def test_clean_body_empty_string():
    assert clean_body("") == ""
