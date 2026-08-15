"""Keira research budget allocator — spend Claude/Seamless on highest EV leads."""

from __future__ import annotations

from typing import Any


def research_priority(qualification: dict, intel: dict | None = None) -> dict:
    """
    Separate ICP fit, timing, and confidence — then rank expected value.
    """
    q = qualification or {}
    icp = int(q.get("icp_fit") or 0)
    timing = int(q.get("timing") or q.get("succession_signal") or 0)
    conf = int(q.get("evidence_confidence") or 0)

    # Expected value: ICP matters, but timing unlocks outreach; confidence gates spend
    priority = int(round(0.40 * icp + 0.40 * timing + 0.20 * conf))
    # Prefer Actava-sourced evidence-rich leads
    source = str((intel or {}).get("source") or ((intel or {}).get("company") or {}).get("source") or "")
    if source.startswith("actava"):
        priority = min(100, priority + 12)
    priority = max(0, min(100, priority))

    reasons: list[str] = []
    geo = ((intel or {}).get("geo") or {}).get("strong_city")
    if geo:
        reasons.append(f"{str(geo).title()} HQ")
    own = (intel or {}).get("ownership") or {}
    if own.get("founder"):
        reasons.append("Founder-led")
    elif own.get("title"):
        reasons.append(str(own.get("title")))
    founded = ((intel or {}).get("company") or {}).get("founded")
    if founded:
        reasons.append(f"Operating since {founded}")
    emp = ((intel or {}).get("company") or {}).get("employees")
    if emp:
        reasons.append(f"{emp} employees")
    succ = (intel or {}).get("succession") or {}
    max_tier = int(succ.get("max_tier") or 0)
    if max_tier >= 2:
        reasons.append(f"Succession tier {max_tier}")
    elif max_tier == 1:
        reasons.append("Weak succession signal")

    if priority >= 75 and timing >= 55 and conf >= 40:
        lead_priority = "HIGH"
    elif priority >= 50 and not q.get("rejected"):
        lead_priority = "MEDIUM"
    else:
        lead_priority = "LOW"

    return {
        "research_priority": priority,
        "lead_priority": lead_priority,
        "icp_fit": icp,
        "timing": timing,
        "evidence_confidence": conf,
        "reason": reasons[:6],
        "outreach_priority": lead_priority if q.get("outreach_ok") else "HOLD",
    }


def allocate_budget(
    candidates: list[dict],
    *,
    claude_cap: int = 8,
    seamless_cap: int = 8,
) -> dict[str, Any]:
    """
    Split survivors into Claude-deep-research vs Seamless-enrich queues.

    Each candidate dict should have `_qual` and optionally `_intel`, or
    embedded `score` / `research_priority`.
    """
    scored: list[tuple[int, dict]] = []
    for row in candidates:
        q = row.get("_qual") or {}
        intel = row.get("_intel")
        meta = row.get("_priority") or research_priority(q, intel)
        rp = int(meta.get("research_priority") or row.get("score") or 0)
        scored.append((rp, {**row, "_priority": meta}))

    scored.sort(key=lambda x: x[0], reverse=True)
    ordered = [r for _, r in scored]

    claude_queue = ordered[: max(0, claude_cap)]
    # Seamless only after Claude pass later; pre-allocate by EV for credit planning
    seamless_queue = ordered[: max(0, seamless_cap)]

    return {
        "ordered": ordered,
        "claude_queue": claude_queue,
        "seamless_queue": seamless_queue,
        "claude_cap": claude_cap,
        "seamless_cap": seamless_cap,
        "deferred": ordered[max(claude_cap, seamless_cap) :],
    }
