"""Keira event-driven rediscovery — re-score when something changes.

Instead of only nightly cold search, monitor known companies for:
  new president / retirement / succession / acquisition language / family join
and bump them back into the research queue when the fingerprint or signal changes.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .db import build_prospect_string, get_leads_by_statuses, upsert_lead
from .keira_budget import research_priority
from .keira_intelligence import (
    build_company_intelligence,
    build_mna_thesis,
    qualify_keira_intelligence,
)
from .keira_memory import (
    companies_due,
    fingerprint_lead,
    memory_enabled,
    remember_research,
    status_from_qualification,
)

logger = logging.getLogger(__name__)

EVENT_PATTERNS = (
    (r"\bnew president\b", "new_president"),
    (r"\bretir(e|ing|ement)\b", "retirement"),
    (r"\bsuccession\b", "succession"),
    (r"\bownership transition\b", "ownership_transition"),
    (r"\bacquir(e|ed|ing|isition)\b", "acquisition"),
    (r"\bexpand(ing|s|ed)?\b", "expansion"),
    (r"\b(daughter|son)\b.*\b(vp|president|joined)\b", "family_join"),
    (r"\bjoined the (company|business)\b", "leadership_join"),
    (r"\bnext chapter\b", "next_chapter"),
    (r"\bstepping back\b", "founder_step_back"),
)


def events_enabled() -> bool:
    return os.getenv("KEIRA_EVENT_REDISCOVERY", "true").lower() not in ("0", "false", "no")


def detect_events(text: str) -> list[dict]:
    text = text or ""
    lower = text.lower()
    out = []
    for pat, etype in EVENT_PATTERNS:
        if re.search(pat, lower, re.I):
            out.append({"type": etype, "pattern": pat})
    return out


def rescore_company(lead: dict, *, config: dict | None = None) -> dict:
    """Re-run intelligence + qualify; return score delta metadata."""
    intel = build_company_intelligence(lead)
    q = qualify_keira_intelligence(intel, config=config)
    priority = research_priority(q, intel)
    q.update(priority)
    thesis = build_mna_thesis(intel, q)
    events = detect_events(lead.get("signal") or intel.get("evidence_text") or "")
    return {
        "intel": intel,
        "qual": q,
        "thesis": thesis,
        "priority": priority,
        "events": events,
        "crossed_threshold": bool(q.get("outreach_ok")) or (
            not q.get("rejected") and int(q.get("research_priority") or 0) >= 70
        ),
    }


def run_keira_rediscovery(
    *,
    limit: int = 25,
    config: dict | None = None,
    on_progress=None,
) -> dict[str, Any]:
    """
    Pull due memory rows + existing imported/watchlist leads, re-score, and
    re-queue those that cross the interest threshold.
    """
    emit = on_progress or (lambda msg: None)
    if not events_enabled() or not memory_enabled():
        emit("Keira rediscovery skipped (disabled)")
        return {"ok": True, "skipped": True, "rescored": 0, "requeued": 0}

    emit("Keira rediscovery — checking due companies + event signals…")
    due = companies_due(limit=limit)
    # Also sample recent imported leads without emails for signal refresh
    existing = [
        dict(r) for r in get_leads_by_statuses(
            ["imported", "awaiting_contact", "skipped"],
            agent="keira",
            limit=limit,
        )
    ]

    by_company: dict[str, dict] = {}
    for mem in due:
        by_company[mem["company"]] = {
            "company": mem["company"],
            "signal": (mem.get("reason") or ""),
            "source": "keira_memory",
            "_from_memory": True,
        }
    for lead in existing:
        name = lead.get("company")
        if not name:
            continue
        prev = by_company.get(name) or {}
        by_company[name] = {**prev, **lead}

    rescored = requeued = event_hits = 0
    details = []

    for company, lead in list(by_company.items())[:limit]:
        result = rescore_company(lead, config=config)
        rescored += 1
        events = result["events"]
        if events:
            event_hits += 1
        q = result["qual"]
        fp = fingerprint_lead(lead)
        status, reason = status_from_qualification(q)

        if result["crossed_threshold"] or events:
            # Re-queue as imported for next pipeline enrich pass
            record = {
                **{k: v for k, v in lead.items() if not str(k).startswith("_") and k != "id"},
                "company": company,
                "status": "imported" if not q.get("rejected") else "skipped",
                "score": q.get("score"),
                "tier": q.get("tier"),
                "signal": lead.get("signal") or (q.get("why_now") or "")[:240],
                "source": lead.get("source") or "keira_rediscovery",
            }
            if events and record.get("signal"):
                types = ",".join(e["type"] for e in events[:3])
                record["signal"] = f"[event:{types}] {record['signal']}"[:240]
            record["prospect"] = build_prospect_string(record)
            if not q.get("rejected"):
                upsert_lead(record, agent="keira")
                requeued += 1
                status = "research_later" if not q.get("outreach_ok") else "qualified"

        # Requeued companies must stay researchable in this same pipeline run.
        # remember_research("qualified") normally sets a 120d cooldown which made
        # Step 2 memory-skip everyone rediscovery just woke up.
        from datetime import datetime, timezone

        requeued_now = bool(not q.get("rejected") and (result["crossed_threshold"] or events))
        remember_research(
            company,
            status=status,
            reason=reason or (events[0]["type"] if events else None),
            fingerprint=fp,
            research_priority=int(q.get("research_priority") or 0),
            meta={
                "events": [e["type"] for e in events],
                "score": q.get("score"),
                "crossed": result["crossed_threshold"],
                "requeued": requeued_now,
            },
            cooldown_days=0 if requeued_now else (30 if events else None),
            force_next=datetime.now(timezone.utc) if requeued_now else None,
        )
        details.append({
            "company": company,
            "events": [e["type"] for e in events],
            "score": q.get("score"),
            "status": status,
            "requeued": bool(result["crossed_threshold"] or events) and not q.get("rejected"),
        })

    emit(f"Rediscovery — rescored {rescored}, events {event_hits}, requeued {requeued}")
    return {
        "ok": True,
        "rescored": rescored,
        "event_hits": event_hits,
        "requeued": requeued,
        "details": details[:20],
    }
