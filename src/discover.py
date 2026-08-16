"""Autonomous prospect discovery — search the web for ICP-fit companies."""

import re

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


def build_search_queries(config: dict, max_queries: int = 8, *, agent: str | None = None) -> list[str]:
    """
    Rotate through a large query pool via DB cursor so nightly runs don't
    hit the same 8 DuckDuckGo SERPs forever.
    """
    from .db import get_cursor, set_cursor

    discover_cfg = config.get("discover") or {}
    agent = agent or config.get("name") or "woodway"

    pool: list[str] = []
    explicit = discover_cfg.get("queries") or []
    pool.extend(explicit)
    pool.extend(discover_cfg.get("query_pool") or [])

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

    for industry in industries:
        for signal in signals:
            pool.append(f"{industry} {signal}{geo_suffix}".strip())

    # Signal-seeded follow-ups from recent DB companies still awaiting contacts
    try:
        from .db import get_companies_needing_contacts

        for company in get_companies_needing_contacts(agent, limit=6):
            if (agent or "").strip().lower() == "keira":
                pool.append(
                    f'"{company}" (Owner OR Founder OR President OR CEO) '
                    f'(Ottawa OR Kingston OR "Eastern Ontario")'
                )
                pool.append(
                    f'"{company}" "family-owned" OR succession OR "looking to sell" OR retirement'
                )
            else:
                pool.append(f'"{company}" "chief privacy officer" OR "data governance" OR DPO')
                pool.append(f'"{company}" new "privacy officer" OR "head of data governance"')
    except Exception:
        pass

    # Dedupe pool preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for q in pool:
        q = (q or "").strip()
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        uniq.append(q)
    pool = uniq
    if not pool:
        return DEFAULT_SIGNAL_PHRASES[:max_queries]

    # Rank by learning weights (winning industries/titles first), then rotate
    try:
        from .outcomes import get_learning_weights
        from .query_learning import query_yield_bonus, rank_query_pool

        weights = get_learning_weights(agent)
        has_learning = (weights.get("positive_count") or 0) + (weights.get("negative_count") or 0) > 0
        if has_learning:
            ranked = rank_query_pool(pool, agent=agent)
            ranked = sorted(
                ranked,
                key=lambda q: (-(query_yield_bonus(agent, q)), ranked.index(q)),
            )
            pool = ranked
    except Exception:
        pass

    cursor = get_cursor(agent, "discover_query")
    n = len(pool)
    selected = [pool[(cursor + i) % n] for i in range(min(max_queries, n))]
    set_cursor(agent, "discover_query", (cursor + max_queries) % max(n, 1))
    return selected


def search_web(query: str, max_results: int = 5) -> list[dict]:
    from .search_provider import search_web as _search

    return _search(query, max_results=max_results)


def discover_raw_hits(
    config: dict,
    *,
    max_queries: int = 8,
    results_per_query: int = 5,
    agent: str | None = None,
) -> list[dict]:
    """Run ICP-driven searches and return deduplicated raw hits."""
    queries = build_search_queries(config, max_queries=max_queries, agent=agent)
    seen_urls: set[str] = set()
    hits: list[dict] = []

    for q in queries:
        for hit in search_web(q, max_results=results_per_query):
            url = hit.get("url", "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            hit = {**hit, "query": q}
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


# Sentence fragments / generics that look like Title Case in SERP snippets
JUNK_COMPANY_TOKENS = frozenset({
    "the", "a", "an", "and", "or", "but", "with", "what", "use", "select", "chief",
    "health", "healthcare", "california", "hipaa", "data", "job", "how", "face",
    "link", "july", "evaluate", "for", "from", "this", "that", "when", "where",
    "who", "why", "are", "is", "was", "were", "been", "new", "our", "your",
    "their", "its", "his", "her", "all", "any", "some", "more", "most", "other",
    "into", "over", "after", "before", "about", "between", "through", "during",
    "inc", "llc", "ltd", "corp", "corporation", "company", "co", "group",
    "privacy", "governance", "officer", "director", "vice", "president",
    "techtarget", "springer", "wikipedia", "linkedin", "indeed", "glassdoor",
})


def looks_like_company_name(name: str) -> bool:
    """Reject SERP fragment junk ("What", "But", "SSM Health Katie Germano")."""
    if not name or not str(name).strip():
        return False
    text = str(name).strip()
    if len(text) < 2 or len(text) > 60:
        return False
    if "..." in text or text.endswith("..") or re.search(r"\.\s+\w+$", text):
        return False
    # Sentence openers / glued person names
    if re.search(r"\b(What|But|With|Use|Select|The|How|Why|When|Where)\b", text):
        if len(text.split()) <= 3:
            return False
    words = re.findall(r"[A-Za-z0-9]+", text)
    if not words:
        return False
    lower_words = [w.lower() for w in words]
    if all(w in JUNK_COMPANY_TOKENS for w in lower_words):
        return False
    if len(words) == 1 and lower_words[0] in JUNK_COMPANY_TOKENS:
        return False
    # "Mercy Health Joe Nguyen" — org + two person-name tokens
    if len(words) >= 4 and words[-2][0].isupper() and words[-1][0].isupper():
        if words[-2].isalpha() and words[-1].isalpha() and len(words[-1]) >= 3:
            # Likely "Company First Last"
            if not any(w.lower() in ("inc", "llc", "ltd", "corp", "plc", "ag", "sa") for w in lower_words):
                return False
    # Must contain at least one token that isn't a junk word (len>=3)
    if not any(w not in JUNK_COMPANY_TOKENS and len(w) >= 2 for w in lower_words):
        return False
    return True


def _should_skip(name: str, config: dict | None = None, *, extra_text: str = "") -> bool:
    lower = name.lower()
    if len(lower) < 3 or len(lower) > 50:
        return True
    if not looks_like_company_name(name):
        return True
    if any(_name_matches_term(s, lower) for s in SKIP_NAMES):
        return True
    if config:
        for term in config.get("blocklist") or []:
            if _name_matches_term(term, lower):
                return True
    from .negative_list import is_excluded

    agent = (config or {}).get("name") or "woodway"
    if is_excluded(name, agent=agent, extra_text=extra_text):
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


def _heuristic_companies_from_hits(hits: list[dict], config: dict, *, limit: int) -> list[dict]:
    """
    Conservative fallback — only keep names Clearbit resolves to a matching domain org.
    Disabled by default via WEB_DISCOVER_HEURISTIC=false because SERP title regex is noisy.
    """
    import os

    if os.getenv("WEB_DISCOVER_HEURISTIC", "false").lower() not in ("1", "true", "yes"):
        return []

    from .enrich import enrich_company
    from .negative_list import is_excluded

    name_re = re.compile(
        r"\b([A-Z][A-Za-z0-9&.'\-]+(?:\s+[A-Z][A-Za-z0-9&.'\-]+){0,3})\b"
        r"(?:'s|\s+(?:names|appoints|hires|announces|privacy|CPO|DPO))"
    )
    at_re = re.compile(r"\bat\s+([A-Z][A-Za-z0-9&.'\-]+(?:\s+[A-Z][A-Za-z0-9&.'\-]+){0,2})")
    found: list[dict] = []
    seen: set[str] = set()
    agent = (config or {}).get("name") or "woodway"
    for h in hits:
        text = f"{h.get('title') or ''} {h.get('snippet') or ''}"
        candidates = [m.group(1).strip().rstrip(".,;:") for m in name_re.finditer(text)]
        candidates += [m.group(1).strip().rstrip(".,;:") for m in at_re.finditer(text)]
        for name in candidates:
            if not looks_like_company_name(name):
                continue
            if _should_skip(name, config, extra_text=text):
                continue
            if is_excluded(name, agent=agent, extra_text=text):
                continue
            enrichment = enrich_company(name)
            resolved = (enrichment.get("name") or "").lower()
            domain = enrichment.get("domain")
            # Require Clearbit to agree this is a real org closely matching the candidate
            if not domain or not resolved:
                continue
            cand = name.lower()
            if cand not in resolved and resolved not in cand and cand.split()[0] not in resolved:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append({
                "company": enrichment.get("name") or name,
                "description": (h.get("snippet") or "")[:200],
                "signal": f"Mentioned in search: {(h.get('title') or '')[:120]}",
                "industry": None,
                "company_domain": domain,
            })
            if len(found) >= limit:
                return found
    return found


def _normalize_extracted_lead(raw: dict, config: dict, *, agent: str, seen: set[str]) -> dict | None:
    """Clean one LLM/heuristic company row; None if skip/dupe."""
    from .negative_list import is_excluded

    if isinstance(raw.get("company"), dict):
        company = (raw["company"].get("value") or "").strip()
    else:
        company = (raw.get("company") or "").strip()
    if not company:
        return None
    company = company.rstrip(".,;:")
    if not looks_like_company_name(company):
        return None
    key = company.lower()
    if key in seen:
        return None
    extra = f"{raw.get('description') or ''} {raw.get('signal') or ''}"
    if _should_skip(company, config, extra_text=extra):
        return None
    if is_excluded(company, agent=agent, extra_text=extra):
        return None
    seen.add(key)
    industry = raw.get("industry")
    if isinstance(industry, dict):
        industry = industry.get("value")
    return {
        "company": company,
        "industry": (industry or "").strip() or None,
        "description": (
            raw["description"].get("value") if isinstance(raw.get("description"), dict)
            else raw.get("description")
        ) or None,
        "signal": (
            raw["signal"].get("value") if isinstance(raw.get("signal"), dict)
            else raw.get("signal")
        ) or None,
        "employee_count": raw.get("employee_count"),
    }


def _extract_companies_for_pipeline(
    hits: list[dict],
    config: dict,
    *,
    limit: int,
    taken_names: list[str] | None = None,
) -> list[dict]:
    """Extract company-only leads from search hits via primary LLM (fast vs Actava agent).

    Batches all hits (40 snippets per LLM call) and merges — does not inject qualify_extra
    scoring notes (those belong in the qualify step, not discovery volume).
    """
    from .llm import chat_json
    from .llm_optimize import task_defaults, truncate_context

    if not hits:
        return []

    defaults = task_defaults("extract")
    icp = config.get("icp") or {}
    prompts = config.get("prompts") or {}
    agent = (config or {}).get("name") or "woodway"
    neg_names = ", ".join(sorted((config.get("negative_list") or {}).get("names") or [])[:12])

    from .retrieval import known_funnel_for_discovery

    # Hint funnel memory from a sample of hits (first batch)
    sample_snips = "\n".join(
        f"{h.get('title') or ''} {h.get('snippet') or ''}"[:200] for h in hits[:40]
    )
    already = known_funnel_for_discovery(agent, limit=15, hint_text=sample_snips)
    taken = [n.strip() for n in (taken_names or []) if n and n.strip()]
    taken_block = ""
    if taken:
        shown = ", ".join(taken[:40])
        taken_block = (
            f"\nTAKEN THIS PASS (already in DB — do NOT return these; find OTHER companies):\n{shown}\n"
        )
    already_block = f"\n{already}\n{taken_block}" if (already or taken_block) else ""

    target = max(limit, 50, limit * 2)
    batch_size = 40
    system = prompts.get("qualify_system") or "Extract B2B companies. Output strict JSON only."
    system = f"""{system}

Extract target companies for {config['company']} ({config['product']}).
Focus on company fit — contact people are found in a later step.
Never return vendors on the negative list ({neg_names or 'privacy-tech vendors'}).
Volume matters: return as many distinct ICP-fit companies as the results support."""

    cleaned: list[dict] = []
    seen: set[str] = set()

    for batch_start in range(0, len(hits), batch_size):
        if len(cleaned) >= target:
            break
        batch = hits[batch_start : batch_start + batch_size]
        snippets = []
        for i, h in enumerate(batch, 1):
            snippets.append(
                f"{i}. [{h['query']}]\n   Title: {h['title']}\n   Snippet: {h['snippet'][:280]}"
            )
        remaining = target - len(cleaned)
        prompt = f"""Extract up to {remaining} distinct target COMPANIES from these search results for {config['company']} — {config['product']}.
Tagline: {config['tagline']}

IDEAL CUSTOMER PROFILE:
- Industries: {_industries_text(icp)}
- Target titles (for later): {', '.join(icp.get('titles', []))}
- Company size: {_size_text(icp)}
- Geography: {_geo_text(icp) or 'global'}
- Blocklist / vendors to EXCLUDE: {neg_names or 'none'}
{already_block}
SEARCH RESULTS:
{truncate_context(chr(10).join(snippets), max(defaults['context_chars'], 9000))}

Rules:
- Only companies mentioned in the results — do NOT invent names
- Prefer pharma, banks, insurers, healthcare, data aggregators
- EXCLUDE privacy-tech vendors, health-IT vendors, consultancies, e-commerce
- Prefer NEW companies not already in the database (see FUNNEL MEMORY)
- Return as many good new fits as possible up to {remaining} — volume matters
- Company name alone is enough; other fields optional

JSON only:
{{
  "leads": [
    {{
      "company": "Exact Company Name",
      "industry": "pharma|banking|insurance|healthcare|null",
      "description": "one short clause",
      "signal": "why it appeared in results"
    }}
  ]
}}"""

        try:
            result = chat_json(
                prompt,
                system=system,
                max_tokens=max(defaults["max_tokens"], 2048),
                temperature=defaults["temperature"],
                task="extract",
            )
        except Exception:
            result = {"leads": []}

        for raw in result.get("leads") or []:
            row = _normalize_extracted_lead(raw, config, agent=agent, seen=seen)
            if row:
                cleaned.append(row)
            if len(cleaned) >= target:
                break

        # Avoid re-suggesting names we already extracted in later batches
        if cleaned:
            more_taken = ", ".join(r["company"] for r in cleaned[:40])
            already_block = (
                f"\n{already}\n{taken_block}\n"
                f"ALREADY EXTRACTED THIS RUN (do NOT repeat):\n{more_taken}\n"
            )

    # If Claude under-delivered, pad with conservative heuristic
    if len(cleaned) < max(4, limit):
        for row in _heuristic_companies_from_hits(hits, config, limit=target):
            normalized = _normalize_extracted_lead(row, config, agent=agent, seen=seen)
            if not normalized:
                continue
            cleaned.append(normalized)
            if len(cleaned) >= target:
                break

    return cleaned[:target]


def _company_seen(company: str, agent: str, *, domain: str | None = None) -> bool:
    from .db import company_seen

    return company_seen(company, agent, domain=domain)


def _coerce_int(val):
    if val is None:
        return None
    if isinstance(val, dict):
        val = val.get("value")
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def search_and_import_companies(
    agent: str,
    *,
    limit: int = 25,
    skip_existing: bool = True,
    on_progress=None,
) -> dict:
    """Fast company discovery: DuckDuckGo web search + primary LLM (Anthropic). No Actava agent."""
    from pathlib import Path

    import yaml

    from .db import build_prospect_string, init_db, upsert_lead
    from .email_patterns import resolve_company_domain

    emit = on_progress or (lambda msg: None)
    agents_dir = Path(__file__).parent.parent / "agents"
    with open(agents_dir / agent / "config.yaml") as f:
        config = yaml.safe_load(f)

    init_db()
    actava_cfg = config.get("actava") or {}
    discover_cfg = config.get("discover") or {}
    max_queries = discover_cfg.get("max_queries") or actava_cfg.get("max_queries") or 10
    results_per_query = discover_cfg.get("results_per_query") or actava_cfg.get("results_per_query") or 8

    emit(f"Web search ({max_queries} queries) + Claude company extraction…")
    hits = discover_raw_hits(
        config, max_queries=max_queries, results_per_query=results_per_query, agent=agent,
    )
    emit(f"{len(hits)} search hits collected")

    if not hits:
        return {
            "provider": "anthropic",
            "mode": "web+llm",
            "searched": 0,
            "companies": [],
            "imported": 0,
            "updated": 0,
            "skipped": 0,
            "with_email": 0,
            "errors": [],
        }

    raw_leads = _extract_companies_for_pipeline(hits, config, limit=limit)
    emit(f"Claude extracted {len(raw_leads)} companies")

    # Deterministic domain dedupe against DB — never rely on the model to "avoid repeats"
    from .db import active_domains, normalize_company
    from .email_patterns import normalize_domain

    def _drop_known(leads: list[dict]) -> tuple[list[dict], list[str]]:
        seen_domains = active_domains(agent) if skip_existing else set()
        seen_names: set[str] = set()
        kept: list[dict] = []
        dropped: list[str] = []
        for raw in leads:
            company = (raw.get("company") or "").strip()
            if not company:
                continue
            key = normalize_company(company)
            domain = normalize_domain(raw.get("company_domain")) or resolve_company_domain(company)
            if key in seen_names or (domain and domain in seen_domains) or (
                skip_existing and _company_seen(company, agent, domain=domain)
            ):
                dropped.append(company)
                continue
            seen_names.add(key)
            if domain:
                seen_domains.add(domain)
                raw["company_domain"] = domain
            kept.append(raw)
        return kept, dropped

    deduped, dropped = _drop_known(raw_leads)
    if dropped:
        emit(f"Domain dedupe — dropped {len(dropped)} already-known companies ({len(deduped)} new)")

    # Same-run refill: extra search + extract for NEW names only (do not restart the pipeline)
    need = max(6, min(limit, 12))
    if skip_existing and len(deduped) < need:
        emit(
            f"Thin new yield ({len(deduped)}/{need}) — extra search + Claude pass for names not in DB…"
        )
        extra_hits = discover_raw_hits(
            config, max_queries=max_queries, results_per_query=results_per_query, agent=agent,
        )
        emit(f"Refill search — {len(extra_hits)} more hits")
        more = _extract_companies_for_pipeline(
            extra_hits or hits,
            config,
            limit=limit,
            taken_names=dropped,
        )
        emit(f"Claude refill extracted {len(more)} companies")
        merged = {normalize_company(r.get("company") or ""): r for r in deduped}
        for r in more:
            key = normalize_company(r.get("company") or "")
            if key:
                merged[key] = r
        deduped, dropped2 = _drop_known(list(merged.values()))
        if dropped2:
            emit(f"Refill dedupe — dropped {len(dropped2)} known, {len(deduped)} new remain")

    raw_leads = deduped
    domain_dupes = len(dropped)

    imported = updated = skipped = with_email = 0
    from .icp_gates import passes_icp_gates

    for raw in raw_leads:
        company = (raw.get("company") or "").strip()
        extra = f"{raw.get('description') or ''} {raw.get('signal') or ''}"
        if not company or _should_skip(company, config, extra_text=extra):
            skipped += 1
            continue
        ok, reason = passes_icp_gates(
            company,
            config,
            agent=agent,
            employee_count=_coerce_int(raw.get("employee_count")),
            extra_text=extra,
        )
        if not ok:
            skipped += 1
            continue
        # Domain dedupe already applied above; name-only seen check as backstop
        if skip_existing and _company_seen(company, agent, domain=resolve_company_domain(company)):
            skipped += 1
            continue

        domain = raw.get("company_domain") or resolve_company_domain(company)
        signal = (raw.get("signal") or raw.get("reason") or "Web discovery match").strip()
        description = (raw.get("description") or "").strip()
        if description and description.lower() not in signal.lower():
            signal = f"{signal} — {description}"

        record = {
            "company": company,
            "contact_name": None,
            "contact_title": None,
            "email": None,
            "linkedin_url": (raw.get("linkedin_url") or "").strip() or None,
            "employee_count": _coerce_int(raw.get("employee_count")),
            "industry": (raw.get("industry") or "").strip() or None,
            "company_domain": domain,
            "signal": f"Discover — {signal}",
            "source": "discover",
            "status": "imported",
            "prospect": build_prospect_string({"company": company}),
        }
        _, created = upsert_lead(record, agent=agent)
        if created:
            imported += 1
        else:
            updated += 1

    emit(f"Done — {imported} imported, {updated} updated, {skipped} skipped")
    from .negative_list import is_excluded

    companies_out = []
    for r in raw_leads:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        extra = f"{r.get('description') or ''} {r.get('signal') or ''}"
        if is_excluded(name, agent=agent, extra_text=extra) or _should_skip(name, config, extra_text=extra):
            continue
        companies_out.append({
            "company": name,
            "description": (r.get("description") or r.get("signal") or "").strip(),
            "industry": (r.get("industry") or "").strip() or None,
            "signal": (r.get("signal") or "").strip() or None,
            "employee_count": _coerce_int(r.get("employee_count")),
        })
    return {
        "provider": "anthropic",
        "mode": "web+llm",
        "searched": len(raw_leads),
        "companies": companies_out,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "with_email": with_email,
        "errors": [],
    }


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
