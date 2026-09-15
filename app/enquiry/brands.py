"""Brand normalisation (SPEC.md §10), added Stage 3.

Extraction happens in Claude's per-message verdict (`brands: list[str]`,
§15.3) -- an unconstrained list, so an unrecognised brand is still
captured. This module only NORMALISES an already-extracted raw brand
string: casing/whitespace, and correcting unambiguous misspellings via
a small alias table. Never the sole source of detection (SPEC.md §10.1).
"""

from __future__ import annotations

import re
from typing import Optional

UNKNOWN_BRAND = "Unknown"

# raw (case/whitespace-normalised) -> canonical display name.
# SPEC.md §10.2's example table -- correct only unambiguous
# misspellings/variants. Do not guess: an unrecognised brand is passed
# through as-is below, never coerced into one of these.
_ALIASES = {
    "schneider electric": "Schneider",
    "schneider-electric": "Schneider",
    "sneider": "Schneider",
    "schneider": "Schneider",
    "siemens ag": "Siemens",
    "seimens": "Siemens",
    "siemens": "Siemens",
    "abb ltd": "ABB",
    "a.b.b.": "ABB",
    "abb": "ABB",
    "larsen & toubro": "L&T",
    "larsen and toubro": "L&T",
    "l and t": "L&T",
    "lnt": "L&T",
    "l&t": "L&T",
    "legrand": "Legrand",
    "legrande": "Legrand",
}


def _normalise_key(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip().lower())


def normalise(raw: Optional[str]) -> str:
    """A raw brand mention -> its canonical display name, or
    "Unknown" if `raw` is empty/None (SPEC.md §10.2: brand is Unknown
    whenever identification is not reliable -- Unknown is signal, not
    a gap to hide; it appears in the brand breakdown like any other
    value).

    A brand not in the alias table is passed through unchanged
    (stripped of leading/trailing whitespace only) -- SPEC.md §10.1:
    a hardcoded list is for normalisation only, never the sole source
    of detection, so an unrecognised (but Claude-extracted) brand must
    still be captured, not discarded or guessed into a known name.
    """
    if not raw or not raw.strip():
        return UNKNOWN_BRAND

    key = _normalise_key(raw)
    return _ALIASES.get(key, raw.strip())
