"""Find contacts via public web search — no Seamless/Apollo/PDL required.

For each target company:
  1. DuckDuckGo searches (LinkedIn + leadership snippets)
  2. Claude extracts contact_name, title, linkedin_url from results
  3. Optional: Hunter.io email lookup (1 credit/person) when enabled

Imports leads with source=web_research. Email is best-effort; LinkedIn-only is fine.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import yaml

from .db import build_prospect_string, get_connection, init_db, mark_company_awaiting_contact, upsert_lead
from .discover import _should_skip, search_web
from .contact_enrich import enrich_lead_contact_fields
from .email_patterns import infer_email_for_contact
from .llm import LLMError, chat_json
from .llm_optimize import task_defaults, truncate_context

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"
LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w\-%.]+", re.I)


def _agent_config(agent: str) -> dict:
    with open(AGENTS_DIR / agent / "config.yaml") as f:
        return yaml.safe_load(f)


def _target_titles(config: dict) -> list[str]:
    seamless = config.get("seamless") or {}
    apollo = config.get("apollo") or {}
    icp = config.get("icp") or {}
    titles = seamless.get("job_title") or apollo.get("person_titles") or icp.get("titles") or []
    return [t for t in titles if t][:6]


def _company_contact_queries(company: str, titles: list[str], *, agent: str = "woodway") -> list[str]:
    if (agent or "").strip().lower() == "keira":
        owner_titles = titles[:4] or ["Owner", "Founder", "President", "CEO"]
        title_blob = " OR ".join(f'"{t}"' for t in owner_titles)
        return [
            f'"{company}" ({title_blob}) site:linkedin.com/in',
            f'"{company}" (Owner OR Founder OR "owner operator" OR "owner-operated")',
            f'"{company}" ("family business" OR "family-owned" OR "privately held") owner OR founder',
            f'"{company}" (President OR CEO OR Founder) (About OR Leadership OR Team OR "our team")',
            f'"{company}" owner founder (Ottawa OR Kingston OR Cornwall OR "Eastern Ontario")',
            f'"{company}" "founded by" OR "family owned" OR succession owner',
        ]
    title_blob = " OR ".join(f'"{t}"' for t in titles[:4]) if titles else '"data governance" OR "privacy officer"'
    return [
        f'"{company}" ({title_blob}) site:linkedin.com/in',
        f'"{company}" "chief privacy officer" OR "VP data governance" OR "director of privacy"',
        f'"{company}" "data governance" leadership OR "head of data governance"',
        f'site:linkedin.com/in "{company}" privacy OR "data governance"',
        f'"{company}" "annual report" OR "10-K" data protection privacy officer',
        f'"{company}" DPO OR "data protection officer" contact',
        f'"{company}" "privacy officer" OR "head of privacy" OR "VP privacy"',
        f'"{company}" "information governance" OR "AI governance" director OR VP',
        f'"{company}" privacy@ OR dpo@ OR "data protection" email',
        f'"{company}" site:linkedin.com/in "data protection" OR GDPR OR "chief privacy"',
    ]


def _fetch_page_snippet(url: str, *, max_chars: int = 1500) -> str | None:
    """Fetch a result page for richer extraction (not just SERP snippet)."""
    if not url or not url.startswith("http"):
        return None
    try:
        import httpx

        r = httpx.get(
            url,
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": "JayAgents/1.0 (contact research)"},
        )
        if r.status_code >= 400:
            return None
        text = r.content[:80_000].decode("utf-8", errors="ignore")
        text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars] if text else None
    except Exception:
        return None


def _contact_seen(contact_name: str, company: str, agent: str) -> bool:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE agent = ? AND contact_name = ? AND company = ?",
            (agent, contact_name, company),
        ).fetchone()
    return bool(row)


def _parse_linkedin(url: str | None) -> str | None:
    if not url:
        return None
    m = LINKEDIN_RE.search(url)
    if not m:
        return None
    link = m.group(0)
    if not link.startswith("http"):
        link = "https://" + link
    return link


def _extract_contacts_from_hits(
    company: str,
    hits: list[dict],
    config: dict,
    *,
    titles: list[str],
) -> list[dict]:
    if not hits:
        return []

    defaults = task_defaults("extract")
    prompts = config.get("prompts") or {}
    title_list = ", ".join(titles) or "privacy / data governance leaders"
    if (config.get("name") or "").strip().lower() == "keira":
        title_list = ", ".join(titles) or "Owner / Founder / President / CEO"

    snippets = []
    for i, h in enumerate(hits[:18], 1):
        body = h.get("page_text") or h.get("snippet") or ""
        snippets.append(
            f"{i}. [{h.get('query', '')}]\n"
            f"   Title: {h.get('title', '')}\n"
            f"   URL: {h.get('url', '')}\n"
            f"   Snippet: {body[:480]}"
        )

    system = prompts.get("qualify_system") or "Extract B2B contacts from search snippets. Output strict JSON only."
    system = f"""{system}

Find up to 3 best contacts at {company} for {config['company']} outreach.
Target roles: {title_list}
Only use evidence from the snippets — do not invent people.
Every field must include src (URL) and snippet from the results."""

    prompt = f"""Company: {company}
Product context: {config['product']} — {config.get('tagline', '')}

SEARCH RESULTS:
{truncate_context(chr(10).join(snippets), defaults['context_chars'])}

Rules:
- Return up to 3 contacts ranked by ICP title fit at {company} ({title_list} first)
- contact_name must look like a real person (First Last), not a job posting
- linkedin_url only if a linkedin.com/in/ URL appears in the results
- Each of contact_name, contact_title, linkedin_url MUST be an evidence object: {{"value","src","snippet"}}
- If no credible person found, return an empty contacts array

JSON only:
{{
  "contacts": [
    {{
      "contact_name": {{"value": "First Last", "src": "https://...", "snippet": "..."}},
      "contact_title": {{"value": "title", "src": "https://...", "snippet": "..."}},
      "linkedin_url": {{"value": "https://linkedin.com/in/...", "src": "https://...", "snippet": "..."}} | null,
      "signal": "why this person fits — cite the snippet"
    }}
  ]
}}"""

    try:
        result = chat_json(
            prompt,
            system=system,
            max_tokens=1024,
            temperature=0.1,
        )
    except LLMError as e:
        logger.warning("Web contact extraction failed for %s: %s", company, e)
        return []

    from .evidence import filter_evidence_or_drop, flatten_for_record

    contacts = result.get("contacts") or []
    cleaned = []
    for c in contacts:
        # Support legacy plain-string responses
        if isinstance(c.get("contact_name"), str):
            name = (c.get("contact_name") or "").strip()
            if not name or len(name.split()) < 2:
                continue
            if name.lower() in ("linkedin", "indeed", "glassdoor"):
                continue
            # Attach best-effort evidence from matching hit
            src = ""
            snip = c.get("signal") or ""
            for h in hits:
                blob = f"{h.get('title','')} {h.get('snippet','')} {h.get('page_text','')}"
                if name.split()[0].lower() in blob.lower() and name.split()[-1].lower() in blob.lower():
                    src = h.get("url") or ""
                    snip = (h.get("snippet") or snip)[:200]
                    break
            if not src:
                continue  # evidence-or-drop
            cleaned.append({
                "contact_name": name,
                "contact_title": (c.get("contact_title") or c.get("title") or "").strip() or None,
                "linkedin_url": _parse_linkedin(c.get("linkedin_url") or c.get("url")),
                "signal": (c.get("signal") or "Web research match").strip(),
                "_evidence": {
                    "contact_name": {"value": name, "src": src, "snippet": snip or name},
                },
            })
            continue

        evidenced = filter_evidence_or_drop(
            c, ["contact_name", "contact_title", "linkedin_url"]
        )
        flat = flatten_for_record(evidenced)
        name = (flat.get("contact_name") or "").strip()
        if not name or len(name.split()) < 2:
            continue
        if name.lower() in ("linkedin", "indeed", "glassdoor"):
            continue
        cleaned.append({
            "contact_name": name,
            "contact_title": (flat.get("contact_title") or "").strip() or None,
            "linkedin_url": _parse_linkedin(flat.get("linkedin_url")),
            "signal": (c.get("signal") or "Web research match").strip(),
            "_evidence": evidenced,
        })
    return cleaned[:3]


def _maybe_resolve_email(contact: dict, company: str, domain: str | None) -> tuple[str | None, dict]:
    """Pattern inference first; optional Hunter per-person if explicitly enabled."""
    extra: dict = {}
    if contact.get("contact_name"):
        inferred = infer_email_for_contact(contact["contact_name"], company, domain=domain)
        if inferred:
            extra.update(inferred)
            return inferred["email"], extra

    if os.getenv("WOODWAY_HUNTER_EMAIL", "").lower() not in ("1", "true", "yes"):
        return None, extra
    from .hunter_api import find_email, hunter_available

    if not hunter_available():
        return None, extra
    parts = contact["contact_name"].split()
    if len(parts) < 2:
        return None, extra
    found = find_email(parts[0], parts[-1], domain=domain, company=company)
    if not found:
        return None, extra
    extra.update({
        "email": found["email"],
        "email_verified": 1 if found.get("verified") else 0,
        "email_source": "hunter_finder",
        "contact_tier": "A",
    })
    return found["email"], extra


def search_and_import_web_contacts(
    agent: str,
    company_names: list[str],
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Web-search contacts at specific companies — free except LLM + optional Hunter."""
    emit = on_progress or (lambda msg: None)
    names = [n.strip() for n in company_names if n and n.strip()]
    if not names:
        return {
            "provider": "web_research",
            "imported": 0,
            "updated": 0,
            "skipped": True,
            "with_email": 0,
            "with_linkedin": 0,
            "errors": [],
        }

    config = _agent_config(agent)
    titles = _target_titles(config)
    delay = float(os.getenv("WEB_CONTACT_DELAY", "0.2"))
    # Yield > frugality: a $3 run that finds contacts beats a $0.07 run that doesn't
    queries_per_co = int(os.getenv("WEB_CONTACT_QUERIES", "10"))
    results_per_query = int(os.getenv("WEB_CONTACT_RESULTS", "12"))
    fetch_pages = os.getenv("WEB_CONTACT_FETCH_PAGES", "true").lower() not in ("0", "false", "no")
    page_fetch_limit = int(os.getenv("WEB_CONTACT_PAGE_FETCH", "5"))

    emit(f"Web contact search at {len(names)} companies (DuckDuckGo + Claude)…")
    init_db()

    imported = updated = no_contact = with_email = with_linkedin = 0
    errors: list[str] = []
    searched = 0

    for i, company in enumerate(names, 1):
        if imported + updated >= limit:
            break
        if _should_skip(company, config):
            no_contact += 1
            continue

        emit(f"  [{i}/{len(names)}] {company}…")
        hits: list[dict] = []
        seen_urls: set[str] = set()
        for q in _company_contact_queries(company, titles, agent=agent)[:queries_per_co]:
            for hit in search_web(q, max_results=results_per_query):
                url = hit.get("url") or ""
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                hit["query"] = q
                hits.append(hit)
            time.sleep(0.1)

        if fetch_pages:
            fetched = 0
            for hit in hits:
                if fetched >= page_fetch_limit:
                    break
                page_text = _fetch_page_snippet(hit.get("url") or "")
                if page_text:
                    hit["page_text"] = page_text
                    fetched += 1

        searched += 1

        contacts = _extract_contacts_from_hits(company, hits, config, titles=titles)
        if not contacts:
            mark_company_awaiting_contact(agent, company)
            no_contact += 1
            continue

        enrichment = {}
        from .email_patterns import resolve_company_domain

        domain = resolve_company_domain(company)

        for contact in contacts[:3]:
            if skip_existing and _contact_seen(contact["contact_name"], company, agent):
                no_contact += 1
                continue

            email, email_meta = _maybe_resolve_email(contact, company, domain)
            record = {
                "company": company,
                "contact_name": contact["contact_name"],
                "contact_title": contact["contact_title"],
                "email": email,
                "linkedin_url": contact.get("linkedin_url"),
                "industry": None,
                "signal": f"Web research — {contact['signal']}",
                "source": "web_research",
                "status": "imported",
                "company_domain": domain,
                **email_meta,
            }
            record = enrich_lead_contact_fields(record, company)
            record["prospect"] = build_prospect_string(record)
            lead_id, created = upsert_lead(record, agent=agent)
            if created:
                imported += 1
            else:
                updated += 1
            if record.get("email"):
                with_email += 1
            if contact.get("linkedin_url"):
                with_linkedin += 1
            # Persist provenance
            if lead_id and contact.get("_evidence"):
                try:
                    from .evidence import persist_entity_evidence
                    persist_entity_evidence(
                        "lead", contact["_evidence"],
                        entity_id=lead_id, company=company,
                    )
                except Exception:
                    pass

        if delay:
            time.sleep(delay)

    emit(
        f"Web research done — {imported} imported, {updated} updated, "
        f"{with_linkedin} with LinkedIn, {with_email} with email"
    )
    return {
        "provider": "web_research",
        "searched": searched,
        "credits_used": 0,
        "imported": imported,
        "updated": updated,
        "no_contact": no_contact,
        "skipped": no_contact,
        "with_email": with_email,
        "with_linkedin": with_linkedin,
        "errors": errors,
        "companies": len(names),
    }
