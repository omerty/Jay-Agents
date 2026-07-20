"""Actava-powered prospecting for Keira (no Seamless required).

Two modes (auto-selected):
  1. External agent — ACTAVA_AGENT_ID set → POST /v1/agents/{id}/run
  2. Discover + Cura — ACTAVA_API_KEY only → web search + Cura JSON extraction

Mode 2 finds owner-operated companies from public web signals (same spirit as
Discover, but extraction runs through Actava Cura instead of Groq).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import yaml

from .actava_api import (
    ActavaError,
    actava_agent_configured,
    actava_available,
    actava_status,
    cura_chat_json,
    extract_response_text,
    run_agent_and_wait,
)
from .db import build_prospect_string, company_seen, get_connection, init_db, upsert_lead
from .discover import _should_skip, discover_raw_hits
from .qualify import _geo_text, _industries_text, _size_text

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"


def _agent_config(agent: str) -> dict:
    with open(AGENTS_DIR / agent / "config.yaml") as f:
        return yaml.safe_load(f)


def _keira_prospect_prompt(config: dict, *, limit: int) -> str:
    actava_cfg = config.get("actava") or {}
    custom = (actava_cfg.get("prospect_prompt") or "").strip()
    if custom:
        return custom.format(limit=limit)

    icp = config.get("icp") or {}
    prompts = config.get("prompts") or {}
    qualify_extra = (prompts.get("qualify_extra") or "").strip()
    geo = _geo_text(icp) or "Ottawa, Eastern Ontario, Kingston, Cornwall"

    extra = f"\n\nAdditional scoring guidance:\n{qualify_extra}" if qualify_extra else ""

    return f"""You are sourcing M&A advisory prospects for {config['company']} ({config['product']}).

Find up to {limit} privately held businesses in {geo} where the OWNER or FOUNDER may be a
succession/exit candidate. Target valuation $10–100M (roughly 20–500 employees).

Focus industries: manufacturing, construction, industrial distribution, logistics, HVAC,
commercial services, B2B — sector-agnostic is fine if size/geography fit.

Skip: M&A brokers, accounting/law firms, PE firms, marketplaces, government, nonprofits.{extra}

Return ONLY valid JSON (no markdown):
{{
  "leads": [
    {{
      "company": "Company Name",
      "contact_name": "First Last or null",
      "contact_title": "Owner|Founder|President|CEO or null",
      "industry": "industry",
      "employee_count": null,
      "linkedin_url": null,
      "signal": "why they may be an exit candidate — cite specific evidence"
    }}
  ]
}}"""


def _parse_leads_json(text: str) -> list[dict]:
    from .llm import _parse_json

    if not text or not text.strip():
        return []

    def _coerce(data) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("leads") or data.get("contacts") or data.get("prospects") or []
        return []

    try:
        return _coerce(_parse_json(text))
    except Exception:
        pass

    # Actava agents often append {"leads":[...]} after long narrative output.
    for match in reversed(list(re.finditer(r'\{\s*"leads"\s*:\s*\[', text))):
        chunk = text[match.start() :]
        depth = 0
        end = None
        for i, ch in enumerate(chunk):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if not end:
            continue
        raw = chunk[:end]
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", raw)
        try:
            return _coerce(json.loads(cleaned))
        except json.JSONDecodeError:
            continue

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", match.group(0))
    try:
        return _coerce(json.loads(cleaned))
    except json.JSONDecodeError:
        return []


def _lead_record(raw: dict, config: dict) -> dict | None:
    company = (raw.get("company") or "").strip()
    if not company or _should_skip(company, config):
        return None

    contact_name = (raw.get("contact_name") or raw.get("name") or "").strip() or None
    contact_title = (raw.get("contact_title") or raw.get("title") or "").strip() or None
    email = (raw.get("email") or "").strip() or None
    linkedin = (raw.get("linkedin_url") or raw.get("liUrl") or "").strip() or None

    emp = raw.get("employee_count")
    if emp is not None and not isinstance(emp, int):
        try:
            emp = int(re.sub(r"[^\d]", "", str(emp))) or None
        except (TypeError, ValueError):
            emp = None

    signal = (raw.get("signal") or raw.get("reason") or "Actava prospect match").strip()

    return {
        "company": company,
        "contact_name": contact_name,
        "contact_title": contact_title,
        "email": email,
        "linkedin_url": linkedin,
        "employee_count": emp,
        "industry": (raw.get("industry") or "").strip() or None,
        "signal": f"Actava — {signal}",
        "source": "actava",
        "status": "imported",
    }


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


def _import_leads(raw_leads: list[dict], agent: str, config: dict, *, skip_existing: bool) -> dict:
    imported = updated = skipped = with_email = 0
    for raw in raw_leads:
        lead = _lead_record(raw, config)
        if not lead:
            skipped += 1
            continue
        if skip_existing and _contact_seen(lead, agent):
            skipped += 1
            continue
        record = dict(lead)
        record["prospect"] = build_prospect_string(record)
        _, created = upsert_lead(record, agent=agent)
        if created:
            imported += 1
        else:
            updated += 1
        if record.get("email"):
            with_email += 1
    return {
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "with_email": with_email,
    }


def _search_via_agent(agent: str, config: dict, *, limit: int, emit) -> list[dict]:
    agent_id = os.getenv("ACTAVA_AGENT_ID", "").strip()
    if not agent_id:
        raise ActavaError("ACTAVA_AGENT_ID not set")

    prompt = _keira_prospect_prompt(config, limit=limit)
    emit(f"Running Actava agent {agent_id[:8]}…")
    result = run_agent_and_wait(agent_id, prompt, on_progress=emit)
    text = extract_response_text(result)
    leads = _parse_leads_json(text)
    emit(f"Agent returned {len(leads)} prospect(s)")
    return leads


def _search_via_discover_cura(agent: str, config: dict, *, limit: int, emit) -> list[dict]:
    discover_cfg = config.get("discover") or {}
    actava_cfg = config.get("actava") or {}
    max_queries = actava_cfg.get("max_queries") or discover_cfg.get("max_queries") or 8
    results_per_query = actava_cfg.get("results_per_query") or discover_cfg.get("results_per_query") or 5

    emit(f"Web search ({max_queries} queries) + Actava Cura extraction…")
    hits = discover_raw_hits(config, max_queries=max_queries, results_per_query=results_per_query)
    emit(f"{len(hits)} search hits collected")

    if not hits:
        return []

    icp = config.get("icp") or {}
    prompts = config.get("prompts") or {}
    qualify_extra = (prompts.get("qualify_extra") or "").strip()
    extra_block = f"\nCUSTOM INSTRUCTIONS:\n{qualify_extra}\n" if qualify_extra else ""

    snippets = []
    for i, h in enumerate(hits[:30], 1):
        snippets.append(
            f"{i}. [{h['query']}]\n   Title: {h['title']}\n   Snippet: {h['snippet'][:280]}"
        )

    prompt = f"""Extract owner-operated business prospects for {config['company']} — {config['product']}.
Tagline: {config['tagline']}

IDEAL CUSTOMER PROFILE:
- Industries: {_industries_text(icp)}
- Target titles: {', '.join(icp.get('titles', []))}
- Company size: {_size_text(icp)}
- Geography: {_geo_text(icp) or 'Ottawa / Eastern Ontario'}
- Blocklist: {', '.join(config.get('blocklist') or [])}
{extra_block}
SEARCH RESULTS:
{chr(10).join(snippets)}

Rules:
- Only companies mentioned in the results — do NOT invent names
- Prefer owner/founder/succession/retirement signals
- Return at most {limit} leads
- Include contact_name/title when visible in snippets

JSON only:
{{
  "leads": [
    {{
      "company": "...",
      "contact_name": null,
      "contact_title": "Owner",
      "industry": "...",
      "signal": "...",
      "employee_count": null,
      "linkedin_url": null
    }}
  ]
}}"""

    system = prompts.get("qualify_system") or "Extract B2B prospects. Output strict JSON only."
    emit("Extracting prospects with Actava Cura…")
    result = cura_chat_json(prompt, system=system)
    return result.get("leads") or []


def search_and_import_actava(
    agent: str = "keira",
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Import Keira prospects via Actava (agent run or discover+Cura)."""
    if not actava_available():
        raise ActavaError("ACTAVA_API_KEY not set — add your key from app.actava.ai to .env")

    emit = on_progress or (lambda msg: None)
    config = _agent_config(agent)
    init_db()

    if actava_agent_configured():
        raw_leads = _search_via_agent(agent, config, limit=limit, emit=emit)
        mode = "agent"
    else:
        raw_leads = _search_via_discover_cura(agent, config, limit=limit, emit=emit)
        mode = "discover+cura"

    counts = _import_leads(raw_leads, agent, config, skip_existing=skip_existing)
    emit(
        f"Done — {counts['imported']} imported, {counts['updated']} updated, "
        f"{counts['skipped']} skipped ({counts['with_email']} with email)"
    )

    return {
        "provider": "actava",
        "mode": mode,
        "searched": len(raw_leads),
        "credits_used": 0,
        **counts,
        "errors": [],
    }


# re-export for web health
def actava_status_export() -> dict:
    return actava_status()
