"""Keira evidence layer — source reliability + signal time decay."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Higher = more trustworthy. LLM inference alone is never evidence.
SOURCE_RELIABILITY: dict[str, float] = {
    "company_website": 1.00,
    "company": 1.00,
    "government_registry": 1.00,
    "registry": 1.00,
    "press_release": 0.95,
    "industry_association": 0.90,
    "news": 0.90,
    "reputable_news": 0.90,
    "linkedin": 0.75,
    "business_directory": 0.55,
    "apollo": 0.55,
    "seamless": 0.55,
    "directory": 0.30,
    "text": 0.55,  # heuristic extract from mixed blob
    "founded_year": 0.70,
    "llm_inference": 0.0,
    "claude": 0.45,  # synthesis — useful but not primary evidence
}

# Age buckets → multiplicative decay on strength/confidence
DECAY_BUCKETS = (
    (90, 1.00),    # < 3 months
    (180, 0.90),   # 3–6 months
    (365, 0.75),   # 6–12 months
    (730, 0.50),   # 12–24 months
    (10**9, 0.20), # > 24 months
)


def source_weight(source: str | None) -> float:
    if not source:
        return SOURCE_RELIABILITY["text"]
    key = str(source).strip().lower().replace(" ", "_").replace("-", "_")
    if key in SOURCE_RELIABILITY:
        return SOURCE_RELIABILITY[key]
    for name, w in SOURCE_RELIABILITY.items():
        if name in key or key in name:
            return w
    return 0.40


def decay_factor(
    *,
    source_date: str | None = None,
    observed_at: str | None = None,
    now: datetime | None = None,
) -> float:
    """Return 0–1 decay multiplier from signal age."""
    now = now or datetime.now(timezone.utc)
    raw = source_date or observed_at
    if not raw:
        return 1.0  # unknown age — no penalty (don't invent staleness)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return 1.0
    age_days = max(0, (now - dt).days)
    for limit, factor in DECAY_BUCKETS:
        if age_days < limit:
            return factor
    return 0.20


def weight_signal(signal: dict, *, now: datetime | None = None) -> dict:
    """Attach reliability, decay, and effective strength to a signal dict."""
    out = dict(signal)
    rel = source_weight(out.get("source"))
    decay = decay_factor(
        source_date=out.get("source_date"),
        observed_at=out.get("observed_at"),
        now=now,
    )
    base_strength = float(out.get("strength") or 0.5)
    base_conf = float(out.get("confidence") or 0.5)
    out["source_reliability"] = rel
    out["decay"] = decay
    out["effective_strength"] = round(base_strength * rel * decay, 3)
    out["effective_confidence"] = round(base_conf * rel * decay, 3)
    if "observed_at" not in out:
        out["observed_at"] = (now or datetime.now(timezone.utc)).isoformat()
    return out


def weight_signals(signals: list[dict], *, now: datetime | None = None) -> list[dict]:
    return [weight_signal(s, now=now) for s in (signals or [])]


def evidence_object(
    *,
    claim: str,
    evidence: str,
    source: str,
    source_url: str | None = None,
    source_date: str | None = None,
    confidence: float = 0.7,
    signal_type: str | None = None,
    strength: float = 0.5,
) -> dict[str, Any]:
    """Structured proof object for every important assertion."""
    now = datetime.now(timezone.utc).isoformat()
    obj = {
        "claim": claim,
        "type": signal_type,
        "evidence": evidence,
        "source": source,
        "source_url": source_url,
        "source_date": source_date,
        "observed_at": now,
        "confidence": confidence,
        "strength": strength,
    }
    return weight_signal(obj)


def aggregate_evidence_confidence(signals: list[dict]) -> float:
    """0–1 research confidence from weighted evidence (not raw count)."""
    weighted = weight_signals(signals)
    usable = [s for s in weighted if float(s.get("source_reliability") or 0) > 0]
    if not usable:
        return 0.15
    # Average effective confidence, boosted slightly by diversity of sources
    avg = sum(float(s.get("effective_confidence") or 0) for s in usable) / len(usable)
    sources = {str(s.get("source") or "") for s in usable}
    diversity = min(0.15, 0.05 * max(0, len(sources) - 1))
    return round(min(0.98, avg + diversity), 3)
