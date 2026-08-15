"""ICP qualification — keyword scoring (fallback) or LLM (real mode)."""

import re

KEYWORD_MAP = {
    "pharma": ["pharma", "pharmaceutical", "biotech", "life sciences"],
    "banking": ["bank", "banking", "credit union"],
    "financial services": ["financial", "fintech", "investment", "asset management", "wealth management"],
    "insurance": ["insurance", "insurer", "underwriting"],
    "healthcare": ["healthcare", "hospital", "health system", "medical"],
    "data aggregators": ["data aggregator", "data broker", "data marketplace"],
    "technology": ["tech", "technology", "software", "saas"],
    "federal government": ["federal", "government of canada", "gc ", "public service canada"],
    "provincial government": ["provincial", "ministry", "state government", "government of ontario"],
    "crown corporations": ["crown corp", "crown corporation", "crown agency"],
    "data centre providers": ["data centre", "data center", "colocation", "colo", "datacenter"],
    "large enterprise": ["enterprise", "fortune", "global 500", "multinational"],
}

GEO_KEYWORDS = {
    "ottawa": ["ottawa"],
    "eastern ontario": ["eastern ontario", "eastern on"],
    "kingston": ["kingston"],
    "cornwall": ["cornwall"],
    "canada": ["canada", "canadian", "ontario", "quebec", "british columbia"],
}

DEFAULT_QUALIFY_SYSTEM = "You qualify B2B sales leads. Output strict JSON only."
DISQUALIFIER_PENALTY = 50
BLOCKLIST_SCORE_CAP = 25


def qualify_prospect(
    config: dict,
    prospect: str,
    *,
    use_llm: bool = True,
    research_context: str | None = None,
    employee_count: int | None = None,
    company: str | None = None,
    fallback: bool = True,
    two_pass: bool | None = None,
) -> dict:
    from .llm import LLMError
    import os

    if two_pass is None:
        two_pass = os.getenv("QUALIFY_TWO_PASS", "true").lower() not in ("0", "false", "no")

    if use_llm:
        try:
            if two_pass:
                gate = _qualify_pass1_gate(
                    config, prospect, research_context, employee_count, company=company
                )
                if not gate.get("pass"):
                    result = {
                        "score": min(gate.get("score", 20), 30),
                        "tier": "cold",
                        "reasons": gate.get("reasons") or ["Failed pass-1 gate"],
                        "talking_points": [],
                        "recommendation": "Skip — failed extraction gate",
                        "mode": "pass1_gate",
                    }
                    return _apply_disqualifiers(result, config, prospect, company)
            result = _qualify_with_llm(
                config, prospect, research_context, employee_count, company=company
            )
            return _apply_disqualifiers(result, config, prospect, company)
        except LLMError as e:
            if not fallback:
                raise
            result = _qualify_keywords(config, prospect, company=company)
            result["reasons"] = list(result.get("reasons") or [])
            result["reasons"].append(f"LLM unavailable — keyword fallback ({e})")
            result["mode"] = "fallback"
            return _apply_disqualifiers(result, config, prospect, company)
    result = _qualify_keywords(config, prospect, company=company)
    return _apply_disqualifiers(result, config, prospect, company)


def _qualify_pass1_gate(
    config: dict,
    prospect: str,
    research_context: str | None,
    employee_count: int | None,
    *,
    company: str | None = None,
) -> dict:
    """Pass 1 (Haiku): structured extraction + hard gates."""
    from .llm import chat_json
    from .llm_optimize import task_defaults, truncate_context

    defaults = task_defaults("qualify_pass1")
    icp = config["icp"]
    prompt = f"""Extract structured facts and apply hard gates for ICP fit.

PROSPECT: {prospect}
COMPANY: {company or 'unknown'}
EMPLOYEES: {employee_count or 'unknown'}

RESEARCH:
{truncate_context(research_context, defaults['context_chars']) or 'None'}

ICP industries: {_industries_text(icp)}
Min employees: {icp.get('min_employees', 1000)}
Preferred size band: {icp.get('prefer_employees_min', 1500)}–{icp.get('prefer_employees_max', 10000)} (megabrands OK)

JSON only:
{{
  "pass": true/false,
  "score": 0-100,
  "industry": "string or null",
  "estimated_company_size": "string or null",
  "reasons": ["why pass or fail"]
}}"""

    try:
        result = chat_json(
            prompt,
            system="You extract B2B lead facts and apply hard ICP gates. JSON only.",
            max_tokens=defaults["max_tokens"],
            temperature=defaults["temperature"],
            task="qualify_pass1",
        )
        return {
            "pass": bool(result.get("pass")),
            "score": int(result.get("score") or 0),
            "reasons": result.get("reasons") or [],
        }
    except Exception:
        return {"pass": True, "score": 50, "reasons": ["Pass-1 skipped — LLM error"]}


def _industries_text(icp: dict) -> str:
    industries = icp.get("industries", [])
    if isinstance(industries, str):
        return industries
    return ", ".join(industries)


def _size_text(icp: dict) -> str:
    size = icp.get("company_size", "large")
    if isinstance(size, dict):
        lo = size.get("valuation_min_m")
        hi = size.get("valuation_max_m")
        if lo and hi:
            return f"business valued ${lo}M–${hi}M"
        return str(size)
    return f"{size} — larger companies score higher"


def _geo_text(icp: dict) -> str:
    geo = icp.get("geography")
    if not geo:
        return ""
    if isinstance(geo, dict):
        parts = []
        if geo.get("primary"):
            parts.append(str(geo["primary"]))
        if geo.get("secondary"):
            parts.append(str(geo["secondary"]))
        return ", ".join(parts)
    if isinstance(geo, list):
        return ", ".join(str(g) for g in geo)
    return str(geo)


def _match_text(prospect: str, company: str | None = None) -> str:
    parts = [prospect or ""]
    if company:
        parts.append(company)
    return " ".join(parts).lower()


def _blocklist_hits(text: str, config: dict) -> list[str]:
    from .discover import _name_matches_term

    hits = []
    for term in config.get("blocklist") or []:
        t = str(term).strip().lower()
        if t and _name_matches_term(t, text):
            hits.append(t)
    return hits


def _disqualifier_hits(text: str, config: dict) -> list[str]:
    hits = list(_blocklist_hits(text, config))
    for dq in config.get("disqualifiers") or []:
        dq_lower = str(dq).lower()
        # Pull parenthetical examples: "Service providers (Rogers, Bell, TELUS)"
        for m in re.findall(r"\(([^)]+)\)", dq_lower):
            for part in re.split(r"[,/]", m):
                token = part.strip()
                if len(token) >= 4 and token in text:
                    hits.append(token)
    return sorted(set(hits))


def _apply_disqualifiers(
    result: dict,
    config: dict,
    prospect: str,
    company: str | None = None,
) -> dict:
    text = _match_text(prospect, company)
    hits = _disqualifier_hits(text, config)
    if not hits:
        return result

    out = dict(result)
    reasons = list(out.get("reasons") or [])
    blocklist = _blocklist_hits(text, config)

    if blocklist:
        out["score"] = min(out.get("score", 0), BLOCKLIST_SCORE_CAP)
        reasons.append(f"Blocklist match — not a target account: {', '.join(blocklist)}")
    else:
        out["score"] = max(0, out.get("score", 0) - DISQUALIFIER_PENALTY)
        reasons.append(f"Disqualifier signal: {', '.join(hits[:3])}")

    out["reasons"] = reasons
    out["tier"] = (
        "hot" if out["score"] >= 75 else "warm" if out["score"] >= 50 else "cold"
    )
    if blocklist or out["score"] < 50:
        out["recommendation"] = "Skip or deprioritize — matches a disqualifier."
    return out


def _qualify_with_llm(
    config: dict,
    prospect: str,
    research_context: str | None,
    employee_count: int | None = None,
    company: str | None = None,
) -> dict:
    from .llm import chat_json, resolve_provider
    from .llm_optimize import task_defaults, truncate_context
    from .retrieval import build_cached_working_memory, build_context

    prompts = config.get("prompts") or {}
    defaults = task_defaults("qualify")
    size_note = ""
    if employee_count:
        size_note = f"\nKNOWN EMPLOYEE COUNT: {employee_count:,}"

    company_line = f"\nCOMPANY: {company}" if company else ""

    # Cached working memory (ICP + exclusions + few-shots) — stable across leads
    base = prompts.get("qualify_system") or DEFAULT_QUALIFY_SYSTEM
    system = f"{base}\n\n{build_cached_working_memory(config, task='qualify')}"

    research_trimmed = truncate_context(research_context, defaults["context_chars"])
    db_state = ""
    if company:
        try:
            db_state = build_context(company, agent=config.get("name") or "woodway")
        except Exception:
            db_state = ""

    prompt = f"""Score this prospect 0-100 against the ICP. Be realistic and use the FULL score range.
Most prospects should land 40-70. Reserve 80+ for exceptional fit with clear industry + title match.
Apply disqualifiers and blocklist strictly — score ≤25 for blocklist matches.
{size_note}
{company_line}

PROSPECT: {prospect}

{db_state}

WEB RESEARCH:
{research_trimmed or 'None available'}

Respond with ONLY valid JSON (no markdown):
{{
  "score": <integer 0-100>,
  "tier": "<hot|warm|cold>",
  "industries": ["matched industries or empty list"],
  "title": "<matched title or null>",
  "reasons": ["reason 1", "reason 2"],
  "recommendation": "<one sentence action recommendation>",
  "talking_points": ["2-3 specific angles for outreach based on research"]
}}"""

    result = chat_json(
        prompt,
        system=system,
        max_tokens=defaults["max_tokens"],
        temperature=defaults["temperature"],
        task="qualify",
    )

    score = int(result.get("score", 0))
    tier = result.get("tier") or ("hot" if score >= 75 else "warm" if score >= 50 else "cold")

    out = {
        "score": score,
        "tier": tier,
        "industries": result.get("industries") or [],
        "title": result.get("title"),
        "reasons": result.get("reasons") or [],
        "recommendation": result.get("recommendation", ""),
        "talking_points": result.get("talking_points") or [],
        "confidence": result.get("confidence"),
        "estimated_company_size": result.get("estimated_company_size"),
        "ownership_confidence": result.get("ownership_confidence"),
        "succession_signals": result.get("succession_signals"),
        "mode": resolve_provider()["provider"],
    }
    calibrated = _calibrate_score(out, config, prospect, company)
    # Closed-loop learning from past replies
    try:
        from .outcomes import apply_learning_boost

        industries = calibrated.get("industries") or []
        ind = industries[0] if industries else None
        new_score, learn_reasons = apply_learning_boost(
            calibrated["score"],
            ind,
            calibrated.get("title") or prospect,
            agent=config.get("name") or "woodway",
        )
        if learn_reasons:
            calibrated["score"] = new_score
            calibrated["reasons"] = list(calibrated.get("reasons") or []) + learn_reasons
            calibrated["tier"] = (
                "hot" if new_score >= 75 else "warm" if new_score >= 50 else "cold"
            )
    except Exception:
        pass
    return calibrated


def _calibrate_score(
    result: dict,
    config: dict,
    prospect: str,
    company: str | None = None,
) -> dict:
    """Post-LLM calibration — spread scores, enforce industry fit for HOT."""
    from .negative_list import is_excluded

    out = dict(result)
    score = int(out.get("score") or 0)
    text = _match_text(prospect, company)
    reasons = list(out.get("reasons") or [])

    excluded = is_excluded(company or prospect.split(" at ")[-1], agent=config.get("name", "woodway"), extra_text=text)
    if excluded:
        score = min(score, BLOCKLIST_SCORE_CAP)
        reasons.append(f"Negative list: {excluded}")

    industries = [str(i).lower() for i in (out.get("industries") or [])]
    icp_inds = config.get("icp", {}).get("industries") or []
    if isinstance(icp_inds, str):
        icp_inds = [icp_inds]
    # Soft industries that inflate scores without proving buyer fit
    weak_only = {"technology", "tech", "software", "saas", "consulting"}
    strong_inds = [i for i in industries if i not in weak_only]

    has_industry = bool(strong_inds)
    if not has_industry and icp_inds:
        for ind in icp_inds:
            if ind.lower() in weak_only:
                continue
            keywords = KEYWORD_MAP.get(ind, [ind.lower()])
            if any(kw in text for kw in keywords):
                has_industry = True
                break

    if not has_industry:
        score = min(score, 55)
        reasons.append("No clear ICP industry match — capped score")

    # Need industry + title for HOT (75+)
    if score >= 75 and not (has_industry and out.get("title")):
        score = min(score, 68)
        reasons.append("Score capped — need industry + title match for HOT")

    if score >= 80 and not (has_industry and out.get("title")):
        score = min(score, 68)

    out["score"] = max(0, min(100, score))
    out["reasons"] = reasons
    out["tier"] = (
        "hot" if out["score"] >= 75 else "warm" if out["score"] >= 50 else "cold"
    )
    return out


def _qualify_keywords(config: dict, prospect: str, company: str | None = None) -> dict:
    icp = config["icp"]
    text = _match_text(prospect, company)
    industry_score, industries = _match_industry(text, icp.get("industries", []))
    title_score, title = _match_title(text, icp.get("titles", []))
    size_score, size = _match_size(text, icp.get("company_size"))
    geo_score, geo = _match_geography(text, icp.get("geography"))

    score = min(100, industry_score + title_score + size_score + geo_score)
    tier = "hot" if score >= 75 else "warm" if score >= 50 else "cold"

    reasons = []
    if industries:
        reasons.append(f"Industry match: {', '.join(industries)}")
    if title:
        reasons.append(f"Title match: {title}")
    if size != "unknown":
        reasons.append(f"Company size signal: {size}")
    if geo:
        reasons.append(f"Geography match: {geo}")
    if not reasons:
        reasons.append("No strong ICP signals detected in prospect description")

    return {
        "score": score,
        "tier": tier,
        "industries": industries,
        "title": title,
        "reasons": reasons,
        "recommendation": _recommendation(tier, config),
        "talking_points": [],
        "mode": "keywords",
    }


def _match_industry(text: str, industries) -> tuple[int, list[str]]:
    if isinstance(industries, str):
        return 20, []
    matched = []
    for industry in industries:
        keywords = KEYWORD_MAP.get(industry, [industry.lower()])
        if any(kw in text for kw in keywords):
            matched.append(industry)
    if not matched:
        return 0, []
    return min(40, len(matched) * 20), matched


def _match_title(prospect: str, titles: list[str]) -> tuple[int, str | None]:
    text = prospect.lower()
    for title in titles:
        if title.lower() in text:
            return 35, title
    for title in titles:
        parts = title.lower().split()
        if any(p in text for p in parts if len(p) > 3):
            return 20, title
    return 0, None


def _match_size(prospect: str, size_config) -> tuple[int, str]:
    text = prospect.lower()
    size_signals = ["large", "global", "top-", "fortune", "enterprise", "major"]
    if any(s in text for s in size_signals):
        return 25, "large"
    if isinstance(size_config, dict):
        return 15, "mid-market (valuation-based ICP)"
    return 10, "unknown"


def _match_geography(text: str, geography) -> tuple[int, str | None]:
    if not geography:
        return 0, None
    targets: list[str] = []
    if isinstance(geography, dict):
        if geography.get("primary"):
            targets.append(str(geography["primary"]))
        if geography.get("secondary"):
            targets.append(str(geography["secondary"]))
    elif isinstance(geography, list):
        targets = [str(g) for g in geography]
    else:
        targets = [str(geography)]

    for target in targets:
        keywords = GEO_KEYWORDS.get(target.lower(), [target.lower()])
        if any(kw in text for kw in keywords):
            return 20, target
    return 0, None


def _recommendation(tier: str, config: dict) -> str:
    if tier == "hot":
        return f"Strong fit for {config['product']}. Prioritize outreach."
    if tier == "warm":
        return "Potential fit. Research further before reaching out."
    return "Likely not a fit based on available info. Skip or deprioritize."
