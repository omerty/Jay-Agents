"""Shared lead processing pipeline."""

import json
import logging

from .contact_tier import classify_contact_tier, tier_allows_email_draft
from .db import build_prospect_string, is_suppressed, record_outcome, upsert_lead
from .enrich import agent_size_score_boost, enrich_company, format_enrichment, mega_brand_demote_penalty
from .llm import LLMError
from .outreach import generate_outreach, generate_linkedin_note
from .qualify import qualify_prospect
from .research import format_research, research_prospect
from .signals import enrich_lead_priority
from .sequences import generate_sequence, save_sequence_steps

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

    from .junk_contacts import is_junk_contact

    if is_junk_contact(lead):
        if save and lead.get("id"):
            from .db import set_lead_fields
            set_lead_fields(lead["id"], status="skipped")
        return {
            "lead_id": lead.get("id"),
            "prospect": prospect,
            "contact": lead,
            "qualification": {
                "score": 0,
                "tier": "cold",
                "reasons": ["Junk / role-inbox contact — skipped"],
            },
            "outreach": None,
            "skipped": True,
        }

    # Company-only rows (no person yet) — skip expensive LLM steps
    if not lead.get("contact_name") and lead.get("source") in ("actava", "discover"):
        from .db import mark_company_awaiting_contact

        if save and lead.get("company"):
            mark_company_awaiting_contact(agent_name, lead["company"])
        return {
            "lead_id": lead.get("id"),
            "prospect": prospect,
            "contact": {"company": company},
            "qualification": {"score": 0, "tier": "cold", "reasons": ["Awaiting contact discovery"]},
            "outreach": None,
            "skipped": True,
        }

    enrichment = enrich_company(company)
    enrichment_text = format_enrichment(enrichment, employee_count)

    from .privacy_footprint import fetch_privacy_footprint, footprint_qualification_boost

    domain = lead.get("company_domain") or enrichment.get("domain")
    footprint = fetch_privacy_footprint(company, domain=domain)
    if footprint:
        fp_line = f"Privacy policy: DPO={footprint.get('dpo_name') or 'n/a'}, "
        fp_line += f"de-id signal={'yes' if footprint.get('mentions_deidentification') else 'no'}"
        enrichment_text = f"{enrichment_text}\n{fp_line}".strip() if enrichment_text else fp_line

    research_results = []
    research_context = None
    if use_research:
        research_results = research_prospect(
            prospect, company=company, keywords=config.get("research_keywords")
        )
        research_context = format_research(research_results)
        if enrichment_text:
            research_context = f"{enrichment_text}\n\n{research_context}"

    # Inject strongest unconsumed trigger signal into research context
    trigger_signal = None
    try:
        from .signals import best_unconsumed_signal

        trigger_signal = best_unconsumed_signal(
            company, agent=agent_name, domain=domain,
        )
        if trigger_signal:
            sig_line = (
                f"TRIGGER SIGNAL ({trigger_signal.get('label') or trigger_signal.get('signal_type')}): "
                f"{(trigger_signal.get('snippet') or '')[:240]} "
                f"src={trigger_signal.get('source_url') or ''}"
            )
            research_context = f"{sig_line}\n\n{research_context or ''}".strip()
    except Exception:
        trigger_signal = None

    # Account brief — retrieval artifact before writing
    account_brief = None
    try:
        from .account_brief import assemble_account_brief

        account_brief = assemble_account_brief(
            company, agent=agent_name, domain=domain,
        )
        brief_text = (account_brief or {}).get("brief_text") or ""
        if brief_text and "thin brief" not in brief_text:
            research_context = f"{brief_text}\n\n{research_context or ''}".strip()
    except Exception as e:
        logger.debug("Account brief skipped: %s", e)
        account_brief = None

    # Keira: dimensional M&A intelligence is the primary qualifier
    if agent_name == "keira":
        from .keira_budget import research_priority
        from .keira_intelligence import (
            build_company_intelligence,
            build_mna_thesis,
            intelligence_to_qualification_result,
            qualify_keira_intelligence,
        )

        intel = build_company_intelligence(lead, research_text=research_context)
        keira_q = qualify_keira_intelligence(intel, config=config)
        keira_q.update(research_priority(keira_q, intel))
        thesis = build_mna_thesis(intel, keira_q)
        qualification = intelligence_to_qualification_result(intel, keira_q)
        # Persist inferred title/city/employees back onto the lead for save
        if intel.get("ownership", {}).get("title") and not lead.get("contact_title"):
            lead = {**lead, "contact_title": intel["ownership"]["title"]}
        if intel.get("company", {}).get("employees") and not lead.get("employee_count"):
            lead = {**lead, "employee_count": intel["company"]["employees"]}
        if intel.get("company", {}).get("hq") and not lead.get("company_city"):
            lead = {**lead, "company_city": intel["company"]["hq"]}
        # Attach thesis for outreach
        qualification["mna_thesis"] = thesis
        qualification["why_now"] = keira_q.get("why_now")
        qualification["why_not"] = keira_q.get("why_not")
        if keira_q.get("rejected"):
            qualification["score"] = min(qualification["score"], 25)
            qualification["tier"] = "cold"
        # Skip generic LLM qualify — Keira gates already ran
    else:
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

        demote = mega_brand_demote_penalty(company, config)
        if demote:
            qualification["score"] = max(0, min(100, qualification["score"] + demote))
            qualification["reasons"].append(
                f"Megabrand approachability demote ({demote:+d}) — prefer regional/mid-large peers"
            )

        fp_boost, fp_reason = footprint_qualification_boost(footprint)
        if fp_boost:
            qualification["score"] = max(0, min(100, qualification["score"] + fp_boost))
            if fp_reason:
                qualification["reasons"].append(fp_reason)

        if boost or demote or fp_boost:
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
        "linkedin_url": lead.get("linkedin_url"),
        "contact_tier": lead.get("contact_tier"),
        "email_inferred": lead.get("email_inferred"),
    }

    contact_tier = classify_contact_tier({**lead, **contact})
    priority_score = enrich_lead_priority({**lead, "score": qualification["score"], "contact_tier": contact_tier})

    suppress_reason = is_suppressed(
        email=contact.get("email"), domain=domain, agent=agent_name,
    )
    if suppress_reason:
        return {
            "lead_id": lead.get("id"),
            "prospect": prospect,
            "contact": contact,
            "qualification": qualification,
            "outreach": None,
            "skipped": True,
            "skip_reason": f"suppressed: {suppress_reason}",
        }

    linkedin_note = None
    sequence_steps = []

    outreach = None
    if draft_outreach and qualification["score"] >= 50:
        if tier_allows_email_draft(contact_tier) and contact.get("email"):
            try:
                outreach = generate_outreach(
                    config,
                    prospect,
                    qualification,
                    use_llm=use_llm,
                    research_context=research_context,
                    contact=contact,
                    trigger_signal=trigger_signal,
                    account_brief=account_brief,
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
                    trigger_signal=trigger_signal,
                    account_brief=account_brief,
                )
                outreach["mode"] = "fallback"
            sequence_steps = generate_sequence(
                config, {**lead, **contact, "prospect": prospect},
                qualification,
                touch1=outreach,
                linkedin_note=None,
                use_llm=use_llm,
                research_context=research_context,
            )
            if trigger_signal and trigger_signal.get("id") and lead.get("id"):
                try:
                    from .db import mark_signal_consumed
                    mark_signal_consumed(trigger_signal["id"], lead_id=lead.get("id"))
                except Exception:
                    pass
        elif contact_tier == "C" and contact.get("linkedin_url"):
            linkedin_note = generate_linkedin_note(
                config,
                prospect,
                qualification,
                contact=contact,
                use_llm=use_llm,
                research_context=research_context,
            )
            # Still generate sequence so LinkedIn touch can advance
            sequence_steps = generate_sequence(
                config, {**lead, **contact, "prospect": prospect},
                qualification,
                touch1=None,
                linkedin_note=linkedin_note,
                use_llm=use_llm,
                research_context=research_context,
            )
            if sequence_steps:
                # Mark email touch1 as skipped when no email
                for s in sequence_steps:
                    if s.get("step_number") == 1 and s.get("channel") == "email":
                        s["status"] = "skipped"
            if trigger_signal and trigger_signal.get("id") and lead.get("id"):
                try:
                    from .db import mark_signal_consumed
                    mark_signal_consumed(trigger_signal["id"], lead_id=lead.get("id"))
                except Exception:
                    pass

    status = lead.get("status", "discovered")
    citation_ok = True if not outreach else bool(outreach.get("citation_ok", True))
    if outreach and citation_ok:
        status = "drafted"
    elif outreach and not citation_ok:
        # Keep copy for QA; do not promote to mailbox draft
        status = "qualified"
        logger.info(
            "Holding %s as qualified — citation gate: %s",
            company, (outreach.get("citation_issues") or [])[:2],
        )
    elif linkedin_note and qualification["score"] >= 50:
        status = "qualified"
    elif qualification["score"] >= 50:
        status = "qualified"
    elif status not in ("emailed", "replied", "skipped", "awaiting_contact"):
        status = "discovered"

    record = {
        **lead,
        "prospect": prospect,
        "score": qualification["score"],
        "tier": qualification["tier"],
        "outreach_subject": outreach["subject"] if outreach else None,
        "outreach_body": outreach["body"] if outreach else None,
        "linkedin_note": linkedin_note,
        "contact_tier": contact_tier,
        "company_domain": domain or lead.get("company_domain"),
        "priority_score": priority_score,
        "qualification_json": json.dumps({
            "reasons": qualification.get("reasons") or [],
            "talking_points": qualification.get("talking_points") or [],
            "recommendation": qualification.get("recommendation") or "",
            "mode": qualification.get("mode"),
            "confidence": qualification.get("confidence"),
            "estimated_company_size": qualification.get("estimated_company_size"),
            "ownership_confidence": qualification.get("ownership_confidence"),
            "succession_signals": qualification.get("succession_signals"),
            "citation_ok": citation_ok if outreach else None,
            "citation_issues": (outreach.get("citation_issues") if outreach else None),
            "citations": (outreach.get("citations") if outreach else None),
        }),
        "status": status,
        "source": lead.get("source", "discover"),
    }

    lead_id = None
    if save:
        lead_id, _ = upsert_lead(record, agent=agent_name)
        if lead_id and sequence_steps and citation_ok:
            save_sequence_steps(lead_id, sequence_steps)
        if lead_id and outreach and citation_ok:
            record_outcome(lead_id, "drafted")

    return {
        "lead_id": lead_id,
        "prospect": prospect,
        "contact": contact,
        "enrichment": enrichment,
        "research": research_results,
        "research_context": research_context,
        "qualification": qualification,
        "outreach": outreach,
        "sequence_steps": sequence_steps,
        "priority_score": priority_score,
    }
