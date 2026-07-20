"""Seamless.AI contact search + conservative research import.

Strategy (10k monthly credits):
  1. Search contacts — FREE, cast a wide net with Keira ICP filters
  2. Pre-filter blocklist + geography locally
  3. Rank candidates by title/size/founded signals (no credits)
  4. Research only top N per run (default 8) — 1 credit each
  5. Poll and import enriched records (email, phone, LinkedIn)
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .db import build_prospect_string, get_connection, init_db, upsert_lead
from .discover import _should_skip
from .seamless_api import SeamlessError, get_seamless_client, seamless_available, seamless_transport
from .seamless_budget import (
    budget_status,
    can_research,
    max_research_per_run,
    record_credits_used,
)

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"

SIZE_MIDPOINT = {
    "0 - 1 (Self-employed)": 1,
    "2 - 10": 6,
    "11 - 50": 30,
    "51 - 200": 125,
    "201 - 500": 350,
    "501 - 1,000": 750,
    "1,001 - 5,000": 3000,
    "5,001 - 10,000": 7500,
    "10,001+": 15000,
}

OWNER_TITLE_KEYWORDS = (
    "owner",
    "founder",
    "co-founder",
    "president",
    "ceo",
    "chief executive",
    "managing partner",
    "principal",
)

ONTARIO_GEO_KEYWORDS = (
    "ottawa",
    "kingston",
    "cornwall",
    "eastern ontario",
    "ontario",
    "kanata",
    "nepean",
    "orleans",
    "brockville",
    "pembroke",
    "belleville",
)


def _agent_config(agent: str) -> dict:
    with open(AGENTS_DIR / agent / "config.yaml") as f:
        return yaml.safe_load(f)


def seamless_filters(agent: str) -> dict:
    """Build Seamless search body from agent config."""
    cfg = _agent_config(agent)
    seamless = cfg.get("seamless") or {}
    icp = cfg.get("icp") or {}

    titles = seamless.get("job_title") or icp.get("titles") or []
    filters: dict = {
        "contactCountry": seamless.get("contact_country") or ["Canada"],
        "contactState": seamless.get("contact_state") or ["Ontario"],
        "locationType": seamless.get("location_type") or "company",
        "jobTitle": titles[:10],
        "seniority": seamless.get("seniority") or ["C-Level", "VP", "Director"],
        "companySize": seamless.get("company_size") or [
            "11 - 50",
            "51 - 200",
            "201 - 500",
        ],
        "companyRevenue": seamless.get("company_revenue") or [
            "$5M - $20M",
            "$20M - $50M",
            "$50M - $100M",
        ],
        "companyFoundedOn": seamless.get("company_founded_on") or ["10+ Years"],
        "limit": 50,
    }

    industries = seamless.get("industry")
    if industries:
        filters["industry"] = industries[:5]

    keywords = seamless.get("contact_keyword")
    if keywords:
        filters["contactKeyword"] = keywords[:10]

    return filters


def _parse_employee_count(range_str: str | None) -> int | None:
    if not range_str:
        return None
    return SIZE_MIDPOINT.get(range_str.strip())


def _in_target_geography(item: dict) -> bool:
    """Post-filter: Eastern Ontario / Ottawa region within Canada."""
    country = (item.get("companyCountry") or item.get("country") or "").lower()
    if country and "canada" not in country:
        return False

    blob = " ".join(
        str(item.get(k) or "")
        for k in ("companyCity", "companyState", "city", "state", "company", "title")
    ).lower()
    return any(kw in blob for kw in ONTARIO_GEO_KEYWORDS)


def _title_score(title: str | None) -> int:
    if not title:
        return 0
    lower = title.lower()
    if any(kw in lower for kw in OWNER_TITLE_KEYWORDS):
        return 30
    if "president" in lower or "ceo" in lower:
        return 25
    return 5


def _size_score(employee_range: str | None) -> int:
    mid = _parse_employee_count(employee_range)
    if mid is None:
        return 0
    if 20 <= mid <= 500:
        return 20
    if 11 <= mid < 20:
        return 5
    if mid > 500:
        return -10
    return 0


def _founded_score(founded: str | None) -> int:
    if not founded:
        return 0
    if "10+" in founded:
        return 15
    if "4-10" in founded or "Last 4-10" in founded:
        return 5
    return 0


def _preview_score(item: dict) -> int:
    """Rank search previews before spending research credits."""
    score = _title_score(item.get("title"))
    score += _size_score(item.get("employeeSizeRange"))
    score += _founded_score(item.get("companyFoundedOn"))
    if _in_target_geography(item):
        score += 10
    return score


def _contact_seen(lead: dict, agent: str) -> bool:
    if not lead.get("contact_name"):
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE agent = ? AND company = ?",
                (agent, lead["company"]),
            ).fetchone()
        return bool(row)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE agent = ? AND contact_name = ? AND company = ?",
            (agent, lead["contact_name"], lead["company"]),
        ).fetchone()
    return bool(row)


def _lead_from_search(item: dict) -> dict | None:
    company = (item.get("company") or "").strip()
    if not company:
        return None
    name = (item.get("name") or "").strip() or None
    title = (item.get("title") or "").strip() or None
    industries = item.get("industries") or []
    industry = industries[0] if industries else None

    return {
        "company": company,
        "contact_name": name,
        "contact_title": title,
        "email": None,
        "linkedin_url": (item.get("liUrl") or "").strip() or None,
        "employee_count": _parse_employee_count(item.get("employeeSizeRange")),
        "industry": industry,
        "signal": f"Seamless match — {title or 'contact'} at {company}",
        "source": "seamless",
        "status": "imported",
        "_search_result_id": item.get("searchResultId"),
        "_preview": item,
    }


def _lead_from_research(contact: dict, preview: dict | None = None) -> dict | None:
    """Map researched Seamless contact record to a lead dict."""
    preview = preview or {}
    company = (contact.get("company") or preview.get("company") or "").strip()
    if not company:
        return None

    first = (contact.get("firstName") or "").strip()
    last = (contact.get("lastName") or "").strip()
    name = (
        contact.get("fullName")
        or contact.get("name")
        or f"{first} {last}".strip()
    )
    name = (name or "").strip() or None
    title = (contact.get("title") or preview.get("title") or "").strip() or None
    email = (contact.get("email") or "").strip() or None

    industries = contact.get("industries") or preview.get("industries") or []
    industry = industries[0] if industries else None
    employee_range = contact.get("employeeSizeRange") or preview.get("employeeSizeRange")

    parts = [f"Seamless enriched — {title or 'contact'} at {company}"]
    if contact.get("companyFoundedOn") or preview.get("companyFoundedOn"):
        founded = contact.get("companyFoundedOn") or preview.get("companyFoundedOn")
        parts.append(f"Founded {founded}")
    if contact.get("companyRevenue") or preview.get("companyRevenue"):
        rev = contact.get("companyRevenue") or preview.get("companyRevenue")
        parts.append(f"Revenue {rev}")

    return {
        "company": company,
        "contact_name": name,
        "contact_title": title,
        "email": email,
        "linkedin_url": (contact.get("liUrl") or preview.get("liUrl") or "").strip() or None,
        "employee_count": _parse_employee_count(employee_range),
        "industry": industry,
        "signal": " · ".join(parts),
        "source": "seamless",
        "status": "imported",
    }


def seamless_status() -> dict:
    if not seamless_available():
        return {"configured": False}
    return {"configured": True, "transport": seamless_transport(), **budget_status()}


def search_and_import_seamless(
    agent: str = "keira",
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Search (free) → rank → research (credits) → import for Keira."""
    if not seamless_available():
        raise SeamlessError("SEAMLESS_API_KEY not set")

    emit = on_progress or (lambda msg: None)
    cfg = _agent_config(agent)
    init_db()
    client = get_seamless_client()
    transport = seamless_transport()
    filters = seamless_filters(agent)

    search_cap = min(max(limit * 4, 20), 50)
    via = "MCP" if transport == "mcp" else "API"
    emit(f"Seamless search via {via} (free) — up to {search_cap} owner contacts in Eastern Ontario…")

    data = client.search_contacts(filters, limit=search_cap)
    candidates = data.get("data") or []
    emit(f"{len(candidates)} contacts returned from Seamless search")

    ranked: list[tuple[int, dict]] = []
    skipped_blocklist = skipped_geo = skipped_dup = 0

    for item in candidates:
        company = (item.get("company") or "").strip()
        if not company or _should_skip(company, cfg):
            skipped_blocklist += 1
            continue
        if not _in_target_geography(item):
            skipped_geo += 1
            continue
        lead = _lead_from_search(item)
        if not lead:
            continue
        if skip_existing and _contact_seen(lead, agent):
            skipped_dup += 1
            continue
        ranked.append((_preview_score(item), item))

    ranked.sort(key=lambda x: x[0], reverse=True)
    emit(
        f"{len(ranked)} candidates after filters "
        f"(skipped {skipped_blocklist} blocklist, {skipped_geo} geo, {skipped_dup} duplicates)"
    )

    research_cap = min(max_research_per_run(agent), limit, len(ranked))
    ok, reason = can_research(research_cap)
    if not ok:
        raise SeamlessError(reason)

    if research_cap == 0:
        emit("No new candidates to research")
        return _result_summary(
            searched=len(candidates),
            researched=0,
            imported=0,
            updated=0,
            skipped=skipped_blocklist + skipped_geo + skipped_dup,
            with_email=0,
            budget=budget_status(client.last_credits),
        )

    to_research = ranked[:research_cap]
    search_ids = [item["searchResultId"] for _, item in to_research if item.get("searchResultId")]
    preview_by_id = {item["searchResultId"]: item for _, item in to_research if item.get("searchResultId")}

    emit(f"Researching top {len(search_ids)} contacts ({len(search_ids)} credits)…")
    if transport == "mcp" and hasattr(client, "research_and_wait_contacts"):
        emit("Waiting for MCP research results…")
        poll_results = client.research_and_wait_contacts(search_ids)
    else:
        request_ids = client.research_contacts(search_ids)
        if not request_ids:
            raise SeamlessError("Seamless research returned no request IDs")
        emit("Polling for enriched results…")
        poll_results = client.wait_for_contact_research(request_ids)

    imported = updated = with_email = research_errors = 0

    for item in poll_results:
        status = item.get("status")
        if status != "done":
            research_errors += 1
            continue
        contact = item.get("contact") or {}
        rid = item.get("searchResultId")
        preview = preview_by_id.get(rid) if rid else None
        lead = _lead_from_research(contact, preview)
        if not lead:
            continue
        if skip_existing and _contact_seen(lead, agent):
            continue

        record = {k: v for k, v in lead.items() if not k.startswith("_")}
        record["prospect"] = build_prospect_string(record)
        _, created = upsert_lead(record, agent=agent)
        if created:
            imported += 1
        else:
            updated += 1
        if record.get("email"):
            with_email += 1

    credits_spent = len(search_ids)
    budget = record_credits_used(credits_spent, remaining=client.last_credits)

    emit(
        f"Done — {imported} imported, {updated} updated, {with_email} with email "
        f"({credits_spent} credits used; {budget['credits_remaining_budget']} budget remaining)"
    )

    return _result_summary(
        searched=len(candidates),
        researched=credits_spent,
        imported=imported,
        updated=updated,
        skipped=skipped_blocklist + skipped_geo + skipped_dup,
        with_email=with_email,
        research_errors=research_errors,
        budget=budget,
        provider="seamless",
        credits_used=credits_spent,
    )


def _result_summary(**kwargs) -> dict:
    return {
        "provider": kwargs.get("provider", "seamless"),
        "total_available": kwargs.get("searched", 0),
        "searched": kwargs.get("searched", 0),
        "researched": kwargs.get("researched", 0),
        "credits_used": kwargs.get("credits_used", 0),
        "imported": kwargs.get("imported", 0),
        "updated": kwargs.get("updated", 0),
        "skipped": kwargs.get("skipped", 0),
        "with_email": kwargs.get("with_email", 0),
        "research_errors": kwargs.get("research_errors", 0),
        "budget": kwargs.get("budget"),
        "errors": [],
    }
