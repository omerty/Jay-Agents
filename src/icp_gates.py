"""Deterministic ICP gates — run before LLM to filter garbage cheaply."""

from __future__ import annotations

import socket

from .db import get_connection, init_db, normalize_company
from .email_patterns import normalize_domain, resolve_company_domain
from .negative_list import is_excluded


def min_employees(config: dict) -> int:
    icp = config.get("icp") or {}
    return int(icp.get("min_employees") or 1000)


def max_employees(config: dict) -> int | None:
    icp = config.get("icp") or {}
    raw = icp.get("max_employees")
    if raw is None or raw == "":
        return None
    return int(raw)


def domain_seen(domain: str, agent: str = "woodway") -> bool:
    """Dedupe by domain, not company name string."""
    init_db()
    domain = normalize_domain(domain)
    if not domain:
        return False
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM leads
            WHERE agent = ? AND status != 'skipped'
              AND (
                LOWER(email) LIKE ?
                OR company_domain = ?
                OR company_normalized IN (
                  SELECT company_normalized FROM leads WHERE company_domain = ?
                )
              )
            LIMIT 1
            """,
            (agent, f"%@{domain}", domain, domain),
        ).fetchone()
    return bool(row)


def domain_resolves(domain: str) -> bool:
    domain = normalize_domain(domain)
    if not domain:
        return False
    try:
        socket.getaddrinfo(domain, 443)
        return True
    except socket.gaierror:
        return False


def passes_icp_gates(
    company: str,
    config: dict,
    *,
    agent: str = "woodway",
    employee_count: int | None = None,
    domain: str | None = None,
    extra_text: str = "",
) -> tuple[bool, str | None]:
    """
    Cheap pre-LLM filter. Returns (ok, reject_reason).
    """
    if not company or len(company.strip()) < 3:
        return False, "empty company"

    excluded = is_excluded(company, agent=agent, extra_text=extra_text)
    if excluded:
        return False, excluded

    domain = normalize_domain(domain) or resolve_company_domain(company)
    if domain:
        if domain_seen(domain, agent):
            return False, "domain duplicate"
        if not domain_resolves(domain):
            return False, "domain does not resolve"

    min_emp = min_employees(config)
    if employee_count is not None and employee_count < min_emp:
        return False, f"below {min_emp} employees"

    # max_employees is a soft preference for Woodway scoring/digest — not a hard reject.
    # Only hard-reject when config explicitly sets max_employees_hard: true
    icp = config.get("icp") or {}
    if icp.get("max_employees_hard"):
        max_emp = max_employees(config)
        if max_emp is not None and employee_count is not None and employee_count > max_emp:
            return False, f"above {max_emp} employees (too ambitious / hard to approach)"

    key = normalize_company(company)
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM suppression WHERE agent = ? AND (company_normalized = ? OR domain = ? OR identifier = ?)",
            (agent, key, domain or "", domain or ""),
        ).fetchone()
    if row:
        return False, "suppressed"

    return True, None
