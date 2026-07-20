"""People Data Labs — person search + CSV import."""

import csv
import re
from pathlib import Path

import yaml

from .db import build_prospect_string, company_seen, upsert_lead
from .pdl_api import PDLClient, PDLError

AGENTS_DIR = Path(__file__).parent.parent / "agents"

# Midpoint estimates for company size ranges (used in ICP scoring)
SIZE_MIDPOINT = {
    "1-10": 5,
    "11-50": 30,
    "51-200": 125,
    "201-500": 350,
    "501-1000": 750,
    "1001-5000": 3000,
    "5001-10000": 7500,
    "10001+": 15000,
}

COLUMN_MAP = {
    "first name": "first_name",
    "last name": "last_name",
    "full name": "contact_name",
    "title": "contact_title",
    "job title": "contact_title",
    "company": "company",
    "company name": "company",
    "job company name": "company",
    "email": "email",
    "work email": "email",
    "work_email": "email",
    "linkedin url": "linkedin_url",
    "linkedin_url": "linkedin_url",
    "person linkedin url": "linkedin_url",
    "employees": "employee_count",
    "job company size": "employee_count_raw",
    "industry": "industry",
}


def _parse_int(val) -> int | None:
    if val is None or val == "":
        return None
    if isinstance(val, str) and val in SIZE_MIDPOINT:
        return SIZE_MIDPOINT[val]
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None


def _normalize_row(row: dict) -> dict:
    mapped = {}
    for header, value in row.items():
        key = COLUMN_MAP.get(header.strip().lower())
        if key and value and str(value).strip():
            mapped[key] = str(value).strip()

    first = mapped.pop("first_name", "")
    last = mapped.pop("last_name", "")
    if first or last:
        mapped["contact_name"] = f"{first} {last}".strip()

    if "employee_count_raw" in mapped:
        mapped["employee_count"] = _parse_int(mapped.pop("employee_count_raw"))
    elif "employee_count" in mapped:
        mapped["employee_count"] = _parse_int(mapped["employee_count"])

    return mapped


def build_search_query(cfg: dict) -> dict:
    """Build Elasticsearch query for PDL Person Search."""
    must: list[dict] = []

    # Large companies only
    sizes = cfg.get("job_company_sizes") or ["1001-5000", "5001-10000", "10001+"]
    must.append({"terms": {"job_company_size": sizes}})

    # Title keyword match (any of). PDL rejects minimum_should_match;
    # a bool with only "should" clauses already requires >=1 match.
    title_keywords = cfg.get("job_title_keywords") or cfg.get("person_titles") or []
    if title_keywords:
        must.append({
            "bool": {
                "should": [{"match": {"job_title": kw}} for kw in title_keywords],
            }
        })

    # Optional role filter (off by default — PDL canonical role values are
    # strict and a wrong value silently matches nothing)
    roles = cfg.get("job_title_roles") or []
    if roles:
        must.append({"terms": {"job_title_role": roles}})

    # Seniority: director, vp, cxo
    levels = cfg.get("job_title_levels") or ["director", "vp", "cxo"]
    if levels:
        must.append({"terms": {"job_title_levels": levels}})

    # Require work email
    if cfg.get("require_work_email", True):
        must.append({"exists": {"field": "work_email"}})

    # Geography
    country = cfg.get("location_country")
    if country:
        must.append({"term": {"location_country": country.lower()}})
    region = cfg.get("location_region")
    if region:
        must.append({"term": {"location_region": region.lower()}})

    # Industry filter (opt-in — can be too strict and return 0 results)
    if cfg.get("filter_industries") and cfg.get("industries"):
        must.append({
            "bool": {
                "should": [{"match": {"industry": ind}} for ind in cfg["industries"]],
            }
        })

    return {"bool": {"must": must}}


def _clean_str(val) -> str | None:
    """PDL free plans return booleans for obscured fields — keep real strings only."""
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _split_name(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = full_name.strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[-1]


def _domain_from_website(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    domain = url.replace("https://", "").replace("http://", "").split("/")[0]
    domain = domain.removeprefix("www.").strip()
    return domain or None


def _lead_from_pdl_person(person: dict) -> dict | None:
    company = _clean_str(person.get("job_company_name"))
    if not company:
        return None

    size_raw = person.get("job_company_size")
    employee_count = SIZE_MIDPOINT.get(size_raw) if size_raw else None

    email = _clean_str(person.get("work_email"))
    if email and "@" not in email:
        email = None

    full_name = _clean_str(person.get("full_name"))
    first, last = _split_name(full_name)
    domain = _domain_from_website(_clean_str(person.get("job_company_website")))

    return {
        "company": company,
        "contact_name": full_name,
        "contact_title": _clean_str(person.get("job_title")),
        "email": email,
        "linkedin_url": _clean_str(person.get("linkedin_url")),
        "employee_count": employee_count,
        "industry": _clean_str(person.get("industry")),
        "signal": f"PDL match — {person.get('job_title') or 'contact'} at {company}",
        "source": "pdl_api",
        "status": "imported",
        "_first_name": first,
        "_last_name": last,
        "_domain": domain,
    }


def _pdl_config(agent: str) -> dict:
    path = AGENTS_DIR / agent / "config.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)
    pdl = cfg.get("pdl") or {}
    icp = cfg.get("icp") or {}
    return {
        "job_title_keywords": pdl.get("job_title_keywords") or icp.get("titles") or [],
        "job_title_roles": pdl.get("job_title_roles") or [],
        "job_company_sizes": pdl.get("job_company_sizes", ["1001-5000", "5001-10000", "10001+"]),
        "job_title_levels": pdl.get("job_title_levels", ["director", "vp", "cxo", "owner"]),
        "require_work_email": pdl.get("require_work_email", True),
        "location_country": pdl.get("location_country"),
        "location_region": pdl.get("location_region"),
        "industries": pdl.get("industries") or icp.get("industries"),
        "filter_industries": pdl.get("filter_industries", False),
        "per_page": pdl.get("per_page", 25),
    }


def _email_seen(email: str, agent: str) -> bool:
    from .db import get_connection, init_db
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE email = ? AND agent = ?",
            (email, agent),
        ).fetchone()
    return row is not None


def search_and_import(
    agent: str = "woodway",
    *,
    limit: int = 25,
    skip_existing: bool = True,
) -> dict:
    """
    Search PDL for ICP-fit contacts and save to leads.db.
    Costs 1 PDL credit per record returned (~100 free/month).
    """
    cfg = _pdl_config(agent)
    client = PDLClient()

    es_query = build_search_query(cfg)

    # Paginate via scroll_token until we have `limit` records or PDL runs out
    people: list[dict] = []
    total_available = 0
    scroll_token = None
    while len(people) < limit:
        page_size = min(limit - len(people), cfg["per_page"])
        result = client.search_people(es_query, size=page_size, scroll_token=scroll_token)
        batch = result.get("data") or []
        total_available = result.get("total") or total_available
        people.extend(batch)
        scroll_token = result.get("scroll_token")
        if not batch or not scroll_token:
            break

    imported = updated = skipped = 0
    with_email = 0
    errors: list[str] = []

    for person in people[:limit]:
        try:
            lead = _lead_from_pdl_person(person)
            if not lead:
                skipped += 1
                continue

            if lead.get("email"):
                with_email += 1

            record = {k: v for k, v in lead.items() if not k.startswith("_")}
            record["prospect"] = build_prospect_string(record)

            if skip_existing:
                if record.get("email"):
                    if _email_seen(record["email"], agent):
                        skipped += 1
                        continue
                elif company_seen(record["company"], agent=agent):
                    skipped += 1
                    continue

            _, created = upsert_lead(record, agent=agent)
            if created:
                imported += 1
            else:
                updated += 1
        except Exception as e:
            errors.append(str(e))

    return {
        "total_available": total_available,
        "searched": len(people),
        "credits_used": len(people),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "with_email": with_email,
        "errors": errors,
    }


def import_csv(path: str | Path, agent: str = "woodway") -> dict:
    """Import contacts from CSV (manual fallback)."""
    path = Path(path).expanduser()
    if not path.exists():
        sample = Path(__file__).parent.parent / "samples" / "contacts-sample.csv"
        hint = f"\nTry the sample:\n  python -m src.pdl_cli import {sample}"
        hint += "\n\nOr search via API:\n  python -m src.pdl_cli search --limit 25"
        raise FileNotFoundError(f"CSV not found: {path}{hint}")

    imported = updated = skipped = 0
    errors: list[str] = []

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            try:
                data = _normalize_row(row)
                company = data.get("company")
                if not company:
                    skipped += 1
                    continue

                data["source"] = "csv"
                data["status"] = "imported"
                data["prospect"] = build_prospect_string(data)
                _, created = upsert_lead(data, agent=agent)
                if created:
                    imported += 1
                else:
                    updated += 1
            except Exception as e:
                errors.append(f"Row {i}: {e}")

    return {
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
