"""Signal detection, taxonomy, and priority scoring."""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone

from .db import list_signals, save_signal
from .email_patterns import normalize_domain, resolve_company_domain
from .search_provider import search_web

logger = logging.getLogger(__name__)

SIGNAL_TYPES = {
    "cpo_hire": {"strength": 2.0, "half_life": 90, "label": "New privacy/governance leader"},
    "job_deidentification": {"strength": 2.0, "half_life": 45, "label": "Job posting — de-identification"},
    "data_partnership": {"strength": 2.0, "half_life": 60, "label": "Data-sharing partnership"},
    "regulatory_action": {"strength": 1.5, "half_life": 180, "label": "Regulatory action"},
    "synthetic_data": {"strength": 1.5, "half_life": 90, "label": "Synthetic / AI data initiative"},
    "ma_closing": {"strength": 1.2, "half_life": 120, "label": "M&A / integration"},
    "published_methodology": {"strength": 1.2, "half_life": 365, "label": "Published de-id methodology"},
    "privacy_policy_deid": {"strength": 1.2, "half_life": 365, "label": "Privacy policy — de-identification"},
    "conference_talk": {"strength": 1.0, "half_life": 90, "label": "Conference talk"},
    "compliance_deadline": {"strength": 0.8, "half_life": 90, "label": "Compliance deadline"},
    "news_governance": {"strength": 1.0, "half_life": 60, "label": "Governance news"},
}

CONTACTABILITY = {"A": 1.0, "B": 0.85, "C": 0.6, "D": 0.3}


def signals_enabled() -> bool:
    return os.getenv("SIGNALS_ENABLED", "true").lower() not in ("0", "false", "no")


def recency_decay(days_since: float, half_life: float) -> float:
    if half_life <= 0:
        return 1.0
    return 0.5 ** (days_since / half_life)


def compute_priority(
    icp_score: float,
    signal_strength: float,
    days_since: float,
    half_life: float,
    contact_tier: str = "D",
) -> float:
    tier = (contact_tier or "D").upper()
    contactability = CONTACTABILITY.get(tier, 0.3)
    decay = recency_decay(days_since, half_life)
    return round(icp_score * signal_strength * decay * contactability, 2)


def _days_since(iso_ts: str) -> float:
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return 0


def _signal_queries(company: str) -> list[str]:
    return [
        f'"{company}" "chief privacy officer" OR "data governance" hired OR appointed',
        f'"{company}" de-identification OR anonymization OR "privacy engineering" job',
        f'"{company}" data sharing partnership OR "real world evidence" OR "clean room"',
        f'"{company}" privacy breach OR consent decree OR regulatory',
    ]


def _classify_hit(company: str, hit: dict) -> dict | None:
    text = f"{hit.get('title', '')} {hit.get('snippet', '')}".lower()
    url = hit.get("url", "")
    if "de-identif" in text or "anonymi" in text:
        if "job" in text or "hiring" in text or "career" in url:
            return {"signal_type": "job_deidentification", "snippet": hit.get("snippet", "")[:300], "source_url": url}
        return {"signal_type": "privacy_policy_deid", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if any(w in text for w in ("appointed", "hired", "joins as", "named")) and any(
        w in text for w in ("privacy", "data governance", "chief privacy", "cdo")
    ):
        return {"signal_type": "cpo_hire", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if any(w in text for w in ("partnership", "collaboration", "data sharing", "rwe")):
        return {"signal_type": "data_partnership", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if any(w in text for w in ("ftc", "ocr", "consent decree", "settlement", "breach")):
        return {"signal_type": "regulatory_action", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if any(w in text for w in ("synthetic data", "ai training", "model training")):
        return {"signal_type": "synthetic_data", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if any(w in text for w in ("acquires", "acquisition", "merger", "m&a")):
        return {"signal_type": "ma_closing", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if any(w in text for w in ("iapp", "conference", "summit")) and "privacy" in text:
        return {"signal_type": "conference_talk", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    if "governance" in text or "privacy" in text:
        return {"signal_type": "news_governance", "snippet": hit.get("snippet", "")[:300], "source_url": url}
    return None


def detect_signals_for_company(
    company: str,
    *,
    agent: str = "woodway",
    domain: str | None = None,
    on_progress=None,
) -> list[dict]:
    """Search for trigger events at a company."""
    if not signals_enabled():
        return []
    emit = on_progress or (lambda msg: None)
    domain = normalize_domain(domain) or resolve_company_domain(company)
    found: list[dict] = []
    seen_urls: set[str] = set()

    for q in _signal_queries(company)[:4]:
        hits = search_web(q, max_results=3)
        time.sleep(0.2)
        for hit in hits:
            url = hit.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            sig = _classify_hit(company, hit)
            if not sig:
                continue
            meta = SIGNAL_TYPES.get(sig["signal_type"], {})
            row = {
                "company": company,
                "company_domain": domain,
                "signal_type": sig["signal_type"],
                "strength": meta.get("strength", 1.0),
                "half_life_days": meta.get("half_life", 90),
                "source_url": sig["source_url"],
                "snippet": sig["snippet"],
                "label": meta.get("label", sig["signal_type"]),
            }
            sid = save_signal(row, agent=agent)
            row["id"] = sid
            found.append(row)
            emit(f"  Signal: {row['label']} @ {company}")

    return found


def detect_signals_for_companies(
    companies: list[str],
    *,
    agent: str = "woodway",
    on_progress=None,
) -> dict:
    emit = on_progress or (lambda msg: None)
    total = []
    for i, co in enumerate(companies, 1):
        emit(f"Signals [{i}/{len(companies)}] {co}…")
        total.extend(detect_signals_for_company(co, agent=agent, on_progress=emit))
    return {"detected": len(total), "signals": total}


def best_unconsumed_signal(company: str, *, agent: str = "woodway", domain: str | None = None) -> dict | None:
    """Return the strongest unconsumed signal for a company."""
    from .db import list_signals

    signals = list_signals(agent=agent, days=120, unconsumed_only=True, limit=50, company=company)
    if not signals and domain:
        signals = [
            s for s in list_signals(agent=agent, days=120, unconsumed_only=True, limit=100)
            if s.get("company_domain") == domain or s.get("company") == company
        ]
    if not signals:
        return None
    signals.sort(key=lambda s: float(s.get("strength") or 0), reverse=True)
    return signals[0]


def enrich_lead_priority(lead: dict, signals: list[dict] | None = None) -> float:
    """Compute priority_score from lead + matching signals."""
    icp = float(lead.get("score") or 50)
    tier = (lead.get("contact_tier") or "D").upper()
    company = lead.get("company", "")
    domain = lead.get("company_domain")

    best = 0.0
    if signals is None:
        signals = list_signals(agent=lead.get("agent", "woodway"), days=90, unconsumed_only=True, limit=100)

    for sig in signals:
        if sig.get("company") != company and sig.get("company_domain") != domain:
            continue
        days = _days_since(sig.get("detected_at", ""))
        p = compute_priority(
            icp,
            float(sig.get("strength") or 1.0),
            days,
            float(sig.get("half_life_days") or 90),
            tier,
        )
        best = max(best, p)
    if best == 0:
        best = round(icp * CONTACTABILITY.get(tier, 0.3), 2)
    return best
