"""Keira Claude research analyst + critic — senior analyst jobs, not search."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Defaults live in src/prompts.py so the dashboard can edit them via prompts.yaml.
from .prompts import DEFAULT_ANALYST_SYSTEM, DEFAULT_CRITIC_SYSTEM

ANALYST_SYSTEM = DEFAULT_ANALYST_SYSTEM
CRITIC_SYSTEM = DEFAULT_CRITIC_SYSTEM


def _prompt_override(key: str, default: str) -> str:
    try:
        from .agent import load_agent

        prompts = (load_agent("keira").get("prompts") or {})
        text = (prompts.get(key) or "").strip()
        return text or default
    except Exception:
        return default


def research_enabled() -> bool:
    return os.getenv("KEIRA_CLAUDE_RESEARCH", "true").lower() not in ("0", "false", "no")


def critic_enabled() -> bool:
    return os.getenv("KEIRA_CLAUDE_CRITIC", "true").lower() not in ("0", "false", "no")


def build_research_packet(intel: dict, *, lead: dict | None = None) -> str:
    """Assemble an analyst research packet from company intelligence."""
    lead = lead or {}
    co = intel.get("company") or {}
    own = intel.get("ownership") or {}
    geo = intel.get("geo") or {}
    size = intel.get("size") or {}
    succ = intel.get("succession") or {}
    quality = intel.get("business_quality") or {}

    evidence_lines = []
    for s in succ.get("signals") or []:
        evidence_lines.append(
            f"- [{s.get('tier')}] {s.get('type')}: {s.get('evidence') or ''} "
            f"(source={s.get('source')}, conf={s.get('confidence')})"
        )
    counters = []
    for c in succ.get("counter_signals") or []:
        counters.append(f"- {c.get('type')}: {c.get('evidence') or ''}")

    contradictions = []
    emp_a = co.get("employees")
    emp_b = lead.get("employee_count")
    if emp_a and emp_b and str(emp_a) != str(emp_b):
        contradictions.append(f"Employees: intel={emp_a} vs lead={emp_b}")
    title_a = own.get("title")
    title_b = lead.get("contact_title")
    if title_a and title_b and str(title_a).lower() != str(title_b).lower():
        contradictions.append(f"Title: intel={title_a} vs lead={title_b}")

    sources = []
    if lead.get("source"):
        sources.append(f"[1] Provider: {lead.get('source')}")
    if co.get("domain"):
        sources.append(f"[2] Domain: {co.get('domain')}")
    if intel.get("evidence_text"):
        sources.append("[3] Extracted text / signal blob")

    packet = f"""COMPANY
Name: {co.get('name')}
Domain: {co.get('domain') or 'unknown'}
HQ / geo: {co.get('hq') or geo.get('strong_city') or 'unknown'}
Geo score: {geo.get('score')} reject={geo.get('reject')} reasons={geo.get('reasons')}
Industry: {co.get('industry') or 'unknown'}
Founded: {co.get('founded') or 'unknown'}
Employees: {co.get('employees') or 'unknown'} ({size.get('reason')})
Ownership type: {co.get('ownership_type') or own.get('ownership_type')}
Business quality: {quality.get('score')}/10 {quality.get('reasons')}

OWNERSHIP
Person: {own.get('owner_name') or 'unknown'}
Title: {own.get('title') or 'unknown'}
Founder: {own.get('founder')}
Ownership confidence: {own.get('ownership_confidence')}
Reject: {own.get('reject')} — {own.get('reason')}

SOURCES
{chr(10).join(sources) or '(none listed)'}

EVIDENCE / SUCCESSION SIGNALS
{chr(10).join(evidence_lines) or '(none)'}

COUNTER-SIGNALS
{chr(10).join(counters) or '(none)'}

RAW EVIDENCE TEXT
{(intel.get('evidence_text') or '')[:1800]}

CONTRADICTIONS
{chr(10).join(contradictions) or '(none flagged)'}

TASK
Determine whether this is a credible 6–24 month confidential M&A outreach opportunity
for Keira (Eastern Ontario private owner-operated, ~$10–100M). Prefer research_further
or reject over weak qualify. Family-owned alone is insufficient.
"""
    return packet.strip()


def run_succession_analyst(
    intel: dict,
    *,
    lead: dict | None = None,
    use_llm: bool = True,
) -> dict:
    """
    Claude (or configured LLM) succession / ownership synthesis.
    Returns structured analyst result; on failure returns heuristic passthrough.
    """
    succ = dict(intel.get("succession") or {})
    fallback = {
        "succession_probability": succ.get("succession_probability"),
        "time_horizon": succ.get("time_horizon") or "unknown",
        "signals": succ.get("signals") or [],
        "counter_signals": succ.get("counter_signals") or [],
        "recommendation": succ.get("recommendation") or "research_further",
        "why_now": None,
        "why_not": [],
        "ownership_assessment": None,
        "mna_thesis": None,
        "mode": "heuristic",
    }

    if not use_llm or not research_enabled():
        return fallback

    packet = build_research_packet(intel, lead=lead)
    prompt = f"""{packet}

Return JSON:
{{
  "succession_probability": 0.0-1.0,
  "time_horizon": "6-24_months" | "24+_months" | "unknown",
  "signals": [{{"type": "...", "strength": 0-1, "evidence": "...", "source": "...", "confidence": 0-1, "tier": 0-4}}],
  "counter_signals": [{{"type": "...", "evidence": "..."}}],
  "recommendation": "outreach_candidate" | "research_further" | "watchlist" | "reject",
  "why_now": "string or null",
  "why_not": ["..."],
  "ownership_assessment": "founder_owned|family_owned|privately_owned|subsidiary|pe|public|unknown",
  "owner_is_credible": true/false,
  "geo_is_credible": true/false,
  "mna_thesis": "2-4 sentence thesis or null",
  "key_uncertainty": "..."
}}
"""
    try:
        from .llm import chat_json
        from .llm_optimize import truncate_context

        result = chat_json(
            truncate_context(prompt, 5500) or prompt,
            system=_prompt_override("analyst_system", ANALYST_SYSTEM),
            task="keira_analyst",
            max_tokens=1600,
            temperature=0.15,
        )
        result["mode"] = "claude"
        # Preserve heuristic signals if model returned empty
        if not result.get("signals"):
            result["signals"] = fallback["signals"]
        if result.get("succession_probability") is None:
            result["succession_probability"] = fallback["succession_probability"]
        return result
    except Exception as e:
        logger.warning("Keira succession analyst failed: %s", e)
        fallback["error"] = str(e)
        return fallback


def run_lead_critic(
    *,
    intel: dict,
    qualification: dict,
    thesis: dict | None = None,
    draft: str | None = None,
    use_llm: bool = True,
    enrich_min_tier: int = 2,
    draft_min_tier: int = 3,
) -> dict:
    """
    Critic gate before Seamless credits / outreach draft.

    Two-tier:
    - approved_for_enrich: owner+geo+private+size + succession tier >= enrich_min_tier
    - approved_for_draft: enrich bar + succession tier >= draft_min_tier + why_now
    `approved` is an alias for approved_for_enrich (credit spend gate).
    """
    heuristic = _heuristic_critic(
        intel,
        qualification,
        thesis=thesis,
        draft=draft,
        enrich_min_tier=enrich_min_tier,
        draft_min_tier=draft_min_tier,
    )
    if not use_llm or not critic_enabled():
        return {**heuristic, "mode": "heuristic"}

    payload = {
        "company": (intel.get("company") or {}),
        "ownership": (intel.get("ownership") or {}),
        "geo": (intel.get("geo") or {}),
        "succession": {
            "probability": (intel.get("succession") or {}).get("succession_probability"),
            "max_tier": (intel.get("succession") or {}).get("max_tier"),
            "signals": (intel.get("succession") or {}).get("signals"),
        },
        "qualification": {
            "score": qualification.get("score"),
            "icp_fit": qualification.get("icp_fit"),
            "timing": qualification.get("timing"),
            "outreach_ok": qualification.get("outreach_ok"),
            "why_now": qualification.get("why_now"),
            "why_not": qualification.get("why_not"),
            "rejected": qualification.get("rejected"),
        },
        "mna_thesis": thesis,
        "draft_excerpt": (draft or "")[:800] or None,
        "tiers": {"enrich_min": enrich_min_tier, "draft_min": draft_min_tier},
    }
    prompt = f"""Review this Keira lead with a TWO-TIER critic.

LEAD JSON:
{json.dumps(payload, default=str)[:4500]}

Hard reject if owner/geo/private/size fail.
Soft ENRICH approve if owner+geo+private+size pass and succession tier >= {enrich_min_tier}
  (tier 1 longevity/2nd-gen is enough to spend email research credits).
Hard DRAFT approve only if succession tier >= {draft_min_tier} AND credible why_now
  (not family-owned alone).

Return JSON:
{{
  "approved_for_enrich": true/false,
  "approved_for_draft": true/false,
  "approved": true/false,
  "issues": ["..."],
  "checklist": {{"owner": true, "geo": true, "private": true, "size": true, "succession": true, "supported": true, "discreet": true, "why_now": true, "reputation": true, "spend_credits": true}},
  "recommendation": "enrich_and_draft" | "enrich" | "research_required" | "reject"
}}
Set approved == approved_for_enrich.
recommendation=enrich_and_draft only when approved_for_draft; enrich when soft-only.
"""
    try:
        from .llm import chat_json
        from .llm_optimize import truncate_context

        result = chat_json(
            truncate_context(prompt, 5000) or prompt,
            system=_prompt_override("critic_system", CRITIC_SYSTEM),
            task="keira_critic",
            max_tokens=500,
            temperature=0.1,
        )
        result["mode"] = "claude"
        # Normalize two-tier fields
        if "approved_for_enrich" not in result:
            result["approved_for_enrich"] = bool(
                result.get("approved") or result.get("recommendation") in ("enrich", "enrich_and_draft")
            )
        if "approved_for_draft" not in result:
            result["approved_for_draft"] = bool(
                result.get("recommendation") == "enrich_and_draft"
            ) or (
                bool(result.get("approved")) and bool((result.get("checklist") or {}).get("succession"))
            )
        result["approved"] = bool(result.get("approved_for_enrich"))
        if result.get("approved_for_draft"):
            result["recommendation"] = "enrich_and_draft"
        elif result.get("approved_for_enrich") and result.get("recommendation") not in ("reject",):
            result["recommendation"] = "enrich"

        # Hard fail if heuristic already rejects — critic cannot override geo/owner hard fails
        if heuristic.get("hard_reject"):
            result["approved"] = False
            result["approved_for_enrich"] = False
            result["approved_for_draft"] = False
            issues = list(result.get("issues") or [])
            issues.extend(heuristic.get("issues") or [])
            result["issues"] = list(dict.fromkeys(issues))
            result["recommendation"] = "reject"
            result["hard_reject"] = True
        else:
            # Never let LLM soft-approve below heuristic enrich floor without owner/geo
            if not heuristic.get("approved_for_enrich") and result.get("approved_for_enrich"):
                # Allow LLM to be stricter, not looser on hard checklist — except succession soft
                if not (
                    heuristic.get("checklist", {}).get("owner")
                    and heuristic.get("checklist", {}).get("geo")
                    and heuristic.get("checklist", {}).get("private")
                    and heuristic.get("checklist", {}).get("size")
                ):
                    result["approved_for_enrich"] = False
                    result["approved"] = False
                    result["approved_for_draft"] = False
                    result["recommendation"] = heuristic.get("recommendation") or "research_required"
            # Draft still cannot beat heuristic draft floor when succession weak
            if result.get("approved_for_draft") and not heuristic.get("approved_for_draft"):
                if int((intel.get("succession") or {}).get("max_tier") or 0) < draft_min_tier:
                    result["approved_for_draft"] = False
                    if result.get("approved_for_enrich"):
                        result["recommendation"] = "enrich"
        result.setdefault("enrich_min_tier", enrich_min_tier)
        result.setdefault("draft_min_tier", draft_min_tier)
        return result
    except Exception as e:
        logger.warning("Keira critic failed: %s", e)
        return {**heuristic, "mode": "heuristic", "error": str(e)}


def _heuristic_critic(
    intel: dict,
    qualification: dict,
    *,
    thesis: dict | None = None,
    draft: str | None = None,
    enrich_min_tier: int = 2,
    draft_min_tier: int = 3,
) -> dict:
    issues: list[str] = []
    hard = False
    own = intel.get("ownership") or {}
    geo = intel.get("geo") or {}
    succ = intel.get("succession") or {}
    size = intel.get("size") or {}
    max_tier = int(succ.get("max_tier") or 0)
    owner_conf = float(own.get("ownership_confidence") or 0)
    strong_geo = bool(geo.get("strong_city") or geo.get("score", 0) >= 18)

    checklist = {
        "owner": owner_conf >= 0.75 and not own.get("reject"),
        "geo": bool(geo.get("pass")) and not geo.get("reject") and strong_geo,
        "private": not own.get("reject") and (own.get("ownership_type") or "") not in (
            "public", "subsidiary", "private_equity", "nonprofit", "government",
        ),
        "size": bool(size.get("pass", True)),
        "succession": max_tier >= draft_min_tier,
        "succession_enrich": max_tier >= enrich_min_tier,
        "supported": bool(qualification.get("why_now")) and max_tier >= enrich_min_tier,
        "discreet": True,
        "why_now": bool(qualification.get("why_now")),
        "reputation": True,
        "spend_credits": not qualification.get("rejected"),
        "score": int(qualification.get("score") or 0) >= 70,
    }

    if draft:
        lower = draft.lower()
        if any(w in lower for w in ("seamless", "apollo", "we noticed you are selling", "for sale listing")):
            checklist["discreet"] = False
            issues.append("Draft reveals source or implies active sale")

    if not checklist["owner"]:
        issues.append("Owner not credible (need ≥0.75 confidence)")
        hard = True
    if not checklist["geo"]:
        issues.append("Geography fail — need strong Eastern Ontario city")
        hard = True
    if not checklist["private"]:
        issues.append("Ownership type rejected")
        hard = True
    if not checklist["size"]:
        issues.append("Size fail")
        hard = True
    if not checklist["succession_enrich"]:
        issues.append(f"Succession evidence below enrich tier {enrich_min_tier}")
    if not checklist["succession"]:
        issues.append(f"Succession evidence below draft tier {draft_min_tier}")
    if not checklist["why_now"]:
        issues.append("No credible why-now")
    if not checklist["score"]:
        issues.append("Score below strong-lead floor (70)")

    # Soft enrich: owner+strong geo+private+size + succession tier >= enrich_min (default 2)
    # Hard draft: also succession tier >= draft_min (default 3) + why_now
    base_ok = (
        checklist["owner"]
        and checklist["geo"]
        and checklist["private"]
        and checklist["size"]
        and checklist["discreet"]
        and checklist["reputation"]
        and checklist["spend_credits"]
        and checklist["score"]
        and not qualification.get("rejected")
    )
    approved_for_enrich = base_ok and checklist["succession_enrich"] and not hard
    approved_for_draft = (
        approved_for_enrich
        and checklist["succession"]
        and checklist["why_now"]
        and checklist["supported"]
    )

    if hard:
        rec = "reject"
    elif approved_for_draft:
        rec = "enrich_and_draft"
    elif approved_for_enrich:
        rec = "enrich"
    else:
        rec = "research_required"

    return {
        "approved": approved_for_enrich,  # backward-compat: approved ⇒ may spend enrich credits
        "approved_for_enrich": approved_for_enrich,
        "approved_for_draft": approved_for_draft,
        "issues": issues,
        "checklist": checklist,
        "recommendation": rec,
        "hard_reject": hard,
        "mode": "heuristic",
        "enrich_min_tier": enrich_min_tier,
        "draft_min_tier": draft_min_tier,
        "max_tier": max_tier,
    }


def merge_analyst_into_intel(intel: dict, analyst: dict) -> dict:
    """Fold Claude analyst output into company intelligence object."""
    out = dict(intel)
    succ = dict(out.get("succession") or {})
    if analyst.get("succession_probability") is not None:
        # Blend heuristic + Claude (Claude 60% when present)
        h = float(succ.get("succession_probability") or 0)
        c = float(analyst["succession_probability"])
        succ["succession_probability"] = round(0.4 * h + 0.6 * c, 2)
    if analyst.get("signals"):
        # Prefer higher-tier union
        by_type: dict[str, dict] = {}
        for s in list(succ.get("signals") or []) + list(analyst["signals"]):
            t = str(s.get("type") or s.get("evidence") or id(s))
            prev = by_type.get(t)
            if not prev or int(s.get("tier") or 0) >= int(prev.get("tier") or 0):
                by_type[t] = s
        succ["signals"] = list(by_type.values())
        succ["max_tier"] = max((int(s.get("tier") or 0) for s in succ["signals"]), default=0)
    if analyst.get("counter_signals"):
        succ["counter_signals"] = list(succ.get("counter_signals") or []) + list(
            analyst["counter_signals"]
        )
    if analyst.get("recommendation"):
        succ["recommendation"] = analyst["recommendation"]
    if analyst.get("time_horizon"):
        succ["time_horizon"] = analyst["time_horizon"]
    succ["analyst_mode"] = analyst.get("mode")
    out["succession"] = succ

    if analyst.get("ownership_assessment"):
        own = dict(out.get("ownership") or {})
        own["ownership_type"] = analyst["ownership_assessment"]
        if analyst.get("owner_is_credible") is False:
            own["reject"] = True
            own["reason"] = "Claude analyst: owner not credible"
        out["ownership"] = own

    out["analyst"] = {
        "why_now": analyst.get("why_now"),
        "why_not": analyst.get("why_not") or [],
        "mna_thesis": analyst.get("mna_thesis"),
        "key_uncertainty": analyst.get("key_uncertainty"),
        "mode": analyst.get("mode"),
    }
    return out
