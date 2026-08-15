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

from .db import build_prospect_string, get_connection, init_db, normalize_company, upsert_lead
from .discover import _should_skip
from .seamless_api import SeamlessError, get_seamless_client, seamless_available, seamless_transport
from .seamless_budget import (
    allocate_research_slots,
    budget_status,
    can_research,
    format_budget_alert_message,
    notify_seamless_budget_exhausted,
    record_credits_used,
)

logger = logging.getLogger(__name__)


def max_contacts_per_company(agent: str = "woodway") -> int:
    """How many people to research per company (default 1 for coverage volume)."""
    import os

    if agent == "keira":
        return max(1, int(os.getenv("SEAMLESS_KEIRA_CONTACTS_PER_COMPANY", "1")))
    return max(1, int(os.getenv("SEAMLESS_WOODWAY_CONTACTS_PER_COMPANY", "1")))


def pick_coverage_first(
    ranked: list[tuple[int, dict]],
    *,
    max_per_company: int,
    research_cap: int,
) -> tuple[list[tuple[int, dict]], dict[str, int]]:
    """Prefer 1 contact per company before any seconds — maximizes company coverage."""
    to_research: list[tuple[int, dict]] = []
    per_company: dict[str, int] = {}
    chosen_ids: set[str] = set()

    def _key(item: dict) -> str:
        return (item.get("company") or "").strip().lower()

    def _sid(item: dict) -> str:
        return str(item.get("searchResultId") or "")

    # Pass 1: best contact for each company
    for score, item in ranked:
        if len(to_research) >= research_cap:
            break
        ck = _key(item)
        if not ck or per_company.get(ck, 0) >= 1:
            continue
        sid = _sid(item)
        if sid and sid in chosen_ids:
            continue
        per_company[ck] = 1
        if sid:
            chosen_ids.add(sid)
        to_research.append((score, item))

    # Pass 2: optional additional contacts per company
    if max_per_company > 1 and len(to_research) < research_cap:
        for score, item in ranked:
            if len(to_research) >= research_cap:
                break
            ck = _key(item)
            if not ck:
                continue
            n = per_company.get(ck, 0)
            if n < 1 or n >= max_per_company:
                continue
            sid = _sid(item)
            if sid and sid in chosen_ids:
                continue
            per_company[ck] = n + 1
            if sid:
                chosen_ids.add(sid)
            to_research.append((score, item))

    return to_research, per_company


def company_names_match(target: str, candidate: str) -> bool:
    """Strict-ish company match — blocks 'Kroon' → 'Kroonpress As' credit waste."""
    from difflib import SequenceMatcher

    a = normalize_company(target or "")
    b = normalize_company(candidate or "")
    if not a or not b:
        return False
    if a == b:
        return True
    # Very short targets must be exact (after normalize)
    if len(a) <= 5:
        return a == b
    if a in b or b in a:
        ratio = min(len(a), len(b)) / max(len(a), len(b))
        return ratio >= 0.72
    return SequenceMatcher(None, a, b).ratio() >= 0.88


def _matches_requested_companies(item: dict, requested: list[str]) -> bool:
    company = (item.get("company") or "").strip()
    if not company or not requested:
        return False
    return any(company_names_match(req, company) for req in requested)


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
    "renfrew",
    "smiths falls",
    "arnprior",
    "hawkesbury",
    "perth",
    "carleton place",
    "gatineau",
)


def _agent_config(agent: str) -> dict:
    with open(AGENTS_DIR / agent / "config.yaml") as f:
        return yaml.safe_load(f)


def seamless_filters(agent: str, *, query: dict | None = None) -> dict:
    """Build Seamless search body from agent config (+ optional discovery query overlay)."""
    cfg = _agent_config(agent)
    seamless = cfg.get("seamless") or {}
    icp = cfg.get("icp") or {}
    query = query or {}

    titles = query.get("job_title") or seamless.get("job_title") or icp.get("titles") or []
    filters: dict = {
        "contactCountry": seamless.get("contact_country") or ["Canada"],
        "contactState": seamless.get("contact_state") or ["Ontario"],
        "locationType": seamless.get("location_type") or "company",
        "jobTitle": titles[:10],
        "seniority": query.get("seniority")
        or seamless.get("seniority")
        or (["C-Level"] if agent == "keira" else ["C-Level", "VP", "Director"]),
        "companySize": query.get("company_size") or seamless.get("company_size") or [
            "11 - 50",
            "51 - 200",
            "201 - 500",
        ],
        "limit": 50,
    }

    omit = {str(x) for x in (query.get("omit") or [])}

    revenue = query.get("company_revenue")
    if revenue is None and "company_revenue" not in omit:
        revenue = seamless.get("company_revenue")
    if revenue:
        filters["companyRevenue"] = revenue

    founded = query.get("company_founded_on")
    if founded is None and "company_founded_on" not in omit:
        founded = seamless.get("company_founded_on")
    if founded:
        filters["companyFoundedOn"] = founded

    industries = query.get("industry")
    if industries is None and "industry" not in omit:
        industries = seamless.get("industry")
    if industries:
        filters["industry"] = industries[:5]

    keywords = query.get("contact_keyword")
    if keywords is None and "contact_keyword" not in omit:
        keywords = seamless.get("contact_keyword")
    if keywords:
        filters["contactKeyword"] = keywords[:10]

    cities = query.get("company_city") or query.get("cities")
    if cities:
        filters["companyCity"] = list(cities)[:8]

    return filters


def _keira_discovery_queries(agent: str) -> list[dict]:
    """Configured multi-queries, or a sensible widened default set."""
    cfg = _agent_config(agent)
    seamless = cfg.get("seamless") or {}
    configured = seamless.get("discovery_queries")
    if configured:
        return list(configured)
    return [
        {"name": "ontario_owners", "omit": ["industry", "company_founded_on", "company_revenue", "contact_keyword"]},
        {
            "name": "ottawa_corridor",
            "company_city": ["Ottawa", "Kanata", "Nepean"],
            "omit": ["industry", "company_revenue", "company_founded_on", "contact_keyword"],
        },
        {
            "name": "kingston_owners",
            "company_city": ["Kingston"],
            "job_title": ["Owner", "Founder", "President", "CEO"],
            "omit": ["industry", "company_revenue", "company_founded_on", "contact_keyword"],
        },
        {
            "name": "eastern_cities",
            "company_city": ["Cornwall", "Belleville", "Pembroke", "Brockville"],
            "omit": ["industry", "company_revenue", "company_founded_on", "contact_keyword"],
        },
        {
            "name": "corridor_towns",
            "company_city": [
                "Renfrew", "Smiths Falls", "Arnprior", "Hawkesbury",
                "Perth", "Carleton Place", "Gatineau", "Orleans",
            ],
            "job_title": ["Owner", "Founder", "President", "CEO"],
            "omit": ["industry", "company_revenue", "company_founded_on", "contact_keyword"],
        },
    ]


def _candidate_dedupe_key(lead: dict) -> str:
    email = (lead.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    company = (lead.get("company") or "").strip().lower()
    name = (lead.get("contact_name") or "").strip().lower()
    return f"co:{company}|{name}"


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
    try:
        from .keira_geo import geo_score

        g = geo_score(
            hq=item.get("companyCity"),
            city=item.get("companyCity") or item.get("city"),
            state=item.get("companyState") or item.get("state"),
            extra_text=blob,
        )
        if g.get("reject"):
            return False
        if g.get("score", 0) >= 15:
            return True
        # Soft keep Ontario for discovery ranking; pipeline hard-gates later
        return "ontario" in blob
    except Exception:
        return any(kw in blob for kw in ONTARIO_GEO_KEYWORDS)


def _title_score(title: str | None) -> int:
    if not title:
        return 0
    lower = title.lower()
    if any(kw in lower for kw in ("owner", "founder", "co-founder")):
        return 40
    if any(kw in lower for kw in OWNER_TITLE_KEYWORDS):
        return 25
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
    try:
        from .keira_geo import geo_score
        from .owner_resolution import classify_owner_title

        g = geo_score(
            hq=item.get("companyCity"),
            city=item.get("companyCity") or item.get("city"),
            state=item.get("companyState") or item.get("state"),
        )
        score += int(g.get("score") or 0)
        owner = classify_owner_title(item.get("title"), company=item.get("company"))
        score += int(round(float(owner.get("ownership_confidence") or 0) * 20))
        if owner.get("reject"):
            score -= 25
    except Exception:
        if _in_target_geography(item):
            score += 10
    return score


def _keira_company_has_owner(company: str, agent: str) -> bool:
    from .owner_resolution import is_owner_like_title

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT contact_title FROM leads
            WHERE agent = ? AND company = ?
              AND status NOT IN ('skipped')
              AND contact_name IS NOT NULL AND TRIM(contact_name) != ''
            """,
            (agent, company),
        ).fetchall()
    return any(is_owner_like_title(r["contact_title"], company=company) for r in rows)


def _contact_seen(lead: dict, agent: str) -> bool:
    company = lead.get("company") or ""
    name = (lead.get("contact_name") or "").strip()
    if name:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM leads WHERE agent = ? AND contact_name = ? AND company = ?",
                (agent, name, company),
            ).fetchone()
        if row:
            return True
        if agent == "keira":
            return _keira_company_has_owner(company, agent)
        return False
    if agent == "keira":
        return _keira_company_has_owner(company, agent)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE agent = ? AND company = ?",
            (agent, company),
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
        "company_city": (item.get("companyCity") or item.get("city") or "").strip() or None,
        "company_state": (item.get("companyState") or item.get("state") or "").strip() or None,
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
    from .seamless_oauth import oauth_connected, seamless_auth_status

    oauth = seamless_auth_status()
    if not seamless_available():
        return {"configured": False, "transport": seamless_transport(), **oauth}
    base = {
        "configured": True,
        "transport": seamless_transport(),
        "auth": "oauth" if oauth_connected() else "api_key",
        **oauth,
        **budget_status(),
    }
    return base


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

    needed = min(limit, len(ranked))
    # Refresh credits if possible, then allocate under daily/monthly/reserve caps
    api_left = client.last_credits
    research_cap, cap_note = allocate_research_slots(
        needed, agent=agent, api_remaining=api_left,
    )
    if research_cap == 0:
        alert = notify_seamless_budget_exhausted(
            cap_note or "no Seamless research budget remaining",
            agent=agent,
            context="contact search",
        )
        emit(format_budget_alert_message(alert["reason"], agent=agent))
        emit(f"Seamless research blocked — {cap_note}")
        out = _result_summary(
            searched=len(candidates),
            researched=0,
            imported=0,
            updated=0,
            skipped=skipped_blocklist + skipped_geo + skipped_dup,
            with_email=0,
            budget=budget_status(client.last_credits, agent=agent),
            note=cap_note,
        )
        out["budget_alert"] = alert
        out["alerts"] = [alert]
        return out
    if cap_note:
        emit(f"Seamless budget guard — {cap_note}")

    ok, reason = can_research(research_cap, agent=agent, api_remaining=api_left)
    if not ok:
        raise SeamlessError(reason)

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
    budget = record_credits_used(credits_spent, remaining=client.last_credits, agent=agent)

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
    out = {
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
        "note": kwargs.get("note"),
        "errors": [],
    }
    if kwargs.get("budget_alert"):
        out["budget_alert"] = kwargs["budget_alert"]
    if kwargs.get("alerts"):
        out["alerts"] = kwargs["alerts"]
    return out


# Seamless rejects company filters with more than 15 values
SEAMLESS_MAX_COMPANIES_PER_SEARCH = 15


def search_and_import_seamless_for_companies(
    agent: str,
    company_names: list[str],
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Search Seamless contacts at specific companies (Woodway company→people step).

    Uses free search filtered by companyName + agent job titles, then researches
    top matches (credits). Geography filter is skipped — company list is the scope.
    Company names are batched (max 15 per Seamless request).
    """
    if not seamless_available():
        raise SeamlessError("SEAMLESS_API_KEY not set")

    emit = on_progress or (lambda msg: None)
    names = [n.strip() for n in company_names if n and n.strip()]
    # Dedupe preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for n in names:
        key = n.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    names = deduped

    if not names:
        emit("No company names to enrich via Seamless")
        return _result_summary(searched=0, researched=0, imported=0, updated=0, skipped=0, with_email=0)

    cfg = _agent_config(agent)
    init_db()
    client = get_seamless_client()
    transport = seamless_transport()
    base = seamless_filters(agent)
    titles = base.get("jobTitle") or []
    seniority = base.get("seniority") or (["C-Level"] if agent == "keira" else ["VP", "Director", "C-Level"])

    via = "MCP" if transport == "mcp" else "API"
    batches = [
        names[i : i + SEAMLESS_MAX_COMPANIES_PER_SEARCH]
        for i in range(0, len(names), SEAMLESS_MAX_COMPANIES_PER_SEARCH)
    ]
    emit(
        f"Seamless search via {via} for contacts at {len(names)} companies "
        f"({len(batches)} batch(es) ≤{SEAMLESS_MAX_COMPANIES_PER_SEARCH}) "
        f"(titles: {', '.join(titles[:4]) or 'any'})…"
    )

    candidates: list[dict] = []
    for bi, batch in enumerate(batches, 1):
        filters = {
            "companyName": batch,
            "companyNameSearchType": "default",
            "jobTitle": titles,
            "seniority": seniority,
            "limit": min(max(limit * 3, 20), 50),
        }
        emit(f"  Seamless batch {bi}/{len(batches)} — {len(batch)} companies…")
        data = client.search_contacts(filters, limit=filters["limit"])
        batch_rows = data.get("data") or []
        candidates.extend(batch_rows)
        emit(f"  Batch {bi} returned {len(batch_rows)} contacts")

    if agent == "keira":
        from .owner_resolution import is_owner_like_title

        have_owner = {
            (item.get("company") or "").strip().lower()
            for item in candidates
            if is_owner_like_title(item.get("title"), company=item.get("company"))
        }
        retry_names = [n for n in names if n.lower() not in have_owner]
        if retry_names:
            emit(
                f"  Owner retry — free search Owner/Founder for {len(retry_names)} "
                "companies without an owner-like hit…"
            )
            retry_batches = [
                retry_names[i : i + SEAMLESS_MAX_COMPANIES_PER_SEARCH]
                for i in range(0, len(retry_names), SEAMLESS_MAX_COMPANIES_PER_SEARCH)
            ]
            for bi, batch in enumerate(retry_batches, 1):
                filters = {
                    "companyName": batch,
                    "companyNameSearchType": "default",
                    "jobTitle": ["Owner", "Founder"],
                    "seniority": ["C-Level"],
                    "limit": min(max(limit * 3, 20), 50),
                }
                data = client.search_contacts(filters, limit=filters["limit"])
                extra = data.get("data") or []
                candidates.extend(extra)
                emit(f"  Owner-retry batch {bi} returned {len(extra)} contacts")

    emit(f"{len(candidates)} contacts returned from Seamless company search")

    ranked: list[tuple[int, dict]] = []
    skipped_blocklist = skipped_dup = skipped_name = skipped_geo = 0
    require_geo = agent == "keira"
    for item in candidates:
        company = (item.get("company") or "").strip()
        if not company or _should_skip(company, cfg):
            skipped_blocklist += 1
            continue
        if not _matches_requested_companies(item, names):
            skipped_name += 1
            continue
        if require_geo and not _in_target_geography(item):
            skipped_geo += 1
            continue
        lead = _lead_from_search(item)
        if not lead:
            continue
        if skip_existing and _contact_seen(lead, agent):
            skipped_dup += 1
            continue
        # Prefer title fit; Keira also ranks by geo/owner preview and skips VPs
        if agent == "keira":
            from .owner_resolution import classify_owner_title

            owner = classify_owner_title(item.get("title"), company=item.get("company"))
            if owner.get("reject"):
                skipped_dup += 1
                continue
            score = _preview_score(item)
        else:
            score = _title_score(item.get("title")) + _size_score(item.get("employeeSizeRange"))
        ranked.append((score, item))

    ranked.sort(key=lambda x: x[0], reverse=True)
    emit(
        f"{len(ranked)} candidates after filters "
        f"(skipped {skipped_blocklist} blocklist, {skipped_name} name-mismatch, "
        f"{skipped_geo} geo, {skipped_dup} duplicates)"
    )

    # Cap people per company. Coverage-first: 1× each company before any 2nds.
    max_per_co = max_contacts_per_company(agent)
    hard_cap = max(len(names) * max_per_co, 1)
    needed = min(
        max(limit, len(names)),
        hard_cap,
        len(ranked),
    )
    api_left = client.last_credits
    research_cap, cap_note = allocate_research_slots(
        needed, agent=agent, api_remaining=api_left,
    )
    if research_cap == 0:
        alert = notify_seamless_budget_exhausted(
            cap_note or "no Seamless research budget remaining",
            agent=agent,
            context="company contact enrich",
        )
        emit(format_budget_alert_message(alert["reason"], agent=agent))
        emit(f"Seamless research blocked — {cap_note}")
        return _result_summary(
            searched=len(candidates),
            researched=0,
            imported=0,
            updated=0,
            skipped=skipped_blocklist + skipped_dup + skipped_name + skipped_geo,
            with_email=0,
            budget=budget_status(client.last_credits, agent=agent),
            note=cap_note,
            budget_alert=alert,
            alerts=[alert],
        )
    if cap_note:
        emit(f"Seamless budget guard — {cap_note}")

    ok, reason = can_research(research_cap, agent=agent, api_remaining=api_left)
    if not ok:
        raise SeamlessError(reason)

    to_research, per_company = pick_coverage_first(
        ranked, max_per_company=max_per_co, research_cap=research_cap,
    )

    search_ids = [item["searchResultId"] for _, item in to_research if item.get("searchResultId")]
    preview_by_id = {item["searchResultId"]: item for _, item in to_research if item.get("searchResultId")}

    emit(
        f"Researching {len(search_ids)} contacts across {len(per_company)} companies "
        f"(coverage-first, max {max_per_co}/company, {len(search_ids)} credits)…"
    )
    if transport == "mcp" and hasattr(client, "research_and_wait_contacts"):
        poll_results = client.research_and_wait_contacts(search_ids)
    else:
        request_ids = client.research_contacts(search_ids)
        if not request_ids:
            raise SeamlessError("Seamless research returned no request IDs")
        poll_results = client.wait_for_contact_research(request_ids)

    imported = updated = with_email = research_errors = 0
    for item in poll_results:
        if item.get("status") != "done":
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
    budget = record_credits_used(credits_spent, remaining=client.last_credits, agent=agent)
    emit(
        f"Done — {imported} imported, {updated} updated, {with_email} with email "
        f"({credits_spent} credits used)"
    )
    return _result_summary(
        searched=len(candidates),
        researched=credits_spent,
        imported=imported,
        updated=updated,
        skipped=skipped_blocklist + skipped_geo + skipped_dup + skipped_name,
        with_email=with_email,
        research_errors=research_errors,
        budget=budget,
        provider="seamless",
        credits_used=credits_spent,
    )


def discover_keira_candidates(
    agent: str = "keira",
    *,
    limit: int = 40,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Free Seamless multi-query search → ranked candidate dicts (no research credits)."""
    if not seamless_available():
        raise SeamlessError("SEAMLESS_API_KEY not set")

    emit = on_progress or (lambda msg: None)
    cfg = _agent_config(agent)
    init_db()
    client = get_seamless_client()
    queries = _keira_discovery_queries(agent)
    try:
        from .query_learning import query_yield_bonus

        queries = sorted(
            queries,
            key=lambda q: (-query_yield_bonus(agent, q.get("name") or ""), q.get("name") or ""),
        )
    except Exception:
        pass
    search_cap = min(max(limit, 20), 50)

    ranked: list[tuple[int, dict]] = []
    seen_keys: set[str] = set()
    skipped_blocklist = skipped_geo = skipped_dup = 0
    raw_total = 0
    query_stats: list[dict] = []

    emit(f"Seamless free multi-query — {len(queries)} searches, up to {search_cap} each (0 credits)…")

    for q in queries:
        qname = q.get("name") or "query"
        filters = seamless_filters(agent, query=q)
        try:
            data = client.search_contacts(filters, limit=search_cap)
        except Exception as e:
            logger.warning("Keira Seamless query %s failed: %s", qname, e)
            query_stats.append({"name": qname, "searched": 0, "kept": 0, "error": str(e)})
            continue

        candidates_raw = data.get("data") or []
        raw_total += len(candidates_raw)
        kept = 0
        for item in candidates_raw:
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
            key = _candidate_dedupe_key(lead)
            if key in seen_keys:
                skipped_dup += 1
                continue
            seen_keys.add(key)
            lead["_discovery_query"] = qname
            ranked.append((_preview_score(item), lead))
            kept += 1
        query_stats.append({"name": qname, "searched": len(candidates_raw), "kept": kept})
        emit(f"  Seamless query '{qname}' — {len(candidates_raw)} raw, {kept} kept")
        try:
            from .query_learning import record_query_outcome

            record_query_outcome(agent, qname, positive=kept > 0)
        except Exception:
            pass

    ranked.sort(key=lambda x: x[0], reverse=True)
    candidates = []
    for score, lead in ranked[:limit]:
        row = {k: v for k, v in lead.items() if not k.startswith("_")}
        row["_search_result_id"] = lead.get("_search_result_id")
        row["_preview"] = lead.get("_preview")
        row["_rank_score"] = score
        row["_discovery_query"] = lead.get("_discovery_query")
        founded = (lead.get("_preview") or {}).get("companyFoundedOn")
        if founded:
            row["signal"] = f"{row.get('signal') or ''} · Founded {founded}".strip(" ·")
        candidates.append(row)

    emit(
        f"{len(candidates)} Keira candidates (free, {len(queries)} queries) — "
        f"skipped {skipped_blocklist} blocklist, {skipped_geo} geo, {skipped_dup} dupes "
        f"(raw {raw_total})"
    )
    return {
        "provider": "seamless",
        "mode": "discover_multi_query",
        "searched": raw_total,
        "candidates": candidates,
        "skipped": skipped_blocklist + skipped_geo + skipped_dup,
        "credits_used": 0,
        "queries": query_stats,
        "budget": budget_status(client.last_credits),
    }


def enrich_keira_companies(
    agent: str,
    company_names: list[str],
    *,
    limit: int = 8,
    on_progress=None,
) -> dict:
    """Spend Seamless research credits only for gated company survivors."""
    return search_and_import_seamless_for_companies(
        agent,
        company_names,
        limit=limit,
        skip_existing=False,
        on_progress=on_progress,
    )
