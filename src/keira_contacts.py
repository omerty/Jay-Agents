"""Keira contact discovery — same stack as Woodway (Seamless-first when configured)."""

from __future__ import annotations

import logging
import os

from .db import get_awaiting_contact_companies, get_leads, set_lead_fields
from .seamless_api import SeamlessError, seamless_available

logger = logging.getLogger(__name__)


def keira_contact_discovery_mode() -> str:
    """auto | web | seamless | paid — mirrors WOODWAY_CONTACT_DISCOVERY."""
    raw = (
        os.getenv("KEIRA_CONTACT_DISCOVERY")
        or os.getenv("WOODWAY_CONTACT_DISCOVERY")
        or "auto"
    ).strip().lower()
    if raw in ("web", "seamless", "paid", "apollo", "pdl"):
        return "paid" if raw in ("apollo", "pdl") else raw
    if seamless_available():
        return "seamless"
    return "web"


def discover_keira_contacts(
    agent: str,
    company_names: list[str],
    *,
    limit: int = 10,
    config: dict | None = None,
    skip_existing: bool = False,
    on_progress=None,
    force_mode: str | None = None,
) -> dict:
    """
    Woodway-equivalent contact fill for Keira companies.

    Seamless mode: people search first (emails from research), skip Hunter/patterns.
    Other modes: patterns → web/paid → Hunter → infer.
    """
    emit = on_progress or (lambda msg: None)
    names = [n.strip() for n in company_names if n and n.strip()]
    mode = (force_mode or keira_contact_discovery_mode()).strip().lower()
    summary: dict = {"imported": 0, "updated": 0, "companies": names, "mode": mode}
    if not names:
        summary["skipped"] = True
        return summary

    web_step: dict
    if mode == "seamless" and seamless_available():
        # Seamless research returns emails — skip pattern/Hunter pre-steps
        from .seamless import max_contacts_per_company, search_and_import_seamless_for_companies

        # Request full company list; allocate_research_slots applies
        # per-run cap + small overage. Do not pre-clamp to SEAMLESS_KEIRA_RESEARCH_LIMIT.
        research_n = min(
            max(len(names) * max_contacts_per_company(agent), len(names), 1),
            max(limit, len(names)),
        )
        emit(f"Contact — Seamless people search ({len(names)} companies, up to {research_n} research)…")
        try:
            web_step = search_and_import_seamless_for_companies(
                agent,
                names,
                limit=research_n,
                skip_existing=skip_existing,
                on_progress=emit,
            )
            web_step["fallback"] = False
            if int(web_step.get("researched") or 0) == 0 and int(web_step.get("imported") or 0) == 0:
                emit("Seamless returned no researched contacts — web fallback…")
                from .web_contacts import search_and_import_web_contacts

                web_fb = search_and_import_web_contacts(
                    agent, names, limit=limit, skip_existing=skip_existing, on_progress=emit,
                )
                web_fb["fallback"] = True
                web_fb["fallback_reason"] = "seamless_empty_or_budget"
                web_fb["seamless"] = web_step
                web_step = web_fb
        except SeamlessError as e:
            emit(f"Seamless failed ({e}) — falling back to web research")
            from .web_contacts import search_and_import_web_contacts

            web_step = search_and_import_web_contacts(
                agent, names, limit=limit, skip_existing=skip_existing, on_progress=emit,
            )
            web_step["fallback"] = True
            web_step["fallback_reason"] = str(e)
    else:
        emit("Contact 1/4 — Resolve email patterns…")
        from .email_patterns import resolve_patterns_for_companies

        summary["patterns"] = resolve_patterns_for_companies(
            names, allow_hunter=True, on_progress=emit,
        )

        emit(f"Contact 2/4 — People search ({mode})…")
        if mode == "web":
            from .web_contacts import search_and_import_web_contacts

            web_step = search_and_import_web_contacts(
                agent, names, limit=limit, skip_existing=skip_existing, on_progress=emit,
            )
            web_step["fallback"] = False
        else:
            from .contacts import search_and_import_contacts_for_companies

            emit("Using paid contact APIs (Apollo/PDL)")
            web_step = search_and_import_contacts_for_companies(
                agent, names, limit=limit, skip_existing=skip_existing, on_progress=emit,
            )
            web_step["fallback"] = True
            web_step["fallback_reason"] = "KEIRA_CONTACT_DISCOVERY=paid"

    summary["imported"] += web_step.get("imported", 0)
    summary["updated"] += web_step.get("updated", 0)
    summary["people_search"] = web_step
    if web_step.get("budget_alert"):
        summary["budget_alert"] = web_step["budget_alert"]
    if web_step.get("alerts"):
        summary["alerts"] = list(web_step.get("alerts") or [])

    # Hunter people search — off by default in Seamless mode (same as Woodway)
    from .hunter_api import hunter_available
    from .hunter_contacts import enrich_contacts_via_hunter
    from .db import get_companies_needing_contacts

    still_need = [
        n for n in get_awaiting_contact_companies(agent, limit=max(len(names) * 2, 50))
        if n and n in set(names)
    ]
    needing = [
        n for n in get_companies_needing_contacts(agent, limit=max(len(names) * 2, 50))
        if n in set(names)
    ]
    hunter_targets = list(dict.fromkeys(still_need + needing))

    hunter_fallback = os.getenv("HUNTER_PEOPLE_SEARCH", "true").lower() not in ("0", "false", "no")
    if mode == "seamless":
        hunter_fallback = os.getenv("HUNTER_PEOPLE_SEARCH", "false").lower() in ("1", "true", "yes")

    if hunter_available() and hunter_targets and hunter_fallback:
        emit(f"Contact — Hunter people search for {len(hunter_targets)} companies…")
        hunter_step = enrich_contacts_via_hunter(
            agent, hunter_targets, config=config, limit=len(hunter_targets), on_progress=emit,
        )
        summary["hunter_contacts"] = hunter_step
        summary["imported"] += hunter_step.get("imported", 0)
        summary["updated"] += hunter_step.get("updated", 0)
    elif mode == "seamless" and hunter_targets:
        emit(
            f"Skipping Hunter for {len(hunter_targets)} still-awaiting "
            f"(Seamless mode — set HUNTER_PEOPLE_SEARCH=true to enable)"
        )
        summary["hunter_contacts"] = {"skipped": True, "reason": "seamless_mode"}
    else:
        emit("Contact — Hunter people search skipped")
        summary["hunter_contacts"] = {"skipped": True}

    # Pattern-infer / privacy for name-only gaps
    emit("Contact — Pattern-infer emails + privacy footprint…")
    from .contact_enrich import enrich_imported_contacts

    _reopen_for_email_fill(agent, names)
    enrich_step = enrich_imported_contacts(agent, names, on_progress=emit)
    summary["contact_enrich"] = enrich_step
    summary["skipped"] = False
    return summary


def _reopen_for_email_fill(agent: str, company_names: list[str]) -> int:
    """Allow email fill on qualified leads that still lack email/outreach."""
    names = set(company_names)
    n = 0
    for lead in get_leads(agent=agent):
        if lead.get("company") not in names:
            continue
        if lead.get("status") not in ("qualified", "drafted", "imported", "awaiting_contact"):
            continue
        if lead.get("email") and str(lead.get("email")).strip():
            continue
        if not lead.get("contact_name"):
            continue
        # Keep score/tier; mark imported so process_imported can draft once email lands
        set_lead_fields(lead["id"], status="imported")
        n += 1
    return n


def companies_needing_keira_contacts(agent: str = "keira", *, limit: int = 30) -> list[str]:
    """Companies missing an email (LinkedIn alone is not enough for Gmail drafts)."""
    from .db import get_connection, init_db

    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT company FROM leads
            WHERE agent = ?
              AND status NOT IN ('skipped', 'emailed', 'replied')
              AND company IS NOT NULL AND TRIM(company) != ''
              AND (
                contact_name IS NULL OR TRIM(contact_name) = ''
                OR email IS NULL OR TRIM(email) = ''
              )
            ORDER BY
              CASE WHEN score IS NOT NULL THEN score ELSE 0 END DESC,
              updated_at DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    return [r["company"] for r in rows if r["company"]]


def recontact_awaiting(
    *,
    agent: str = "keira",
    limit: int = 50,
    skip_existing: bool = False,
    on_progress=None,
    config: dict | None = None,
) -> dict:
    """Re-run contact search for awaiting_contact + leads still missing email."""
    emit = on_progress or (lambda msg: None)
    from .db import get_recontact_companies

    names = get_recontact_companies(agent, limit=max(1, limit))
    if not names:
        emit("No companies needing re-contact (awaiting or missing email)")
        return {
            "ok": True,
            "skipped": True,
            "imported": 0,
            "updated": 0,
            "companies": [],
            "awaiting": 0,
        }
    emit(f"Re-contact — {len(names)} companies (awaiting_contact / missing email)…")
    result = discover_keira_contacts(
        agent,
        names,
        limit=max(limit, len(names)),
        config=config,
        skip_existing=skip_existing,
        on_progress=emit,
    )
    result["ok"] = True
    result["awaiting"] = len(names)
    emit(
        f"Re-contact done — +{result.get('imported', 0)} imported / "
        f"~{result.get('updated', 0)} updated across {len(names)} companies"
    )
    return result
