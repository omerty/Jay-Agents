"""Shared lead processing pipeline."""

import json
import logging

from .db import build_prospect_string, upsert_lead
from .enrich import agent_size_score_boost, enrich_company, format_enrichment
from .llm import LLMError
from .outreach import generate_outreach
from .qualify import qualify_prospect
from .research import format_research, research_prospect

logger = logging.getLogger(__name__)


def process_lead(
    config: dict,
    lead: dict,
    *,
    use_llm: bool = True,
    use_research: bool = True,
    draft_outreach: bool = True,
    agent_name: str = "woodway",
    save: bool = True,
) -> dict:
    """Enrich → research → qualify → outreach → save."""
    company = lead["company"]
    prospect = lead.get("prospect") or build_prospect_string(lead)
    employee_count = lead.get("employee_count")

    enrichment = enrich_company(company)
    enrichment_text = format_enrichment(enrichment, employee_count)

    research_results = []
    research_context = None
    if use_research:
        research_results = research_prospect(
            prospect, company=company, keywords=config.get("research_keywords")
        )
        research_context = format_research(research_results)
        if enrichment_text:
            research_context = f"{enrichment_text}\n\n{research_context}"

    qualification = qualify_prospect(
        config,
        prospect,
        use_llm=use_llm,
        research_context=research_context,
        employee_count=employee_count,
        company=company,
    )

    boost = agent_size_score_boost(employee_count, config)
    if boost:
        qualification["score"] = max(0, min(100, qualification["score"] + boost))
        label = "Company size boost" if boost > 0 else "Company size adjustment"
        qualification["reasons"].append(f"{label}: {employee_count:,} employees ({boost:+d})")
        qualification["tier"] = (
            "hot" if qualification["score"] >= 75
            else "warm" if qualification["score"] >= 50
            else "cold"
        )

    contact = {
        "contact_name": lead.get("contact_name"),
        "contact_title": lead.get("contact_title"),
        "email": lead.get("email"),
        "company": company,
    }

    outreach = None
    if draft_outreach and qualification["score"] >= 50:
        try:
            outreach = generate_outreach(
                config,
                prospect,
                qualification,
                use_llm=use_llm,
                research_context=research_context,
                contact=contact,
            )
        except LLMError as e:
            logger.warning("Outreach LLM failed for %s — using template fallback: %s", company, e)
            outreach = generate_outreach(
                config,
                prospect,
                qualification,
                use_llm=False,
                research_context=research_context,
                contact=contact,
            )
            outreach["mode"] = "fallback"

    status = lead.get("status", "discovered")
    if outreach:
        status = "drafted"
    elif qualification["score"] >= 50:
        status = "qualified"
    elif status not in ("emailed", "replied", "skipped"):
        status = "discovered"

    record = {
        **lead,
        "prospect": prospect,
        "score": qualification["score"],
        "tier": qualification["tier"],
        "outreach_subject": outreach["subject"] if outreach else None,
        "outreach_body": outreach["body"] if outreach else None,
        "qualification_json": json.dumps({
            "reasons": qualification.get("reasons") or [],
            "talking_points": qualification.get("talking_points") or [],
            "recommendation": qualification.get("recommendation") or "",
            "mode": qualification.get("mode"),
            "confidence": qualification.get("confidence"),
            "estimated_company_size": qualification.get("estimated_company_size"),
            "ownership_confidence": qualification.get("ownership_confidence"),
            "succession_signals": qualification.get("succession_signals"),
        }),
        "status": status,
        "source": lead.get("source", "discover"),
    }

    lead_id = None
    if save:
        lead_id, _ = upsert_lead(record, agent=agent_name)

    return {
        "lead_id": lead_id,
        "prospect": prospect,
        "contact": contact,
        "enrichment": enrichment,
        "research": research_results,
        "research_context": research_context,
        "qualification": qualification,
        "outreach": outreach,
    }
