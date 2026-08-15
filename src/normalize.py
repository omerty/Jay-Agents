"""Display normalization — title case for names/companies stored lowercase."""

from __future__ import annotations

import re

ACRONYMS = frozenset({
    "ibm", "hsbc", "rbc", "td", "bmo", "cibc", "jpm", "bp", "gs", "ms", "ubs",
    "dpo", "gdpr", "ai", "it", "usa", "uk", "eu", "llc", "inc", "ltd", "thg",
})

# Brands that look like acronyms / need specific casing
BRAND_CASING = {
    "sofi": "SoFi",
    "jpmorgan": "JPMorgan",
    "jpmorgan chase": "JPMorgan Chase",
    "mastercard": "Mastercard",
    "linkedin": "LinkedIn",
}

SMALL_WORDS = frozenset({"and", "or", "the", "of", "at", "for", "in", "on"})


def _title_word(word: str, *, first: bool = False) -> str:
    w = word.strip()
    if not w:
        return w
    lower = w.lower()
    if lower in ACRONYMS:
        return lower.upper()
    if not first and lower in SMALL_WORDS:
        return lower
    if w.isupper() and len(w) <= 4:
        return w
    return lower.capitalize()


def normalize_display_name(name: str | None, *, kind: str = "company") -> str | None:
    """Title-case lowercase-only names for dashboard display."""
    if not name:
        return name
    text = name.strip()
    if not text:
        return text
    # Already has mixed case — leave as-is unless fully lower
    if text != text.lower():
        return text
    brand = BRAND_CASING.get(text.lower())
    if brand:
        return brand
    if kind == "person":
        parts = re.split(r"(\s+|-)", text)
        out = []
        for i, p in enumerate(parts):
            if p.strip() == "" or p in ("-", " "):
                out.append(p)
            else:
                out.append(_title_word(p, first=(i == 0)))
        return "".join(out)
    # Company: handle "at", parentheses lightly
    words = re.split(r"(\s+)", text)
    out = []
    word_idx = 0
    for w in words:
        if w.strip() == "":
            out.append(w)
        else:
            out.append(_title_word(w, first=(word_idx == 0)))
            word_idx += 1
    return "".join(out)
