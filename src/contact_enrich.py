"""Post-import contact enrichment — email patterns, privacy DPO, tier assignment."""

from __future__ import annotations

import logging

from .contact_tier import classify_contact_tier
from .db import get_connection, init_db, upsert_lead, build_prospect_string
from .email_patterns import infer_email_for_contact, normalize_domain, resolve_company_domain
from .privacy_footprint import contact_from_footprint, fetch_privacy_footprint

logger = logging.getLogger(__name__)


def enrich_lead_contact_fields(lead: dict, company: str) -> dict:
    """Apply pattern inference and tier classification to a lead dict."""
    updated = dict(lead)
    domain = normalize_domain(updated.get("company_domain")) or resolve_company_domain(company)
    if domain:
        updated["company_domain"] = domain

    if not updated.get("email") and updated.get("contact_name"):
        inferred = infer_email_for_contact(updated["contact_name"], company, domain=domain)
        if inferred:
            updated.update(inferred)

    updated["contact_tier"] = classify_contact_tier(updated)
    return updated


def enrich_imported_contacts(
    agent: str,
    company_names: list[str],
    *,
    on_progress=None,
) -> dict:
    """
    For each company:
      1. Keira: site leadership/about emails (never DPO/privacy@)
         Other agents: privacy footprint (DPO email if no contact yet)
      2. Pattern-infer emails for name-only contacts
      3. Assign contact tiers
    """
    emit = on_progress or (lambda msg: None)
    init_db()
    names = [n.strip() for n in company_names if n and n.strip()]
    if not names:
        return {"enriched": 0, "privacy_contacts": 0, "inferred_emails": 0}

    enriched = privacy_contacts = inferred_emails = 0

    from .email_patterns import resolve_pattern
    from .site_emails import match_email_to_name, sweep_company_site

    skip_privacy = (agent or "").strip().lower() == "keira"
    site_emails_filled = 0

    for i, company in enumerate(names, 1):
        emit(f"  Enrich [{i}/{len(names)}] {company}…")
        domain = resolve_company_domain(company)
        # Ensure pattern is cached (free ladder; Hunter only if still missing)
        if domain:
            resolve_pattern(company, domain=domain, allow_hunter=False, on_progress=emit)
        footprint = None
        site_emails: list[str] = []
        if skip_privacy:
            if domain:
                try:
                    site_emails = [
                        h["email"] for h in sweep_company_site(domain, max_pages=6) if h.get("email")
                    ]
                except Exception as e:
                    logger.debug("Keira site sweep failed for %s: %s", company, e)
        else:
            footprint = fetch_privacy_footprint(company, domain=domain)

        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM leads
                WHERE agent = ? AND company = ?
                  AND status IN ('imported', 'awaiting_contact', 'discovered')
                  AND contact_name IS NOT NULL AND TRIM(contact_name) != ''
                """,
                (agent, company),
            ).fetchall()

        if not rows and footprint:
            pf_contact = contact_from_footprint(footprint, company)
            if pf_contact:
                from .junk_contacts import is_junk_contact

                if is_junk_contact(pf_contact):
                    emit(f"  Skip junk privacy contact for {company}: {pf_contact.get('contact_name')}")
                    continue
                record = {
                    "company": company,
                    **pf_contact,
                    "source": "privacy_footprint",
                    "status": "imported",
                }
                record["prospect"] = build_prospect_string(record)
                if domain:
                    record["company_domain"] = domain
                upsert_lead(record, agent=agent)
                privacy_contacts += 1
                enriched += 1
            continue

        for row in rows:
            lead = dict(row)
            from .junk_contacts import is_junk_contact

            if is_junk_contact(lead):
                # Drop role-inbox junk from qualify queue
                with get_connection() as conn:
                    conn.execute(
                        "UPDATE leads SET status = 'skipped', updated_at = datetime('now') WHERE id = ?",
                        (lead["id"],),
                    )
                    conn.commit()
                emit(f"  Skipped junk contact: {lead.get('contact_name')}")
                continue
            before_email = lead.get("email")
            if not lead.get("email") and site_emails and lead.get("contact_name"):
                matched = match_email_to_name(lead["contact_name"], site_emails)
                if matched:
                    lead["email"] = matched
                    lead["email_source"] = "site_sweep"
                    site_emails_filled += 1
            lead = enrich_lead_contact_fields(lead, company)
            if not before_email and lead.get("email"):
                inferred_emails += 1
            if footprint and footprint.get("mentions_deidentification"):
                sig = lead.get("signal") or ""
                if "de-identification" not in sig.lower():
                    lead["signal"] = f"{sig} — privacy policy mentions de-identification".strip(" —")

            upsert_lead({**lead, "status": lead.get("status", "imported")}, agent=agent)
            enriched += 1

    emit(
        f"Contact enrichment — {enriched} updated, "
        f"{privacy_contacts} from privacy policy, {inferred_emails} pattern emails"
        + (f", {site_emails_filled} site emails" if site_emails_filled else "")
    )
    return {
        "enriched": enriched,
        "privacy_contacts": privacy_contacts,
        "inferred_emails": inferred_emails,
        "site_emails": site_emails_filled,
    }
