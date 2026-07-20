"""Autonomous prospect discovery — search the web for ICP-fit companies."""

import re
from ddgs import DDGS

from .qualify import _geo_text, _industries_text, _size_text

# Fallback signals if an agent config has none (Woodway-style)
DEFAULT_SIGNAL_PHRASES = [
    '"data governance" OR "chief privacy officer" OR "VP data governance"',
    "anonymized data OR de-identification OR re-identification risk",
    '"director of privacy" OR "head of data governance" hiring',
]

# Well-known companies to ignore (too generic / not actionable)
SKIP_NAMES = {
    "linkedin", "indeed", "glassdoor", "wikipedia", "reddit",
    "google", "microsoft", "amazon", "facebook", "meta",
    "woodway assurance", "evidata", "fonex", "keira capital",
    "dealstream", "sunbelt", "sunbelt network", "bizbuysell", "businessesforsale",
    "mnp", "bdc", "axial", "flippa",
}


def build_search_queries(config: dict, max_queries: int = 8) -> list[str]:
    discover_cfg = config.get("discover") or {}

    explicit = discover_cfg.get("queries")
    if explicit:
        return list(explicit)[:max_queries]

    signals = discover_cfg.get("search_signals") or DEFAULT_SIGNAL_PHRASES
    industries = config["icp"].get("industries", [])
    if isinstance(industries, str):
        industries = [""]

    geo = config["icp"].get("geography", "")
    geo_suffix = ""
    if geo:
        geo_text = _geo_text(config["icp"])
        if geo_text and "canada" in geo_text.lower():
            geo_suffix = " Canada"

    queries = []
    for industry in industries:
        for signal in signals:
            queries.append(f"{industry} {signal}{geo_suffix}".strip())
            if len(queries) >= max_queries:
                return queries
    return queries


def search_web(query: str, max_results: int = 5) -> list[dict]:
    results = []
    try:
        with DDGS() as ddgs:
            for hit in ddgs.text(query, max_results=max_results):
                results.append({
                    "query": query,
                    "title": hit.get("title", ""),
                    "snippet": hit.get("body", ""),
                    "url": hit.get("href", ""),
                })
    except Exception:
        pass
    return results


def discover_raw_hits(config: dict, *, max_queries: int = 8, results_per_query: int = 5) -> list[dict]:
    """Run ICP-driven searches and return deduplicated raw hits."""
    queries = build_search_queries(config, max_queries=max_queries)
    seen_urls: set[str] = set()
    hits: list[dict] = []

    for q in queries:
        for hit in search_web(q, max_results=results_per_query):
            url = hit.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            hits.append(hit)

    return hits


def extract_companies_from_hits(hits: list[dict], config: dict, *, use_llm: bool = True) -> list[dict]:
    """
    Turn search hits into structured prospect leads.
    Returns [{company, industry, signal, suggested_contact, source}].
    """
    if not hits:
        return []

    if use_llm:
        try:
            return _extract_with_llm(hits, config)
        except Exception:
            return _extract_with_heuristics(hits, config)
    return _extract_with_heuristics(hits, config)


def _default_title(config: dict) -> str:
    titles = config["icp"].get("titles") or []
    return titles[0] if titles else "Decision maker"


def _extract_with_llm(hits: list[dict], config: dict) -> list[dict]:
    from .llm import chat_json

    icp = config["icp"]
    prompts = config.get("prompts") or {}
    qualify_extra = (prompts.get("qualify_extra") or "").strip()
    extra_block = ""
    if qualify_extra:
        extra_block = f"""
CUSTOM INSTRUCTIONS (you MUST follow these when picking companies):
{qualify_extra}
"""

    snippets = []
    for i, h in enumerate(hits[:30], 1):
        snippets.append(
            f"{i}. [{h['query']}]\n   Title: {h['title']}\n   Snippet: {h['snippet'][:250]}"
        )
    block = "\n".join(snippets)

    geo_line = _geo_text(icp)
    prompt = f"""Extract REAL companies from these web search results that would be good prospects for {config['company']} — {config['product']}.
Tagline: {config['tagline']}

IDEAL CUSTOMER PROFILE:
- Industries: {_industries_text(icp)}
- Target titles: {', '.join(icp.get('titles', []))}
- Company size: {_size_text(icp)}
- Geography: {geo_line or 'not restricted'}
- Disqualifiers (never include these): {', '.join(config.get('disqualifiers', []))}
- Blocklist (never include): {', '.join(config.get('blocklist') or []) or 'none'}
{extra_block}
SEARCH RESULTS:
{block}

Rules:
- Only include actual companies/organizations (not job boards, news sites, or generic articles)
- Each lead needs a concrete reason tied to the search result
- Do NOT invent companies not mentioned in the results
- Skip blocklisted companies and disqualifiers entirely
- Skip duplicates
- Return at most 8 leads

Respond with ONLY this JSON shape (no extra keys, no trailing commas):
{{
  "leads": [
    {{
      "company": "Company Name",
      "industry": "matched industry",
      "signal": "why they are a prospect",
      "suggested_contact": "{_default_title(config)} at Company Name",
      "confidence": "high"
    }}
  ]
}}"""

    system = prompts.get("qualify_system") or "Extract B2B sales leads from search results. Output strict JSON only."
    result = chat_json(prompt, system=system)
    leads = result.get("leads") or []
    return _filter_leads(leads, config)


def _extract_with_heuristics(hits: list[dict], config: dict) -> list[dict]:
    """Fallback extraction without LLM — pattern match company names from titles."""
    leads = []
    seen: set[str] = set()

    patterns = [
        re.compile(r"at ([A-Z][A-Za-z0-9&\.\- ]{2,40})(?:\s*[|\-\–]|$)", re.I),
        re.compile(r"^([A-Z][A-Za-z0-9&\.\- ]{2,30})\s+(?:hiring|jobs|careers)", re.I),
        re.compile(r"([A-Z][A-Za-z0-9&\.\- ]{2,30})\s+-\s+(?:Data Governance|Privacy|Chief Privacy)", re.I),
    ]

    for h in hits:
        text = f"{h['title']} {h['snippet']}"
        for pat in patterns:
            m = pat.search(text)
            if not m:
                continue
            company = m.group(1).strip().rstrip(".")
            key = company.lower()
            if key in seen or _should_skip(company, config):
                continue
            seen.add(key)
            leads.append({
                "company": company,
                "industry": _guess_industry(text, config),
                "signal": h["snippet"][:120] or h["title"],
                "suggested_contact": f"{_default_title(config)} at {company}",
                "confidence": "low",
            })
            break

    return leads[:10]


def _filter_leads(leads: list[dict], config: dict) -> list[dict]:
    seen: set[str] = set()
    filtered = []
    for lead in leads:
        company = (lead.get("company") or "").strip()
        if not company or _should_skip(company, config):
            continue
        key = company.lower()
        if key in seen:
            continue
        seen.add(key)
        if not lead.get("suggested_contact"):
            lead["suggested_contact"] = f"{_default_title(config)} at {company}"
        filtered.append(lead)
    return filtered


def _name_matches_term(term: str, name: str) -> bool:
    """Match blocklist/skip terms without false positives (e.g. meta ≠ metal)."""
    t = str(term).strip().lower()
    if not t:
        return False
    if " " in t or len(t) >= 6:
        return t in name
    return bool(re.search(rf"\b{re.escape(t)}\b", name))


def _should_skip(name: str, config: dict | None = None) -> bool:
    lower = name.lower()
    if len(lower) < 3 or len(lower) > 50:
        return True
    if any(_name_matches_term(s, lower) for s in SKIP_NAMES):
        return True
    if config:
        for term in config.get("blocklist") or []:
            if _name_matches_term(term, lower):
                return True
    return False


def _guess_industry(text: str, config: dict) -> str:
    industries = config["icp"].get("industries", [])
    if isinstance(industries, str):
        return industries
    text = text.lower()
    for ind in industries:
        if ind.lower() in text:
            return ind
    return "unknown"


def discover_prospects(
    config: dict,
    *,
    limit: int = 5,
    use_llm: bool = True,
    max_queries: int = 8,
    agent_name: str = "woodway",
    save_to_db: bool = True,
    draft_outreach_for_top: bool = True,
    on_progress=None,
) -> dict:
    """
    Full discovery pipeline: search → extract → qualify → save.
    Skips companies already in leads.db.
    """
    from .db import company_seen, upsert_lead
    from .pipeline import process_lead
    from .outreach import generate_outreach

    emit = on_progress or (lambda msg: None)

    emit(f"Searching the web ({max_queries} queries)…")
    hits = discover_raw_hits(config, max_queries=max_queries)
    emit(f"{len(hits)} search hits collected")

    emit("Extracting companies" + (" with LLM…" if use_llm else "…"))
    leads = extract_companies_from_hits(hits, config, use_llm=use_llm)
    emit(f"{len(leads)} companies extracted")

    skipped_duplicates = 0
    qualified = []
    new_leads = [l for l in leads if not company_seen(l["company"], agent=agent_name)]
    skipped_duplicates = len(leads) - len(new_leads)
    if skipped_duplicates:
        emit(f"{skipped_duplicates} already in database — skipped")

    for i, lead in enumerate(new_leads, 1):
        emit(f"Researching & qualifying {lead['company']} ({i}/{len(new_leads)})…")

        lead_data = {
            "company": lead["company"],
            "industry": lead.get("industry"),
            "signal": lead.get("signal"),
            "prospect": lead.get("suggested_contact"),
            "source": "discover",
            "status": "discovered",
        }

        result = process_lead(
            config,
            lead_data,
            use_llm=use_llm,
            draft_outreach=False,
            agent_name=agent_name,
            save=save_to_db,
        )

        qualified.append({
            **lead,
            "prospect": result["prospect"],
            "research": result["research"],
            "research_context": result["research_context"],
            "qualification": result["qualification"],
            "lead_id": result.get("lead_id"),
        })

    qualified.sort(key=lambda x: x["qualification"]["score"], reverse=True)
    top = qualified[:limit]

    if draft_outreach_for_top and top and top[0]["qualification"]["score"] >= 50:
        best = top[0]
        emit(f"Drafting outreach for top prospect: {best['company']}…")
        outreach = generate_outreach(
            config,
            best["prospect"],
            best["qualification"],
            use_llm=use_llm,
            research_context=best.get("research_context"),
            contact={"company": best["company"]},
        )
        best["outreach"] = outreach
        if save_to_db:
            upsert_lead({
                "company": best["company"],
                "industry": best.get("industry"),
                "signal": best.get("signal"),
                "prospect": best["prospect"],
                "score": best["qualification"]["score"],
                "tier": best["qualification"]["tier"],
                "outreach_subject": outreach["subject"],
                "outreach_body": outreach["body"],
                "source": "discover",
                "status": "drafted",
            }, agent=agent_name)

    return {
        "hits": hits,
        "leads": leads,
        "qualified": top,
        "skipped_duplicates": skipped_duplicates,
    }
