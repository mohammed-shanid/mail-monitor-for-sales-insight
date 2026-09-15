"""Unit tests for app.enquiry.brands.normalise -- SPEC.md §20.2 cases
27-29.
"""

from __future__ import annotations

import pytest

from app.enquiry.brands import UNKNOWN_BRAND, normalise


# --- 27. Single brand extraction and normalisation ---------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Schneider Electric", "Schneider"),
        ("schneider-electric", "Schneider"),
        ("Sneider", "Schneider"),
        ("SIEMENS AG", "Siemens"),
        ("seimens", "Siemens"),
        ("ABB Ltd", "ABB"),
        ("a.b.b.", "ABB"),
        ("Larsen & Toubro", "L&T"),
        ("larsen and toubro", "L&T"),
        ("L and T", "L&T"),
        ("LNT", "L&T"),
        ("Legrand", "Legrand"),
        ("legrande", "Legrand"),
    ],
)
def test_normalise_known_aliases(raw, expected):
    assert normalise(raw) == expected


def test_normalise_is_case_and_whitespace_insensitive():
    assert normalise("  ScHNeider   Electric  ") == "Schneider"


# --- 28. Multiple brands -> counted under each (handled by metrics, not
# normalise() itself -- this just confirms normalise() is stable per call) --


def test_normalise_each_brand_independently():
    assert normalise("Schneider") == "Schneider"
    assert normalise("Siemens") == "Siemens"


# --- 29. Unknown brand surfaces as Unknown ------------------------------------


def test_normalise_none_is_unknown():
    assert normalise(None) == UNKNOWN_BRAND


def test_normalise_empty_string_is_unknown():
    assert normalise("") == UNKNOWN_BRAND


def test_normalise_whitespace_only_is_unknown():
    assert normalise("   ") == UNKNOWN_BRAND


def test_normalise_unrecognised_brand_passes_through_not_discarded():
    # SPEC.md §10.1: a hardcoded list is for normalisation only, never
    # the sole source of detection -- an unrecognised brand must still
    # be captured, not silently dropped or coerced to Unknown.
    assert normalise("Havells") == "Havells"


def test_normalise_does_not_guess_a_similar_but_different_brand():
    assert normalise("Polycab") == "Polycab"
    assert normalise("Polycab") != "Schneider"
