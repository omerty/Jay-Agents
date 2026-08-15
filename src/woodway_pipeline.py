"""Woodway end-to-end pipeline.

Flow:
  1. Company discovery — DuckDuckGo + Claude (default) or optional Actava agent
  2. Digest → LLM ranks / keeps the best ICP fits
  3. **Contacts** — web search + Claude (default), or Seamless / Apollo / PDL if configured
  4. Process imported → qualify + generate outreach emails
  5. Create Microsoft 365 / Outlook drafts (Gmail only if Outlook not connected)

Nothing is sent automatically — user reviews drafts and clicks send.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from .actava_api import ActavaError, actava_available
from .db import (
    get_awaiting_contact_companies,
    get_companies_needing_contacts,
    get_leads,
    merge_duplicate_domains,
    normalize_company,
    purge_excluded_leads,
    set_lead_fields,
    skip_actava_company_placeholders,
)
from .llm import LLMError, chat_json
from .llm_optimize import task_defaults, truncate_context
from .seamless_api import SeamlessError, seamless_available

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"
DRAFT_MIN_SCORE = int(os.getenv("DAILY_DRAFT_MIN_SCORE", "50"))

# Approachable ICP seed pool — tops up discovery to ~50 companies/day
WOODWAY_SEED_COMPANIES = [
    # Regional / mid-large banks & credit unions
    "Regions Bank", "Fifth Third Bank", "Huntington Bancshares", "KeyBank",
    "PNC Financial Services", "M&T Bank", "Citizens Financial Group",
    "Ally Financial", "Synchrony Financial", "Discover Financial Services",
    "Comerica", "Zions Bancorporation", "First Horizon", "Webster Bank",
    "Popular Inc", "Eastern Bank", "Valley National Bank", "Flagstar Bank",
    "PenFed Credit Union", "Navy Federal Credit Union", "SchoolsFirst FCU",
    # Health systems
    "Froedtert Health", "Sanford Health", "Advocate Health", "Banner Health",
    "Geisinger", "Corewell Health", "Intermountain Health", "Sutter Health",
    "Northwell Health", "CommonSpirit Health", "Ascension", "Trinity Health",
    "Providence Health", "Bon Secours Mercy Health", "Atrium Health",
    "Hackensack Meridian Health", "Mass General Brigham", "UPMC",
    "Cleveland Clinic", "Ochsner Health", "Baylor Scott & White Health",

    "Henry Ford Health", "Main Line Health",
    # Insurers / specialty / mutuals
    "Unum", "Lincoln Financial", "Principal Financial", "Cincinnati Financial",
    "Highmark", "Horizon Blue Cross Blue Shield", "GuideWell",
    "Health Care Service Corporation", "Blue Cross Blue Shield of Michigan",
    "Independence Blue Cross", "Cambia Health Solutions", "Point32Health",
    "EmblemHealth", "Mutual of Omaha", "Guardian Life", "MassMutual",
    "New York Life", "Pacific Life", "Thrivent", "Auto-Owners Insurance",
    "Erie Insurance", "American Family Insurance", "Nationwide Mutual", "USAA",
    # Specialty pharma / biotech
    "Incyte", "BioMarin", "Alkermes", "Exelixis", "Ionis Pharmaceuticals",
    "Vertex Pharmaceuticals", "Regeneron", "Biogen", "Sarepta Therapeutics",
    "Jazz Pharmaceuticals", "Ultragenyx", "Blueprint Medicines",
    "Neurocrine Biosciences", "Exact Sciences", "Guardant Health", "Illumina",
    # Data / analytics / aggregators
    "Verisk", "IQVIA", "LexisNexis Risk Solutions", "TransUnion", "Equifax",
    "Nielsen", "Kantar", "Cotiviti", "Change Healthcare", "Premier Inc",
    "Health Catalyst", "Datavant", "Acxiom", "Epsilon", "LiveRamp",
    "Dun & Bradstreet",
]

def woodway_company_discovery_mode() -> str:
    """How Woodway step 1 finds companies: anthropic (fast) or actava (slow agent run)."""
    raw = (os.getenv("WOODWAY_COMPANY_DISCOVERY") or "anthropic").strip().lower()
    if raw in ("actava", "agent"):
        return "actava"
    return "anthropic"


def woodway_contact_discovery_mode() -> str:
    """How Woodway step 3 finds people: auto, web, seamless, or paid (apollo/pdl)."""
    raw = (os.getenv("WOODWAY_CONTACT_DISCOVERY") or "auto").strip().lower()
    if raw in ("web", "seamless", "paid", "apollo", "pdl"):
        return "paid" if raw in ("apollo", "pdl") else raw
    # auto — Seamless (OAuth or API key) when connected, else free web research
    if seamless_available():
        return "seamless"
    return "web"


def _agent_config(agent: str = "woodway") -> dict:
    with open(AGENTS_DIR / agent / "config.yaml") as f:
        return yaml.safe_load(f)


def digest_companies(
    companies: list[dict],
    config: dict,
    *,
    limit: int,
    on_progress=None,
) -> list[dict]:
    """Rank Actava companies with the LLM; return keepers (up to limit)."""
    emit = on_progress or (lambda msg: None)
    if not companies:
        return []

    # Dedupe by normalized company name and domain; drop negative-list noise
    from .db import normalize_company
    from .email_patterns import normalize_domain, resolve_company_domain
    from .negative_list import is_excluded

    seen_names: set[str] = set()
    seen_domains: set[str] = set()
    unique = []
    for c in companies:
        name = (c.get("company") or "").strip()
        if not name:
            continue
        key = normalize_company(name)
        domain = normalize_domain(c.get("company_domain")) or normalize_domain(resolve_company_domain(name))
        extra = f"{c.get('industry') or ''} {c.get('description') or ''} {c.get('signal') or ''}"
        # Don't call passes_icp_gates here — domain_seen would reject already-imported keepers
        if is_excluded(name, agent=config.get("name") or "woodway", extra_text=extra):
            continue
        emp = c.get("employee_count")
        min_emp = int((config.get("icp") or {}).get("min_employees") or 1000)
        if emp is not None and int(emp) < min_emp:
            continue
        if key in seen_names:
            continue
        if domain and domain in seen_domains:
            continue
        seen_names.add(key)
        if domain:
            seen_domains.add(domain)
        unique.append(c)

    if len(unique) <= limit:
        emit(f"Digest — keeping all {len(unique)} companies (under limit)")
        return unique[:limit]

    emit(f"Digest — ranking {len(unique)} companies down to {limit}…")
    icp = config.get("icp") or {}
    defaults = task_defaults("digest")
    lines = []
    for i, c in enumerate(unique, 1):
        lines.append(
            f"{i}. {c.get('company')} | industry={c.get('industry') or 'n/a'} | "
            f"{(c.get('description') or c.get('signal') or '')[:180]}"
        )

    system = config.get("prompts", {}).get("qualify_system") or "Filter B2B companies. Output strict JSON only."
    system = f"""{system}

Filter companies for {config['company']} ({config['product']}).
ICP industries: {', '.join(icp.get('industries') or [])}
Target roles (later): {', '.join(icp.get('titles') or [])}
Company size: prefer ~{icp.get('prefer_employees_min', 1500)}–{icp.get('prefer_employees_max', 10000)} employees (regional / mid-large).
Megabrands are allowed — just do not return an all-megabrand list when regional peers exist.
Geography: {icp.get('geography')}"""

    companies_block = truncate_context("\n".join(lines), defaults["context_chars"])
    from .retrieval import known_funnel_for_discovery

    already = known_funnel_for_discovery(
        config.get("name") or "woodway",
        limit=15,
        hint_text=companies_block,
    )
    already_block = f"\n{already}\nPrefer companies NOT already in the pipeline.\n" if already else ""

    prompt = f"""Keep the best {limit} ICP fits from this list.
Aim for a MIX: majority approachable regional/mid-large (banks, health systems, specialty pharma, insurers)
when those peers exist; household megabrands are fine for remaining slots — never drop a strong
industry+signal megabrand solely for fame. Drop weak / tiny / off-ICP names.
{already_block}
COMPANIES:
{companies_block}

Return JSON only:
{{
  "keep": [
    {{"company": "Exact name from list", "reason": "one short reason"}}
  ]
}}"""

    try:
        result = chat_json(
            prompt,
            system=system,
            max_tokens=defaults["max_tokens"],
            temperature=defaults["temperature"],
        )
    except LLMError as e:
        logger.warning("Digest LLM failed (%s) — keeping gated companies only", e)
        emit(f"Digest LLM unavailable — applying hard ICP gates to {len(unique)} companies")
        from .icp_gates import min_employees
        from .negative_list import is_excluded

        gated = []
        min_emp = min_employees(config)
        for c in unique:
            name = c.get("company") or ""
            extra = f"{c.get('industry') or ''} {c.get('description') or ''} {c.get('signal') or ''}"
            if is_excluded(name, agent=config.get("name") or "woodway", extra_text=extra):
                continue
            emp = c.get("employee_count")
            if emp is not None and int(emp) < min_emp:
                continue
            # Prefer companies with a real signal/description over empty shells
            if not (c.get("signal") or c.get("description") or c.get("industry")):
                continue
            gated.append(c)
            if len(gated) >= limit:
                break
        if not gated:
            # Last resort: first N that aren't excluded (still better than raw first N)
            gated = [
                c for c in unique
                if not is_excluded(c.get("company") or "", agent=config.get("name") or "woodway")
            ][:limit]
        return gated

    keep_names = []
    for item in result.get("keep") or []:
        name = (item.get("company") or "").strip()
        if name:
            keep_names.append(name.lower())

    if not keep_names:
        emit("Digest returned no keepers — falling back to gated companies")
        from .negative_list import is_excluded
        return [
            c for c in unique
            if not is_excluded(c.get("company") or "", agent=config.get("name") or "woodway")
        ][:limit]

    # Apply learning weights: prefer industries that convert
    try:
        from .outcomes import get_learning_weights
        weights = get_learning_weights(config.get("name") or "woodway").get("industry_weights") or {}
        if weights:
            def _ind_w(c):
                ind = (c.get("industry") or "unknown").lower()
                return float(weights.get(ind, 1.0))
            # Re-order keep_names candidates by weight when filling
            unique_sorted = sorted(unique, key=_ind_w, reverse=True)
            by_name_pref = {c["company"].strip().lower(): c for c in unique_sorted}
        else:
            by_name_pref = {c["company"].strip().lower(): c for c in unique}
    except Exception:
        by_name_pref = {c["company"].strip().lower(): c for c in unique}

    by_name = by_name_pref
    kept = []
    for key in keep_names:
        if key in by_name:
            kept.append(by_name[key])
        if len(kept) >= limit:
            break

    # Fill if model skipped some
    if len(kept) < min(limit, len(unique)):
        for c in unique:
            if c not in kept:
                kept.append(c)
            if len(kept) >= limit:
                break

    emit(f"Digest — kept {len(kept)} companies")
    return kept


def create_mailbox_drafts(
    agent: str = "woodway",
    *,
    prefer_microsoft: bool = True,
    companies: set[str] | list[str] | None = None,
    require_company_allowlist: bool = False,
) -> dict:
    """Create mailbox drafts for drafted leads. Woodway prefers Outlook.

    Hard rule: never call send_message / send_draft. Humans send from the dashboard.

    If `require_company_allowlist` is True (Keira draft gate), only companies in
    `companies` get mailbox drafts — soft enrich-only leads stay out of the inbox.
    """
    from .contact_tier import classify_contact_tier, tier_allows_email_draft
    from .email_verify import verify_enabled, verify_lead_email
    # Import create_draft only — send_* must stay unreachable from this module.
    from .gmail_api import GmailError, create_draft, gmail_status
    from .microsoft_mail_api import MicrosoftMailError, create_draft as ms_create_draft, microsoft_status

    ms_ok = microsoft_status()["connected"]
    gmail_ok = gmail_status()["connected"]

    if prefer_microsoft and ms_ok:
        use_ms, use_gmail = True, False
    elif gmail_ok:
        use_ms, use_gmail = False, True
    elif ms_ok:
        use_ms, use_gmail = True, False
    else:
        return {"created": 0, "failed": 0, "provider": None, "detail": "No mailbox connected"}

    allow = {(c or "").strip().lower() for c in (companies or []) if (c or "").strip()}
    if require_company_allowlist and not allow:
        return {
            "created": 0,
            "failed": 0,
            "provider": "microsoft" if use_ms else "gmail",
            "skipped_draft_gate": 0,
            "detail": "No critic draft-approved companies",
        }

    created = failed = skipped_tier = skipped_verify = skipped_draft_gate = 0
    provider = "microsoft" if use_ms else "gmail"
    for lead in get_leads(agent=agent, status="drafted"):
        if require_company_allowlist or allow:
            co = (lead.get("company") or "").strip().lower()
            if co not in allow:
                skipped_draft_gate += 1
                continue
        tier = classify_contact_tier(lead)
        if not tier_allows_email_draft(tier):
            skipped_tier += 1
            continue
        if not lead.get("email") or lead.get("gmail_draft_id"):
            continue
        if (lead.get("score") or 0) < DRAFT_MIN_SCORE or not lead.get("outreach_body"):
            continue

        # Verify before creating mailbox draft
        if verify_enabled() and not lead.get("email_verified"):
            v = verify_lead_email(lead)
            if not v.get("ok"):
                skipped_verify += 1
                logger.info("Skip draft lead %s — email verify failed: %s", lead["id"], v.get("detail"))
                continue
            tier = v.get("contact_tier") or classify_contact_tier({**lead, **(v.get("patch") or {})})
            if not tier_allows_email_draft(tier):
                skipped_tier += 1
                continue

        subject = lead.get("outreach_subject") or "Quick question"
        if tier == "B" or lead.get("email_inferred"):
            if not subject.lower().startswith("[verify email]"):
                subject = f"[Verify email] {subject}"
        try:
            if use_ms:
                result = ms_create_draft(
                    lead["email"],
                    subject,
                    lead["outreach_body"],
                )
            else:
                result = create_draft(
                    lead["email"],
                    subject,
                    lead["outreach_body"],
                )
        except (GmailError, MicrosoftMailError) as e:
            logger.warning("Draft failed for lead %s: %s", lead["id"], e)
            failed += 1
            continue
        set_lead_fields(
            lead["id"],
            gmail_draft_id=result["draft_id"],
            gmail_thread_id=result.get("thread_id"),
            gmail_message_id=result.get("message_id"),
            mail_provider=provider,
        )
        created += 1
    out = {
        "created": created,
        "failed": failed,
        "provider": provider,
        "skipped_tier": skipped_tier,
        "skipped_verify": skipped_verify,
    }
    if require_company_allowlist or allow:
        out["skipped_draft_gate"] = skipped_draft_gate
    return out


def run_woodway_pipeline(
    *,
    limit: int = 50,
    use_llm: bool = True,
    skip_existing: bool = True,
    create_drafts: bool = True,
    on_progress=None,
    run_id: str | None = None,
    resume: bool = True,
) -> dict:
    """Run the full Woodway pipeline. Safe when Seamless/Claude keys are missing."""
    import uuid

    from .db import save_checkpoint, get_checkpoint, resolve_awaiting_contact_shells, prune_extra_contacts_per_company
    from .pipeline_preflight import PipelinePreflightError, assert_mailbox_ready
    from .run_costs import set_run_context, BudgetExceededError

    emit = on_progress or (lambda msg: None)
    agent = "woodway"
    config = _agent_config(agent)
    run_id = run_id or str(uuid.uuid4())[:12]
    set_run_context(run_id, "pipeline")
    summary: dict = {"agent": agent, "run_id": run_id, "steps": {}}

    try:
        mb = assert_mailbox_ready(create_drafts=create_drafts)
        if mb.get("ready"):
            emit("Preflight — mailbox ready for drafts")
    except PipelinePreflightError as e:
        emit(f"Preflight abort — {e}")
        summary["ok"] = False
        summary["error"] = str(e)
        summary["preflight"] = {"mailbox": False, "detail": str(e)}
        return summary

    dedupe = merge_duplicate_domains(agent)
    if dedupe.get("deleted"):
        emit(f"Dedupe — merged {dedupe['deleted']} duplicate domain row(s)")

    purged = purge_excluded_leads(agent)
    if purged.get("purged"):
        emit(f"ICP purge — skipped {purged['purged']} negative-list / vendor row(s)")
    summary["steps"]["cleanup"] = {**dedupe, **purged}

    # Resume: skip completed steps when same run_id has checkpoints
    resumed_from: set[str] = set()
    if resume:
        for step_name in ("discovery", "digest", "contacts", "process", "drafts"):
            if get_checkpoint(agent, run_id, step_name):
                resumed_from.add(step_name)
        if resumed_from:
            emit(f"Resume — found checkpoints for: {', '.join(sorted(resumed_from))}")

    # ---- 1. Company discovery (Claude + web search by default; Actava optional) ----
    if "discovery" in resumed_from:
        discovery_cp = get_checkpoint(agent, run_id, "discovery") or {}
        companies = discovery_cp.get("company_list") or []
        summary["steps"]["discovery"] = {**discovery_cp, "resumed": True}
        emit(f"Step 1/5 — resumed discovery ({len(companies)} companies)")
    else:
        discovery_mode = woodway_company_discovery_mode()
        if discovery_mode == "actava":
            emit("Step 1/5 — Actava company discovery…")
            if not actava_available():
                raise ActavaError(
                    "ACTAVA_API_KEY not set — set WOODWAY_COMPANY_DISCOVERY=anthropic or add Actava key"
                )
            from .actava import search_and_import_actava

            discovery_result = search_and_import_actava(
                agent, limit=limit, skip_existing=skip_existing, on_progress=emit
            )
        else:
            emit("Step 1/5 — Web search + Claude company discovery…")
            from .discover import search_and_import_companies

            discovery_result = search_and_import_companies(
                agent, limit=limit, skip_existing=skip_existing, on_progress=emit
            )

        companies = discovery_result.get("companies") or []
        summary["steps"]["discovery"] = {
            "provider": discovery_result.get("provider") or discovery_mode,
            "mode": discovery_result.get("mode"),
            "searched": discovery_result.get("searched"),
            "imported": discovery_result.get("imported"),
            "companies": len(companies),
            "company_list": companies,
        }
        save_checkpoint(agent, run_id, "discovery", summary["steps"]["discovery"])

    # ---- 2. Digest ----
    emit("Step 2/5 — Digesting companies…")
    if use_llm:
        kept = digest_companies(companies, config, limit=limit, on_progress=emit)
    else:
        kept = companies[:limit]
        emit(f"Digest skipped (mock) — keeping {len(kept)} companies")

    # Mark company-only Actava rows not kept by digest (single SQL pass)
    kept_keys = {normalize_company(c["company"]) for c in kept if c.get("company")}
    skipped_cos = skip_actava_company_placeholders(agent, kept_keys)
    if skipped_cos:
        emit(f"Digest — {skipped_cos} company-only rows marked awaiting_contact (not in keep list)")

    company_names = [c["company"].strip() for c in kept if c.get("company")]

    # Drop negative-list leaks that survived extraction (e.g. "Axtria, Inc.")
    from .negative_list import is_excluded

    company_names = [
        n for n in company_names
        if not is_excluded(n, agent=agent)
    ]

    # Retry companies still missing a person / channel
    for name in get_companies_needing_contacts(agent, limit=limit * 4):
        if name not in company_names and not is_excluded(name, agent=agent):
            company_names.append(name)
    for name in get_awaiting_contact_companies(agent, limit=limit * 4):
        if name not in company_names and not is_excluded(name, agent=agent):
            company_names.append(name)

    # Always top up to `limit` with approachable ICP seeds (volume + mix)
    from .db import company_seen, upsert_lead, build_prospect_string, get_connection, init_db
    from .email_patterns import resolve_company_domain
    from .enrich import is_mega_brand

    mega_n = sum(1 for n in company_names if is_mega_brand(n, config))
    if len(company_names) < limit:
        emit(
            f"Seed fill — approachable ICP accounts "
            f"({len(company_names)}/{limit} companies, {mega_n} megabrand)…"
        )
        for name in WOODWAY_SEED_COMPANIES:
            if len(company_names) >= limit:
                break
            if is_excluded(name, agent=agent):
                continue
            if name in company_names:
                continue
            domain = resolve_company_domain(name)
            # Skip only if we already have an emailable contact
            if company_seen(name, agent, domain=domain):
                init_db()
                with get_connection() as conn:
                    row = conn.execute(
                        """
                        SELECT 1 FROM leads WHERE agent = ? AND company_normalized = ?
                          AND email IS NOT NULL AND TRIM(email) != ''
                        LIMIT 1
                        """,
                        (agent, normalize_company(name)),
                    ).fetchone()
                if row:
                    continue
            upsert_lead({
                "company": name,
                "company_domain": domain,
                "source": "icp_seed",
                "status": "awaiting_contact",
                "signal": "ICP seed — approachable enterprise buyer account",
                "prospect": build_prospect_string({"company": name}),
            }, agent=agent)
            company_names.append(name)

    summary["steps"]["digest"] = {
        "input": len(companies),
        "kept": len(company_names),
        "companies": company_names,
    }
    save_checkpoint(agent, run_id, "digest", summary["steps"]["digest"])

    # ---- 3. Contacts — Seamless first when configured; patterns/Hunter only as fallbacks ----
    emit("Step 3/5 — Finding people at those companies…")
    contact_step: dict = {"imported": 0, "updated": 0}
    contact_mode = woodway_contact_discovery_mode()

    if not company_names:
        contact_step = {"imported": 0, "updated": 0, "skipped": True, "fallback": False}
        emit("No companies to enrich — skipping contact step")
    else:
        web_step: dict
        if contact_mode == "seamless" and seamless_available():
            # Seamless research returns emails — skip pattern/Hunter pre-steps (saves Hunter credits)
            from .seamless import search_and_import_seamless_for_companies

            try:
                from .seamless_budget import max_research_per_run

                research_n = max(limit, max_research_per_run(agent), len(company_names))
                web_step = search_and_import_seamless_for_companies(
                    agent,
                    company_names,
                    limit=research_n,
                    skip_existing=skip_existing,
                    on_progress=emit,
                )
                web_step["fallback"] = False

                # Gap fill: companies Seamless didn't cover get web contact search only
                gap_names = []
                init_db()
                with get_connection() as conn:
                    for name in company_names:
                        key = normalize_company(name)
                        row = conn.execute(
                            """
                            SELECT 1 FROM leads
                            WHERE agent = ? AND company_normalized = ?
                              AND contact_name IS NOT NULL AND TRIM(contact_name) != ''
                              AND status != 'skipped'
                            LIMIT 1
                            """,
                            (agent, key),
                        ).fetchone()
                        if not row:
                            gap_names.append(name)

                if gap_names:
                    emit(
                        f"Step 3a — Web contact fallback for {len(gap_names)} companies "
                        f"still missing a person after Seamless…"
                    )
                    from .web_contacts import search_and_import_web_contacts

                    gap_step = search_and_import_web_contacts(
                        agent,
                        gap_names,
                        limit=len(gap_names),
                        skip_existing=skip_existing,
                        on_progress=emit,
                    )
                    web_step["gap_fallback"] = gap_step
                    web_step["imported"] = web_step.get("imported", 0) + gap_step.get("imported", 0)
                    web_step["updated"] = web_step.get("updated", 0) + gap_step.get("updated", 0)
            except SeamlessError as e:
                emit(f"Seamless failed ({e}) — falling back to web research")
                from .web_contacts import search_and_import_web_contacts

                web_step = search_and_import_web_contacts(
                    agent, company_names, limit=limit, skip_existing=skip_existing, on_progress=emit,
                )
                web_step["fallback"] = True
                web_step["fallback_reason"] = str(e)
        else:
            # Non-Seamless paths: resolve patterns first (Hunter last), then web/paid
            emit("Step 3a — Resolve email patterns (PMC / site / privacy → Hunter last)…")
            from .email_patterns import resolve_patterns_for_companies

            pattern_step = resolve_patterns_for_companies(
                company_names,
                allow_hunter=True,
                on_progress=emit,
            )
            summary["steps"]["patterns"] = pattern_step

            if contact_mode == "web":
                from .web_contacts import search_and_import_web_contacts

                emit("Step 3b — Deep web research (DuckDuckGo + Claude + page fetch)…")
                web_step = search_and_import_web_contacts(
                    agent, company_names, limit=limit, skip_existing=skip_existing, on_progress=emit,
                )
                web_step["fallback"] = False
            else:
                from .contacts import search_and_import_contacts_for_companies

                emit("Using paid contact APIs (Apollo/PDL)")
                web_step = search_and_import_contacts_for_companies(
                    agent, company_names, limit=limit, skip_existing=skip_existing, on_progress=emit,
                )
                web_step["fallback"] = True
                web_step["fallback_reason"] = "WOODWAY_CONTACT_DISCOVERY=paid"

        contact_step["imported"] += web_step.get("imported", 0)
        contact_step["updated"] += web_step.get("updated", 0)
        contact_step.update({k: v for k, v in web_step.items() if k not in contact_step})

        # Hunter people search only when not on Seamless (or Seamless left gaps and flag enabled)
        from .hunter_api import hunter_available
        from .hunter_contacts import enrich_contacts_via_hunter

        still_need = [
            n for n in get_awaiting_contact_companies(agent, limit=max(len(company_names) * 2, 50))
            if n
        ]
        hunter_fallback = os.getenv("HUNTER_PEOPLE_SEARCH", "true").lower() not in ("0", "false", "no")
        # Default: skip Hunter when Seamless is primary (set HUNTER_PEOPLE_SEARCH=true explicitly to fill gaps)
        if contact_mode == "seamless":
            hunter_fallback = os.getenv("HUNTER_PEOPLE_SEARCH", "false").lower() in ("1", "true", "yes")
        if hunter_available() and still_need and hunter_fallback:
            emit(f"Step 3c — Hunter people search for {len(still_need)} still-awaiting companies…")
            hunter_step = enrich_contacts_via_hunter(
                agent, still_need, config=config, limit=len(still_need), on_progress=emit,
            )
            summary["steps"]["hunter_contacts"] = hunter_step
            contact_step["imported"] += hunter_step.get("imported", 0)
            contact_step["updated"] += hunter_step.get("updated", 0)
        elif contact_mode == "seamless" and still_need:
            emit(
                f"Skipping Hunter for {len(still_need)} still-awaiting "
                f"(Seamless mode — set HUNTER_PEOPLE_SEARCH=true to enable)"
            )
        elif not hunter_available():
            emit("Hunter not configured — skipping people search fallback")
    summary["steps"]["contacts"] = contact_step

    shells = resolve_awaiting_contact_shells(agent)
    if shells.get("resolved"):
        emit(f"Dedupe — resolved {shells['resolved']} awaiting_contact shell(s) with named contacts")
    from .seamless import max_contacts_per_company
    pruned = prune_extra_contacts_per_company(agent, keep=max_contacts_per_company(agent))
    if pruned.get("skipped"):
        emit(f"Dedupe — skipped {pruned['skipped']} extra contact(s) (keep {pruned['keep']}/company)")
    summary["steps"]["shell_resolve"] = {**shells, **pruned}
    save_checkpoint(agent, run_id, "contacts", contact_step)
    # ---- 3b. Email patterns + privacy footprint (fills gaps; skip junk when Seamless already emailed) ----
    emit("Step 3b — Email patterns + privacy disclosures…")
    from .contact_enrich import enrich_imported_contacts

    enrich_step = enrich_imported_contacts(agent, company_names, on_progress=emit)
    summary["steps"]["contact_enrich"] = enrich_step
    save_checkpoint(agent, run_id, "contact_enrich", enrich_step)

    # ---- 3c. Signal detection ----
    emit("Step 3c — Detecting trigger signals…")
    from .signals import detect_signals_for_companies

    try:
        signal_step = detect_signals_for_companies(company_names, agent=agent, on_progress=emit)
    except Exception as e:
        logger.warning("Signal detection failed: %s", e)
        signal_step = {"detected": 0, "error": str(e)}
    summary["steps"]["signals"] = signal_step
    save_checkpoint(agent, run_id, "signals", signal_step)

    # ---- 4. Qualify + generate emails ----
    emit("Step 4/5 — Qualifying contacts and generating outreach…")
    from .agent import run_process_imported

    processed = run_process_imported(
        agent,
        limit=limit,
        use_llm=use_llm,
        require_identity=True,
        on_progress=emit,
    )
    summary["steps"]["process"] = {
        "count": processed.get("count", 0),
    }
    save_checkpoint(agent, run_id, "process", summary["steps"]["process"])

    # ---- 5. Outlook drafts ----
    drafts = {"created": 0, "failed": 0, "provider": None}
    if create_drafts:
        emit("Step 5/5 — Creating Microsoft 365 / Outlook drafts…")
        drafts = create_mailbox_drafts(agent, prefer_microsoft=True)
        if drafts["created"]:
            emit(
                f"Created {drafts['created']} {drafts['provider']} draft(s) — "
                "open Outlook and send when ready"
            )
        elif drafts.get("detail"):
            emit(f"Drafts skipped — {drafts['detail']}")
        else:
            emit("No new drafts to create (need email + score ≥ 50 + outreach body)")
    else:
        emit("Step 5/5 — Draft creation skipped")
    summary["steps"]["drafts"] = drafts
    save_checkpoint(agent, run_id, "drafts", drafts)

    # ---- 5b. Advance due sequence touches (2–4) ----
    emit("Step 5b — Advancing due sequence touches…")
    try:
        from .sequences import advance_due_sequences
        seq_step = advance_due_sequences(
            agent=agent, limit=limit * 2, create_drafts=create_drafts, on_progress=emit,
        )
    except Exception as e:
        logger.warning("Sequence advance failed: %s", e)
        seq_step = {"advanced": 0, "error": str(e)}
    summary["steps"]["sequences"] = seq_step
    save_checkpoint(agent, run_id, "sequences", seq_step)

    from .run_costs import cost_dashboard
    from .retrieval import append_journal, maybe_compress_journal
    from .funnel_metrics import compute_funnel_from_summary, persist_funnel

    summary["costs"] = cost_dashboard(agent)
    summary["ok"] = True

    try:
        funnel = compute_funnel_from_summary(summary)
        persist_funnel(agent, run_id, funnel)
        summary["funnel"] = funnel
        emit(
            f"Funnel — keep={funnel.get('kept')} contacts={funnel.get('contacts')} "
            f"drafted={funnel.get('drafted')} rates={funnel.get('rates')}"
        )
    except Exception as e:
        logger.warning("Funnel metrics failed: %s", e)

    # Rolling journal — small cross-run memory, compressed when it grows
    try:
        contacts_n = contact_step.get("imported", 0) + contact_step.get("updated", 0)
        append_journal(
            agent,
            f"run {run_id}: discovery={summary['steps'].get('discovery', {}).get('imported', 0)} "
            f"companies, contacts={contacts_n}, processed={processed.get('count', 0)}, "
            f"drafts={drafts.get('created', 0)}, sequences={seq_step.get('advanced', 0)}; "
            f"patterns={summary['steps'].get('patterns', {})}",
            kind="note",
        )
        maybe_compress_journal(agent)
    except Exception:
        pass

    emit(
        f"Woodway pipeline done — {len(company_names)} companies, "
        f"{contact_step.get('imported', 0)} contacts imported, "
        f"{processed.get('count', 0)} processed, "
        f"{drafts.get('created', 0)} drafts"
    )
    return summary
