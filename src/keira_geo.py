"""Keira geography verification — Eastern Ontario precision, not 'Ontario'."""

from __future__ import annotations

import re

# Strong HQ / ops cities in Keira's corridor (city-level only — not marketing phrases)
STRONG_CITIES = frozenset({
    "ottawa", "kanata", "nepean", "orleans", "orléans", "gloucester",
    "kingston", "cornwall", "brockville", "belleville", "pembroke",
    "renfrew", "smiths falls", "arnprior", "hawkesbury", "perth",
    "carleton place", "vanier", "barrhaven", "stittsville",
    "gatineau",  # NCR — often treated as Ottawa market
})

# Corridor phrases that are weak alone (need a real city HQ)
CORRIDOR_PHRASES = frozenset({
    "eastern ontario",
})

# Weak / insufficient alone
WEAK_GEO = frozenset({
    "ontario", "eastern canada", "national", "canada",
    "serves ottawa", "ottawa market", "greater toronto", "gta",
    "toronto", "mississauga", "brampton", "markham", "vaughan",
    "hamilton", "london", "windsor", "kitchener", "waterloo",
})

REJECT_HQ = frozenset({
    "toronto", "mississauga", "brampton", "markham", "vaughan",
    "hamilton", "kitchener", "waterloo", "london ontario", "calgary",
    "vancouver", "montreal", "montréal", "gta", "greater toronto",
    "peel region", "york region", "durham region",
})

# Backward-compatible alias used by older call sites / tests
STRONG_GEO = STRONG_CITIES | CORRIDOR_PHRASES


def _blob(*parts: str | None) -> str:
    return " ".join(str(p or "") for p in parts).lower()


def _has_city(blob: str, city: str) -> bool:
    return bool(re.search(rf"\b{re.escape(city)}\b", blob))


def _market_language_only(text: str, *, hq_city_blob: str) -> bool:
    """True when Ottawa/corridor appears only as 'serves / market' language, not HQ."""
    if any(_has_city(hq_city_blob, c) for c in STRONG_CITIES):
        return False
    return bool(
        re.search(
            r"\bserves\s+(ottawa|ontario|eastern ontario)\b"
            r"|\b(ottawa|ontario)\s+market\b"
            r"|\bgreat(er)?\s+ottawa\b"
            r"|\bthroughout\s+(ontario|eastern ontario)\b"
            r"|\bacross\s+(ontario|eastern ontario)\b"
            r"|\bcovering\s+(ottawa|ontario)\b",
            text,
        )
    )


def _hq_is_rejected(hq: str | None, city: str | None) -> str | None:
    """If structured HQ/city is outside corridor, return the reject label."""
    structured = _blob(hq, city)
    if not structured.strip():
        return None
    for bad in REJECT_HQ:
        if bad == "london ontario":
            if "london" in structured and "ontario" in structured:
                return bad
            continue
        if _has_city(structured, bad) or bad in structured:
            return bad
    return None


def geo_score(
    *,
    hq: str | None = None,
    city: str | None = None,
    state: str | None = None,
    locations: list[str] | None = None,
    owner_location: str | None = None,
    extra_text: str | None = None,
) -> dict:
    """
    Score geography 0–20 with hard reject flags.
    Strong city HQ >= 15 typically required before expensive research.

    Hard rules:
    - Structured HQ/city in GTA/Toronto/etc. always rejects — marketing
      "serves Eastern Ontario" cannot override.
    - "Serves Ottawa/Ontario" language is never HQ evidence.
    """
    hq_city_blob = _blob(hq, city)
    text = _blob(hq, city, state, owner_location, extra_text, *(locations or []))
    score = 0
    reasons: list[str] = []
    reject = False

    serves_only = _market_language_only(text, hq_city_blob=hq_city_blob)

    # 1) Structured HQ/city outside corridor — hard reject (no marketing override)
    structured_bad = _hq_is_rejected(hq, city)
    if structured_bad:
        score -= 20
        reasons.append(f"HQ/ops outside corridor: {structured_bad}")
        reject = True

    # 2) Free-text reject cities only if no credible corridor city HQ
    if not reject:
        for bad in REJECT_HQ:
            if bad == "london ontario":
                if "london" in text and "ontario" in text and "ottawa" not in hq_city_blob:
                    score -= 20
                    reasons.append("HQ appears London ON / outside corridor")
                    reject = True
                    break
                continue
            if not (_has_city(text, bad) or bad in text):
                continue
            # Allow Toronto mention only when HQ/city is a real corridor city
            if any(_has_city(hq_city_blob, c) for c in STRONG_CITIES):
                reasons.append(f"mentions {bad} but HQ is in corridor")
                continue
            score -= 20
            reasons.append(f"HQ/ops outside corridor: {bad}")
            reject = True
            break

    strong_hit = None
    for city_name in STRONG_CITIES:
        if _has_city(hq_city_blob, city_name):
            strong_hit = city_name
            break
    if not strong_hit and not serves_only and not reject:
        for city_name in STRONG_CITIES:
            if _has_city(text, city_name):
                strong_hit = city_name
                break

    if strong_hit and not reject and not serves_only:
        if hq and strong_hit in (hq or "").lower():
            score += 20
            reasons.append(f"HQ in corridor ({strong_hit})")
        elif city and strong_hit in (city or "").lower():
            score += 18
            reasons.append(f"City in corridor ({strong_hit})")
        else:
            score += 15
            reasons.append(f"Corridor location signal ({strong_hit})")
    elif any(p in text for p in CORRIDOR_PHRASES) and not reject and not strong_hit:
        score += 0
        reasons.append("Eastern Ontario phrase without city HQ — insufficient")
    elif "ontario" in text and not reject:
        score += 0
        reasons.append("Ontario-only — insufficient geo precision")

    if serves_only:
        score = min(score, 0)
        reasons.append("'Serves Ottawa/Ontario' is not HQ evidence")

    if owner_location and not reject:
        ol = owner_location.lower()
        if any(_has_city(ol, s) for s in STRONG_CITIES):
            score += 5
            reasons.append("Owner location in corridor")

    score = max(0, min(20, score))
    return {
        "score": score,
        "max": 20,
        "pass": score >= 15 and not reject,
        "reject": reject,
        "reasons": reasons,
        "strong_city": strong_hit,
    }


def passes_keira_geo(text: str, *, min_score: int = 15) -> bool:
    g = geo_score(extra_text=text)
    return g["pass"] and g["score"] >= min_score
