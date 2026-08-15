"""Unified contact search — pluggable people-data providers.

Providers:
  - apollo  Apollo.io. Person SEARCH is free (no credits, 230M+ contacts).
  - pdl     People Data Labs. 1 credit per contact returned (100 free/month).

Emails are looked up on demand via Hunter.io (dashboard button) when a contact
has no email on file. Not guaranteed; uses 1 Hunter search credit per attempt.

Selection: CONTACTS_PROVIDER env (apollo|pdl), or auto-detect:
APOLLO_API_KEY set → apollo, else pdl.
"""

import logging
import os
from pathlib import Path

import yaml

from .db import build_prospect_string, company_seen, get_connection, init_db, upsert_lead

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"


class ContactsError(Exception):
    pass


def resolve_contacts_provider() -> str:
    provider = (os.getenv("CONTACTS_PROVIDER") or "").strip().lower()
    if provider not in ("apollo", "pdl", ""):
        raise ContactsError(f"Unknown CONTACTS_PROVIDER '{provider}'. Use apollo or pdl.")
    if provider:
        return provider
    if os.getenv("APOLLO_API_KEY"):
        return "apollo"
    return "pdl"


def contacts_status() -> dict:
    """Health info for the dashboard."""
    provider = resolve_contacts_provider()
    key_env = "APOLLO_API_KEY" if provider == "apollo" else "PDL_API_KEY"
    from .hunter_api import hunter_available

    return {
        "provider": provider,
        "configured": bool(os.getenv(key_env)),
        "email_finder": hunter_available(),
    }


def search_and_import_contacts(
    agent: str = "woodway",
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Search the active provider for ICP-fit contacts and save to leads.db."""
    provider = resolve_contacts_provider()
    emit = on_progress or (lambda msg: None)

    if provider == "apollo":
        return _apollo_search_and_import(agent, limit=limit, skip_existing=skip_existing, emit=emit)

    from .pdl import search_and_import

    emit(f"Searching People Data Labs (up to {limit} contacts, 1 credit each)…")
    result = search_and_import(agent, limit=limit, skip_existing=skip_existing)
    result["provider"] = "pdl"
    return result


# ---------------------------------------------------------------- Apollo


def _agent_config(agent: str) -> dict:
    with open(AGENTS_DIR / agent / "config.yaml") as f:
        return yaml.safe_load(f)


def _apollo_filters(agent: str) -> dict:
    cfg = _agent_config(agent)
    apollo = cfg.get("apollo") or {}
    icp = cfg.get("icp") or {}
    return {
        "person_titles": apollo.get("person_titles") or icp.get("titles") or [],
        "person_seniorities": apollo.get("person_seniorities") or [],
        "person_locations": apollo.get("person_locations") or [],
        "organization_locations": apollo.get("organization_locations") or [],
        "organization_num_employees_ranges": apollo.get("employee_ranges") or [],
        "q_keywords": apollo.get("keywords"),
    }


def _apollo_lead(person: dict) -> dict | None:
    org = person.get("organization") or {}
    company = (org.get("name") or "").strip()
    if not company:
        return None

    name = (person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}").strip()
    title = (person.get("title") or "").strip() or None
    domain = (org.get("primary_domain") or org.get("website_url") or "").strip() or None
    if domain:
        domain = domain.replace("https://", "").replace("http://", "").split("/")[0].removeprefix("www.")

    employee_count = org.get("estimated_num_employees")

    return {
        "company": company,
        "contact_name": name or None,
        "contact_title": title,
        "email": None,  # filled by the email waterfall
        "linkedin_url": (person.get("linkedin_url") or "").strip() or None,
        "employee_count": int(employee_count) if employee_count else None,
        "industry": (org.get("industry") or "").strip() or None,
        "signal": f"Apollo match — {title or 'contact'} at {company}",
        "source": "apollo",
        "status": "imported",
        "_apollo_id": person.get("id"),
        "_first_name": person.get("first_name"),
        "_last_name": person.get("last_name"),
        "_domain": domain,
    }


def _split_name(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = full_name.strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[-1]


def hunter_research_lead(lead_id: int) -> dict:
    """On-demand Hunter email lookup for one lead. Costs 1 Hunter search credit."""
    from .db import get_lead, set_lead_fields
    from .hunter_api import find_email, hunter_available

    if not hunter_available():
        raise ContactsError("Hunter.io is not configured — add HUNTER_API_KEY to .env")

    lead = get_lead(lead_id)
    if not lead:
        raise ContactsError("lead not found")

    email = (lead.get("email") or "").strip()
    if email and "@" in email:
        return {
            "found": True,
            "already_had_email": True,
            "email": email,
            "message": "Lead already has an email address.",
        }

    name = (lead.get("contact_name") or "").strip()
    if not name:
        raise ContactsError("Lead has no contact name — Hunter needs a person to search.")

    first, last = _split_name(name)
    if not first or not last:
        raise ContactsError("Contact name needs a first and last name for Hunter lookup.")

    company = (lead.get("company") or "").strip()
    if not company:
        raise ContactsError("Lead has no company for Hunter lookup.")

    found = find_email(first, last, company=company)
    if not found:
        return {
            "found": False,
            "message": "Hunter could not find a reliable email for this contact. LinkedIn may still be your best option.",
        }

    set_lead_fields(lead_id, email=found["email"])
    return {
        "found": True,
        "email": found["email"],
        "score": found["score"],
        "verified": found.get("verified"),
        "message": f"Found {found['email']} (confidence {found['score']}%)",
    }


def try_find_email(lead: dict, *, on_progress=None) -> str | None:
    """Hunter-only email lookup for in-memory lead dicts (used by on-demand research)."""
    from .hunter_api import find_email, hunter_available

    emit = on_progress or (lambda msg: logger.info(msg))
    if not hunter_available():
        return None
    first = lead.get("_first_name") or _split_name(lead.get("contact_name"))[0]
    last = lead.get("_last_name") or _split_name(lead.get("contact_name"))[1]
    if not (first and last):
        return None
    found = find_email(
        first,
        last,
        domain=lead.get("_domain"),
        company=lead.get("company"),
    )
    if found:
        emit(f"  Hunter found email for {lead.get('contact_name')} (score {found['score']})")
        return found["email"]
    return None


def _apollo_search_and_import(agent: str, *, limit: int, skip_existing: bool, emit) -> dict:
    from .apollo_api import ApolloClient

    client = ApolloClient()
    filters = _apollo_filters(agent)

    emit(f"Searching Apollo (free search, up to {limit} contacts)…")

    people: list[dict] = []
    page = 1
    total_available = 0
    while len(people) < limit and page <= 5:
        result = client.search_people(per_page=min(limit - len(people), 100), page=page, **filters)
        batch = result.get("people") or []
        pagination = result.get("pagination") or {}
        total_available = pagination.get("total_entries") or total_available
        people.extend(batch)
        if not batch or page >= (pagination.get("total_pages") or 1):
            break
        page += 1

    emit(f"{len(people)} contacts found ({total_available:,} total matches in Apollo)")

    imported = updated = skipped = 0
    with_email = 0
    errors: list[str] = []

    init_db()
    for person in people[:limit]:
        try:
            lead = _apollo_lead(person)
            if not lead:
                skipped += 1
                continue

            if skip_existing and _contact_seen(lead, agent):
                skipped += 1
                continue

            # Email lookup is on-demand per lead (Hunter button in dashboard).
            record = {k: v for k, v in lead.items() if not k.startswith("_")}
            record["prospect"] = build_prospect_string(record)
            _, created = upsert_lead(record, agent=agent)
            if created:
                imported += 1
            else:
                updated += 1
        except Exception as e:
            errors.append(str(e))

    return {
        "provider": "apollo",
        "total_available": total_available,
        "searched": len(people),
        "credits_used": 0,  # Apollo search is free; Hunter credits tracked separately
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "with_email": with_email,
        "errors": errors,
    }


def search_and_import_contacts_for_companies(
    agent: str,
    company_names: list[str],
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Find ICP contacts at specific companies — Apollo first (free search), PDL if configured."""
    emit = on_progress or (lambda msg: None)
    names = [n.strip() for n in company_names if n and n.strip()]
    if not names:
        return {
            "provider": resolve_contacts_provider(),
            "imported": 0,
            "updated": 0,
            "skipped": True,
            "with_email": 0,
            "errors": [],
        }

    provider = resolve_contacts_provider()

    if os.getenv("APOLLO_API_KEY"):
        try:
            return _apollo_search_for_companies(
                agent, names, limit=limit, skip_existing=skip_existing, emit=emit
            )
        except ContactsError as e:
            if provider == "apollo":
                raise
            emit(f"Apollo unavailable ({e}) — trying PDL")

    if provider == "pdl" and os.getenv("PDL_API_KEY"):
        from .pdl import search_and_import
        from .pdl_api import PDLError

        emit(f"Searching PDL (up to {limit} contacts, 1 credit each)…")
        try:
            result = search_and_import(agent, limit=limit, skip_existing=skip_existing)
        except PDLError as e:
            if e.status_code == 402 and os.getenv("APOLLO_API_KEY"):
                raise ContactsError(
                    "PDL credits exhausted — set CONTACTS_PROVIDER=apollo in .env (Apollo search is free)"
                ) from e
            raise ContactsError(str(e)) from e
        result["provider"] = "pdl"
        result["fallback"] = True
        return result

    raise ContactsError(
        "No contact provider available — set APOLLO_API_KEY (free search) or wait for PDL credits to reset"
    )


def _apollo_search_for_companies(
    agent: str,
    company_names: list[str],
    *,
    limit: int,
    skip_existing: bool,
    emit,
) -> dict:
    from .apollo_api import ApolloClient, ApolloError

    client = ApolloClient()
    filters = _apollo_filters(agent)
    titles = filters.get("person_titles") or []

    emit(f"Apollo search at {len(company_names)} companies (free, no PDL credits)…")
    people: list[dict] = []
    seen_ids: set[str] = set()

    for company in company_names:
        if len(people) >= limit:
            break
        try:
            result = client.search_people(
                person_titles=titles or None,
                person_seniorities=filters.get("person_seniorities") or None,
                organization_num_employees_ranges=filters.get("organization_num_employees_ranges") or None,
                q_organization_name=company,
                per_page=min(5, limit - len(people)),
            )
        except ApolloError as e:
            logger.warning("Apollo search failed for %s: %s", company, e)
            continue
        for person in result.get("people") or []:
            pid = person.get("id")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            people.append(person)
            if len(people) >= limit:
                break

    emit(f"{len(people)} contacts found via Apollo")

    imported = updated = skipped = 0
    errors: list[str] = []
    init_db()
    for person in people[:limit]:
        try:
            lead = _apollo_lead(person)
            if not lead:
                skipped += 1
                continue
            if skip_existing and _contact_seen(lead, agent):
                skipped += 1
                continue
            record = {k: v for k, v in lead.items() if not k.startswith("_")}
            record["prospect"] = build_prospect_string(record)
            _, created = upsert_lead(record, agent=agent)
            if created:
                imported += 1
            else:
                updated += 1
        except Exception as e:
            errors.append(str(e))

    if not people and not os.getenv("APOLLO_API_KEY"):
        raise ContactsError("APOLLO_API_KEY not set")

    return {
        "provider": "apollo",
        "searched": len(people),
        "credits_used": 0,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "with_email": 0,
        "errors": errors,
        "companies": len(company_names),
    }


def _contact_seen(lead: dict, agent: str) -> bool:
    """A contact is a duplicate if the same person already exists at that company."""
    if not lead.get("contact_name"):
        return company_seen(lead["company"], agent=agent)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE agent = ? AND contact_name = ? AND company = ?",
            (agent, lead["contact_name"], lead["company"]),
        ).fetchone()
    return row is not None
