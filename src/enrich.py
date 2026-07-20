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
    """Boost ICP score based on company size — larger = better fit for Woodway/FONEX."""
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
    size_cfg = config.get("icp", {}).get("company_size")
    if isinstance(size_cfg, dict):
        return keira_size_score_boost(employee_count)
    if size_cfg == "large":
        return size_score_boost(employee_count)
    return 0
