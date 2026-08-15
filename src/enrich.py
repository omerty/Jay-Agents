"""Company enrichment — Clearbit autocomplete (free, no key)."""

import httpx


def enrich_company(company_name: str) -> dict:
    """
    Look up company via Clearbit autocomplete.
    Returns {name, domain, logo} or empty dict on failure.
    """
    if not company_name or not company_name.strip():
        return {}

    try:
        r = httpx.get(
            "https://autocomplete.clearbit.com/v1/companies/suggest",
            params={"query": company_name.strip()},
            timeout=5.0,
            headers={"User-Agent": "JayAgents/1.0"},
        )
        r.raise_for_status()
        results = r.json()
    except Exception:
        return {}

    if not results:
        return {}

    best = results[0]
    return {
        "name": best.get("name"),
        "domain": best.get("domain"),
        "logo": best.get("logo"),
    }


def format_enrichment(enrichment: dict, employee_count: int | None = None) -> str:
    parts = []
    if enrichment.get("name"):
        parts.append(f"Company: {enrichment['name']}")
    if enrichment.get("domain"):
        parts.append(f"Domain: {enrichment['domain']}")
    if employee_count:
        parts.append(f"Employees: {employee_count:,}")
        if employee_count >= 1000:
            parts.append("Size tier: large enterprise (1000+ employees)")
        elif employee_count >= 200:
            parts.append("Size tier: mid-market (200-999 employees)")
        else:
            parts.append("Size tier: small (<200 employees)")
    return "\n".join(parts) if parts else "No enrichment data"


def size_score_boost(employee_count: int | None) -> int:
    """Legacy larger-is-better boost (FONEX / old Woodway). Prefer woodway_size_score_boost."""
    if employee_count is None:
        return 0
    if employee_count >= 5000:
        return 15
    if employee_count >= 1000:
        return 12
    if employee_count >= 200:
        return 5
    if employee_count < 50:
        return -10
    return 0


def woodway_size_score_boost(
    employee_count: int | None,
    *,
    min_employees: int = 1000,
    max_employees: int | None = None,
    prefer_min: int = 1500,
    prefer_max: int = 10000,
) -> int:
    """Boost preferred mid-large band; megabrands stay neutral (not blocked)."""
    if employee_count is None:
        return 0
    n = int(employee_count)
    if n < 50:
        return -15
    if n < min_employees:
        return -10
    if prefer_min <= n <= prefer_max:
        # Best: approachable 1.5k–10k
        if 2000 <= n <= 8000:
            return 15
        return 12
    if min_employees <= n < prefer_min:
        return 8
    # Above prefer band (incl. megabrands) — still fine; small nudge only
    if prefer_max < n:
        return 5
    return 0


def is_mega_brand(company: str | None, config: dict) -> bool:
    """True if company matches the household-megabrand preference list."""
    from .db import normalize_company

    names = config.get("mega_brand_prefer_peers") or config.get("mega_brand_demote") or []
    if not company or not names:
        return False
    key = normalize_company(company)
    for name in names:
        n = normalize_company(str(name))
        if not n:
            continue
        # Exact or prefix match only — avoid "Citi" hitting "Citizens"
        if key == n or key.startswith(n + " ") or n.startswith(key + " "):
            return True
    return False


def mega_brand_demote_penalty(company: str | None, config: dict) -> int:
    """Deprecated: megabrands are not score-blocked. Kept for callers; always 0."""
    return 0


def keira_size_score_boost(employee_count: int | None) -> int:
    """Mid-market employee proxy for Keira's $10–100M valuation sweet spot."""
    if employee_count is None:
        return 0
    if 50 <= employee_count <= 500:
        return 15
    if 20 <= employee_count < 50:
        return 8
    if 500 < employee_count <= 1500:
        return 5
    if employee_count >= 5000:
        return -15
    if employee_count < 20:
        return -10
    return 0


def agent_size_score_boost(employee_count: int | None, config: dict) -> int:
    """Agent-specific size adjustment from ICP config."""
    icp = config.get("icp") or {}
    size_cfg = icp.get("company_size")
    if isinstance(size_cfg, dict):
        return keira_size_score_boost(employee_count)
    # Woodway: prefer mid-large band boost; megabrands allowed
    if config.get("name") == "woodway" or icp.get("prefer_employees_max") is not None:
        max_raw = icp.get("max_employees")
        return woodway_size_score_boost(
            employee_count,
            min_employees=int(icp.get("min_employees") or 1000),
            max_employees=int(max_raw) if max_raw not in (None, "") else None,
            prefer_min=int(icp.get("prefer_employees_min") or 1500),
            prefer_max=int(icp.get("prefer_employees_max") or 10000),
        )
    if size_cfg == "large":
        return size_score_boost(employee_count)
    return 0
