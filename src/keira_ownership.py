"""Keira corporate ownership detector — PE / subsidiary / public vs founder-owned."""

from __future__ import annotations

import re

OWNERSHIP_ALLOWED = frozenset({
    "founder_owned", "family_owned", "privately_owned", "unknown_private",
})

OWNERSHIP_REJECTED = frozenset({
    "private_equity", "subsidiary", "public", "nonprofit", "government",
    "cooperative", "franchisor",
})

PE_PATTERNS = (
    r"\bprivate equity\b", r"\bpe-backed\b", r"\bportfolio company\b",
    r"\bacquired by\b.*\b(capital|partners|equity)\b",
)
SUB_PATTERNS = (
    r"\bsubsidiary of\b", r"\ba (wholly )?owned subsidiary\b",
    r"\bdivision of\b", r"\bpart of the .+ group\b",
)
PUBLIC_PATTERNS = (
    r"\b(nyse|nasdaq|tsx|tsxv):\s*[A-Z]{1,5}\b",
    r"\bpublicly traded\b", r"\blisted company\b",
    r"\bshareholders\b.*\bannual report\b",
)
NONPROFIT_PATTERNS = (r"\bnon-?profit\b", r"\bcharity\b", r"\bnot-?for-?profit\b")
GOV_PATTERNS = (r"\bgovernment of\b", r"\bcity of ottawa\b", r"\bfederal\b.*\bagency\b", r"\bcrown corp")
FAMILY_PATTERNS = (
    r"\bfamily[- ]owned\b", r"\bfamily business\b", r"\bsecond generation\b",
    r"\bthird generation\b", r"\bfamily[- ]run\b",
)
FOUNDER_PATTERNS = (
    r"\bfounded by\b", r"\bfounder[- ]owned\b", r"\bfounder[- ]led\b",
)


def classify_ownership(
    *,
    company: str | None = None,
    text: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Classify ownership_type from available text.
    Reject PE / subsidiary / public / nonprofit / government.
    """
    blob = f"{company or ''} {title or ''} {text or ''}".lower()
    reasons: list[str] = []

    def _hit(patterns: tuple[str, ...]) -> str | None:
        for p in patterns:
            if re.search(p, blob, re.I):
                return p
        return None

    if _hit(PE_PATTERNS):
        return _result("private_equity", 0.9, reject=True, reason="PE-backed / portfolio language")
    if _hit(PUBLIC_PATTERNS):
        return _result("public", 0.9, reject=True, reason="public company signals")
    if _hit(SUB_PATTERNS) or re.search(r"\b[a-z]+ canada\b", blob) and re.search(r"\b(inc|corp)\b", blob):
        # "Acme Canada" alone is weak — only reject if subsidiary language or clear national brand pattern
        if _hit(SUB_PATTERNS):
            return _result("subsidiary", 0.85, reject=True, reason="subsidiary language")
    if _hit(NONPROFIT_PATTERNS):
        return _result("nonprofit", 0.9, reject=True, reason="nonprofit")
    if _hit(GOV_PATTERNS):
        return _result("government", 0.9, reject=True, reason="government")
    if re.search(r"\bfranchisor\b|\bfranchise system\b", blob):
        return _result("franchisor", 0.85, reject=True, reason="franchisor")

    if _hit(FAMILY_PATTERNS):
        return _result("family_owned", 0.88, reject=False, reason="family-owned language")
    if _hit(FOUNDER_PATTERNS) or (title and re.search(r"\bfounder\b", title, re.I)):
        return _result("founder_owned", 0.86, reject=False, reason="founder-owned / founder-led")

    if title and re.search(r"\b(owner|president|ceo)\b", title, re.I):
        return _result("privately_owned", 0.65, reject=False, reason="private operator assumed from title")

    return _result("unknown_private", 0.4, reject=False, reason="ownership unclear — treat cautiously")


def _result(ownership_type: str, confidence: float, *, reject: bool, reason: str) -> dict:
    return {
        "ownership_type": ownership_type,
        "confidence": confidence,
        "allowed": ownership_type in OWNERSHIP_ALLOWED and not reject,
        "reject": reject or ownership_type in OWNERSHIP_REJECTED,
        "reason": reason,
    }
