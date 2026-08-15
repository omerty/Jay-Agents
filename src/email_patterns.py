"""Domain email pattern cache — free sources first, Hunter last resort."""

from __future__ import annotations

import logging
import os
import re

from .db import get_connection, init_db, _now
from .enrich import enrich_company
from .hunter_api import domain_search, hunter_available

logger = logging.getLogger(__name__)

PATTERN_PLACEHOLDERS = {
    "{first}": lambda f, l: f.lower(),
    "{last}": lambda f, l: l.lower(),
    "{f}": lambda f, l: f[0].lower() if f else "",
    "{l}": lambda f, l: l[0].lower() if l else "",
    "{first}.{last}": lambda f, l: f"{f}.{l}".lower(),
    "{f}{last}": lambda f, l: f"{f[0]}{l}".lower() if f else l.lower(),
    "{first}{l}": lambda f, l: f"{f}{l[0]}".lower() if l else f.lower(),
    "{first}_{last}": lambda f, l: f"{f}_{l}".lower(),
    "{last}.{first}": lambda f, l: f"{l}.{f}".lower(),
    "{f}.{last}": lambda f, l: f"{f[0]}.{l}".lower() if f else l.lower(),
    "{first}-{last}": lambda f, l: f"{f}-{l}".lower(),
}

# Try these in order when inferring from a known email + name
INFER_CANDIDATES = (
    "{first}.{last}",
    "{f}{last}",
    "{first}{l}",
    "{first}_{last}",
    "{last}.{first}",
    "{f}.{last}",
    "{first}-{last}",
)


def normalize_domain(domain: str | None) -> str | None:
    if not domain:
        return None
    d = domain.strip().lower()
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    d = d.removeprefix("www.")
    return d or None


# Clearbit occasionally returns sponsorship / event / wrong-company domains — pin the real ones
DOMAIN_OVERRIDES: dict[str, str] = {
    "bank of america": "bankofamerica.com",
    "bofa": "bankofamerica.com",
    "jp morgan": "jpmorganchase.com",
    "jpmorgan": "jpmorganchase.com",
    "jpmorgan chase": "jpmorganchase.com",
    "citigroup": "citi.com",
    "citi": "citi.com",
    "goldman sachs": "gs.com",
    "goldman": "gs.com",
    "morgan stanley": "morganstanley.com",
    "wells fargo": "wellsfargo.com",
    "unitedhealth": "unitedhealthgroup.com",
    "united health": "unitedhealthgroup.com",
    "moderna": "modernatx.com",
    "eli lilly": "lilly.com",
    "lilly": "lilly.com",
    "johnson & johnson": "jnj.com",
    "j&j": "jnj.com",
    "roche": "roche.com",
    "f. hoffmann-la roche": "roche.com",
    "hoffmann-la roche": "roche.com",
    "genentech": "gene.com",
    "pfizer": "pfizer.com",
    "novartis": "novartis.com",
    "merck": "merck.com",
    "sanofi": "sanofi.com",
    "regeneron": "regeneron.com",
    "astrazeneca": "astrazeneca.com",
    "bristol myers squibb": "bms.com",
    "bristol-myers squibb": "bms.com",
    "takeda": "takeda.com",
    "prudential": "prudential.com",
    "gsk": "gsk.com",
    "glaxosmithkline": "gsk.com",
    "glaxo smith kline": "gsk.com",
}


def _slug_token_hit(token: str, slug: str) -> bool:
    """Token/slug match without false positives like 'roche' ⊂ 'rochester'."""
    if not token or not slug:
        return False
    if token == slug:
        return True
    # Brand prefix of domain (moderna → modernatx) — allow short alphabetic tail only
    if len(token) >= 4 and slug.startswith(token):
        rem = slug[len(token):]
        if not rem:
            return True
        # Reject long unrelated remainder (roche + chester)
        if rem.isalpha() and len(rem) > 3:
            return False
        return True
    if len(slug) >= 4 and token.startswith(slug):
        return True
    return False


def resolve_company_domain(company: str) -> str | None:
    domain, _conf = resolve_company_domain_with_confidence(company)
    return domain


def resolve_company_domain_with_confidence(company: str) -> tuple[str | None, float]:
    """
    Resolve corporate domain with a 0–1 confidence score.
    <0.5 should not drive pattern inference without human confirm.
    """
    key = (company or "").lower().strip()
    key = re.sub(r"\s+(inc|ltd|llc|corp|corporation|limited|co)\.?$", "", key).strip()
    if key in DOMAIN_OVERRIDES:
        return DOMAIN_OVERRIDES[key], 1.0
    for prefix, domain in DOMAIN_OVERRIDES.items():
        if key == prefix or key.startswith(prefix + " ") or key.startswith(prefix + ","):
            return domain, 0.95
        # Avoid substring traps (roche in rochester) — only whole-token / prefix match
        if re.search(rf"(^|[^a-z]){re.escape(prefix)}([^a-z]|$)", key):
            return domain, 0.95

    enrichment = enrich_company(company)
    domain = normalize_domain(enrichment.get("domain"))
    if not domain:
        return None, 0.0

    # Reject clear mismatches (event/sponsor domains etc.)
    bad_hosts = ("eventbrite", "meetup", "linkedin", "facebook", "twitter", "youtube")
    if any(b in domain for b in bad_hosts):
        return None, 0.0

    # Academic / gov hosts are almost never the right corp domain for ICP seeds
    if domain.endswith(".edu") or domain.endswith(".gov") or domain.endswith(".ac.uk"):
        return None, 0.0

    # Name token overlap with domain slug (strict — no substring false positives)
    tokens = [t for t in re.split(r"[^a-z0-9]+", key) if len(t) > 2]
    slug = domain.split(".")[0]
    hits = sum(1 for t in tokens if _slug_token_hit(t, slug))
    if hits:
        return domain, min(0.9, 0.55 + 0.15 * hits)
    # Clearbit hit with no token overlap — low confidence
    return domain, 0.4


def domain_confidence_ok(confidence: float) -> bool:
    import os
    min_c = float(os.getenv("DOMAIN_CONFIDENCE_MIN", "0.5"))
    return confidence >= min_c


def _is_plausible_corporate_email(email: str, domain: str | None) -> bool:
    email = (email or "").lower()
    if "@" not in email:
        return False
    host = normalize_domain(email.split("@")[-1])
    if not host:
        return False
    # Never learn patterns from academic / free-mail hosts
    if host.endswith(".edu") or host.endswith(".ac.uk") or host.endswith(".gov"):
        return False
    if host in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com"):
        return False
    if not domain:
        return True
    domain = normalize_domain(domain)
    return host == domain or host.endswith("." + domain) or domain.endswith("." + host) or _domains_related(host, domain)


def _domains_related(a: str, b: str) -> bool:
    """jpmorgan.com ↔ jpmchase.com etc. — same org, different mail domain."""
    pairs = [
        {"jpmorgan.com", "jpmchase.com", "jpmorganchase.com"},
        {"citi.com", "citigroup.com"},
        {"gs.com", "goldmansachs.com"},
        {"bofa.com", "bankofamerica.com"},
        {"ms.com", "morganstanley.com"},
    ]
    for group in pairs:
        if a in group and b in group:
            return True
    return False


def get_cached_pattern(domain: str) -> dict | None:
    init_db()
    domain = normalize_domain(domain)
    if not domain:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM company_email_patterns WHERE domain = ?", (domain,)
        ).fetchone()
    return dict(row) if row else None


def save_pattern(
    domain: str,
    *,
    pattern: str,
    confidence: float,
    source: str,
    sample_email: str | None = None,
):
    init_db()
    domain = normalize_domain(domain)
    if not domain or not pattern:
        return
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO company_email_patterns (domain, pattern, confidence, source, sample_email, verified_count, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(domain) DO UPDATE SET
              pattern = excluded.pattern,
              confidence = excluded.confidence,
              source = excluded.source,
              sample_email = COALESCE(excluded.sample_email, sample_email),
              updated_at = excluded.updated_at
            """,
            (domain, pattern, confidence, source, sample_email, _now()),
        )
        conn.commit()


def infer_pattern_from_email(
    email: str,
    first_name: str | None,
    last_name: str | None,
) -> str | None:
    """Derive {first}.{last}-style pattern from one known person email."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    local = email.split("@")[0]
    # Role aliases don't encode a person pattern
    if local in ("privacy", "dpo", "dataprotection", "data-protection", "gdpr", "info", "media", "press", "ir", "investor"):
        return None
    first = (first_name or "").strip().split()[0].lower() if first_name else ""
    last = (last_name or "").strip().split()[-1].lower() if last_name else ""
    if not first or not last:
        # Heuristic from local alone: first.last
        if "." in local:
            return "{first}.{last}"
        if "_" in local:
            return "{first}_{last}"
        if "-" in local:
            return "{first}-{last}"
        return None

    for pattern in INFER_CANDIDATES:
        fn = PATTERN_PLACEHOLDERS.get(pattern)
        if not fn:
            continue
        if fn(first, last) == local:
            return pattern
    # Accent / umlaut mismatches (geissbuehler vs geissbühler) — still first.last shape
    if "." in local and local.count(".") == 1:
        return "{first}.{last}"
    return None


def _emails_already_in_db(domain: str) -> list[dict]:
    """Any stored lead email on this domain — free pattern seed."""
    init_db()
    domain = normalize_domain(domain)
    if not domain:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT email, contact_name FROM leads
            WHERE email IS NOT NULL AND email != ''
              AND (
                company_domain = ?
                OR LOWER(email) LIKE ?
              )
            LIMIT 20
            """,
            (domain, f"%@{domain}"),
        ).fetchall()
    out = []
    for row in rows:
        email = (row["email"] or "").strip().lower()
        if not email or "@" not in email:
            continue
        name = (row["contact_name"] or "").strip()
        parts = name.split() if name else []
        out.append({
            "email": email,
            "first_name": parts[0] if len(parts) >= 2 else None,
            "last_name": parts[-1] if len(parts) >= 2 else None,
            "source": "leads_db",
        })
    return out


def _try_cache_from_samples(domain: str, samples: list[dict], *, source: str) -> dict | None:
    """Infer + cache pattern from sample person emails."""
    domain = normalize_domain(domain)
    votes: dict[str, int] = {}
    by_pattern: dict[str, list[str]] = {}
    for s in samples:
        email = (s.get("email") or "").lower()
        if not email or "@" not in email:
            continue
        if not _is_plausible_corporate_email(email, domain):
            continue
        host = normalize_domain(email.split("@")[-1])
        cache_domain = domain or host
        pat = infer_pattern_from_email(email, s.get("first_name"), s.get("last_name"))
        if not pat:
            continue
        # Require the email local to actually match the inferred pattern shape
        local = email.split("@")[0]
        if "{first}.{last}" in pat or pat == "{f}.{last}" or pat == "{last}.{first}":
            if "." not in local:
                continue
        if "{first}_{last}" in pat and "_" not in local:
            continue
        if "{first}-{last}" in pat and "-" not in local:
            continue
        votes[pat] = votes.get(pat, 0) + 1
        by_pattern.setdefault(pat, []).append(email)
        domain = cache_domain

    if not votes or not domain:
        return None
    pattern = max(votes.items(), key=lambda x: x[1])[0]
    conf = min(0.95, 0.55 + 0.1 * votes[pattern])
    # Prefer a clean sample that exhibits the winning pattern
    sample_email = (by_pattern.get(pattern) or [None])[0]
    save_pattern(domain, pattern=pattern, confidence=conf, source=source, sample_email=sample_email)
    return get_cached_pattern(domain)


def _hunter_pattern(domain: str) -> dict | None:
    if not hunter_available():
        return None
    mode = (os.getenv("HUNTER_MODE") or "domain_search").lower()
    if mode != "domain_search":
        return None
    data = domain_search(domain)
    if not data:
        return None
    pattern = data.get("pattern") or ""
    sample = None
    emails = data.get("emails") or []
    if emails:
        sample = emails[0].get("value")
    if not pattern and sample:
        # Infer from first named Hunter email
        for row in emails:
            email = row.get("value")
            pat = infer_pattern_from_email(email, row.get("first_name"), row.get("last_name"))
            if pat:
                pattern = pat
                sample = email
                break
    if not pattern:
        return None
    save_pattern(
        domain,
        pattern=pattern,
        confidence=float(data.get("confidence") or 0.7),
        source="hunter",
        sample_email=sample,
    )
    return get_cached_pattern(domain)


def resolve_pattern(
    company: str,
    *,
    domain: str | None = None,
    allow_hunter: bool = True,
    on_progress=None,
) -> dict | None:
    """
    Escalating free ladder → cache forever. Hunter is last resort (1 credit).

      1. cached pattern
      2. existing verified/known email in leads.db
      3. Europe PMC author emails (pharma / life sciences)
      4. SEC EDGAR filing emails (US public cos / banks)
      5. OpenAlex affiliation backstop
      6. company site sweep (mailto + text)
      7. privacy policy / DPO block
      8. Hunter domain-search (optional, last)
    """
    emit = on_progress or (lambda msg: None)
    domain = normalize_domain(domain) or resolve_company_domain(company)
    if not domain:
        return None

    if os.getenv("EMAIL_PATTERN_CACHE", "true").lower() in ("0", "false", "no"):
        allow_cache = False
    else:
        allow_cache = True

    if allow_cache:
        cached = get_cached_pattern(domain)
        if cached and cached.get("pattern"):
            return cached

    def _accept(samples: list[dict], source: str, label: str) -> dict | None:
        if not samples:
            return None
        hit = _try_cache_from_samples(domain, samples, source=source)
        if hit:
            emit(f"  Pattern {domain} ← {label} ({len(samples)} emails)")
            return hit
        # Sample emails may live on a sibling corporate domain — cache both
        alt = normalize_domain(samples[0]["email"].split("@")[-1])
        if alt and alt != domain:
            hit = _try_cache_from_samples(alt, samples, source=source)
            if hit:
                save_pattern(
                    domain,
                    pattern=hit["pattern"],
                    confidence=float(hit.get("confidence") or 0.7),
                    source=source,
                    sample_email=hit.get("sample_email"),
                )
                emit(f"  Pattern {domain} ← {label} via {alt}")
                return get_cached_pattern(domain)
        return None

    # 2. Existing leads
    hit = _accept(_emails_already_in_db(domain), "leads_db", "leads.db")
    if hit:
        return hit

    # 3. Europe PMC
    try:
        from .europe_pmc import search_author_emails, search_deidentification_authors

        pmc = search_deidentification_authors(company, domain=domain)
        if not pmc:
            pmc = search_author_emails(company, domain=domain)
        hit = _accept(pmc, "europe_pmc", "Europe PMC")
        if hit:
            return hit
    except Exception as e:
        logger.debug("Europe PMC pattern step failed: %s", e)

    # 4. SEC EDGAR (banks / insurers / US public)
    try:
        from .sec_edgar import search_filing_emails

        sec = search_filing_emails(company, domain=domain)
        hit = _accept(sec, "sec_edgar", "SEC EDGAR")
        if hit:
            return hit
    except Exception as e:
        logger.debug("SEC pattern step failed: %s", e)

    # 5. OpenAlex backstop
    try:
        from .openalex import search_author_emails as openalex_emails

        oa = openalex_emails(company, domain=domain)
        hit = _accept(oa, "openalex", "OpenAlex")
        if hit:
            return hit
    except Exception as e:
        logger.debug("OpenAlex pattern step failed: %s", e)

    # 6. Site sweep
    try:
        from .site_emails import sweep_company_site

        site = sweep_company_site(domain)
        samples = []
        for row in site:
            email = row["email"]
            local = email.split("@")[0]
            parts = re.split(r"[._\-]", local)
            if len(parts) < 2 or not all(p.isalpha() and len(p) > 1 for p in parts[:2]):
                continue
            if any(x in local for x in ("quer", "noreply", "support", "webmaster", "newsletter", "privacy", "dpo")):
                continue
            samples.append({
                "email": email,
                "first_name": parts[0],
                "last_name": parts[1],
                "source": "site_sweep",
            })
        hit = _accept(samples, "site_sweep", "site sweep")
        if hit:
            return hit
    except Exception as e:
        logger.debug("Site sweep pattern step failed: %s", e)

    # 7. Privacy / DPO
    try:
        from .privacy_footprint import fetch_privacy_footprint

        fp = fetch_privacy_footprint(company, domain=domain)
        if fp and fp.get("dpo_email"):
            dpo = fp["dpo_email"]
            name = (fp.get("dpo_name") or "").split()
            samples = [{
                "email": dpo,
                "first_name": name[0] if len(name) >= 2 else None,
                "last_name": name[-1] if len(name) >= 2 else None,
                "source": "privacy_dpo",
            }]
            hit = _accept(samples, "privacy_dpo", "privacy policy")
            if hit:
                return hit
    except Exception as e:
        logger.debug("Privacy pattern step failed: %s", e)

    # 8. Hunter — last resort, 1 credit
    if allow_hunter:
        emit(f"  Pattern {domain} — free sources missed, trying Hunter…")
        hit = _hunter_pattern(domain)
        if hit:
            emit(f"  Pattern {domain} ← Hunter")
            return hit

    return get_cached_pattern(domain) if allow_cache else None


def fetch_and_cache_pattern(domain: str, *, company: str | None = None, allow_hunter: bool = True) -> dict | None:
    """
    Resolve + cache pattern for a domain.
    Prefer resolve_pattern(company=...) when company name is known (PMC needs it).
    """
    domain = normalize_domain(domain)
    if not domain:
        return None
    cached = get_cached_pattern(domain)
    if cached and cached.get("pattern"):
        return cached
    return resolve_pattern(company or domain.split(".")[0].title(), domain=domain, allow_hunter=allow_hunter)


def apply_pattern_to_name(pattern: str, first_name: str, last_name: str, domain: str) -> str | None:
    if not pattern or not domain or not last_name:
        return None
    parts = first_name.strip().split()
    first = parts[0] if parts else ""
    last = last_name.strip().split()[-1] if last_name.strip() else ""
    if not last:
        return None

    fn = PATTERN_PLACEHOLDERS.get(pattern)
    if fn:
        local = fn(first, last)
    else:
        local = pattern
        for key, repl in PATTERN_PLACEHOLDERS.items():
            local = local.replace(key, repl(first, last))
    local = re.sub(r"[^a-z0-9._-]", "", local.lower())
    if not local or "@" in local:
        return None
    return f"{local}@{normalize_domain(domain)}"


def infer_email_for_contact(
    contact_name: str,
    company: str,
    *,
    domain: str | None = None,
    allow_hunter: bool = False,
) -> dict | None:
    """
    Resolve email via cached/free pattern. Hunter off by default here —
    pattern resolution should have already run in the enrich step.
    """
    if domain:
        domain = normalize_domain(domain)
        conf = 1.0  # caller-provided domain is trusted
    else:
        domain, conf = resolve_company_domain_with_confidence(company)
    if not domain:
        return None
    if not domain_confidence_ok(conf):
        logger.info("Skip pattern email for %s — domain confidence %.2f too low (%s)", company, conf, domain)
        return None
    row = get_cached_pattern(domain)
    if not row or not row.get("pattern"):
        row = resolve_pattern(company, domain=domain, allow_hunter=allow_hunter)
    if not row or not row.get("pattern"):
        return None
    parts = contact_name.strip().split()
    if len(parts) < 2:
        return None
    email = apply_pattern_to_name(row["pattern"], parts[0], parts[-1], domain)
    if not email:
        return None
    source = row.get("source") or "pattern"
    return {
        "email": email,
        "email_inferred": 1,
        "email_source": f"{source}_pattern",
        "company_domain": domain,
        "contact_tier": "B",
        "domain_confidence": conf,
    }


def resolve_patterns_for_companies(
    companies: list[str],
    *,
    allow_hunter: bool = True,
    on_progress=None,
) -> dict:
    """Walk the free ladder for each company domain; Hunter only when needed."""
    emit = on_progress or (lambda msg: None)
    resolved = hunter_used = failed = 0
    sources: dict[str, int] = {}

    for i, company in enumerate(companies, 1):
        company = (company or "").strip()
        if not company:
            continue
        domain = resolve_company_domain(company)
        emit(f"  Pattern [{i}/{len(companies)}] {company} ({domain or 'no domain'})…")
        before = get_cached_pattern(domain) if domain else None
        row = resolve_pattern(company, domain=domain, allow_hunter=allow_hunter, on_progress=emit)
        if row and row.get("pattern"):
            resolved += 1
            src = row.get("source") or "unknown"
            sources[src] = sources.get(src, 0) + 1
            if src == "hunter" and not (before and before.get("pattern")):
                hunter_used += 1
        else:
            failed += 1

    emit(f"Patterns — {resolved} resolved, {hunter_used} Hunter credits, {failed} unresolved")
    return {
        "resolved": resolved,
        "hunter_credits": hunter_used,
        "failed": failed,
        "sources": sources,
    }
