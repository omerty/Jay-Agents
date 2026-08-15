"""Stateful retrieval for stateless LLM calls.

DB = long-term memory. Cached system block = working memory.
Each call gets only the slice it needs — never the whole thread.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .db import get_connection, init_db, normalize_company
from .email_patterns import normalize_domain, resolve_company_domain
from .negative_list import load_negative_list

logger = logging.getLogger(__name__)
AGENTS_DIR = Path(__file__).parent.parent / "agents"


def _load_few_shots(agent: str) -> dict:
    path = AGENTS_DIR / agent / "few_shots.yaml"
    if not path.exists():
        return {"good": [], "bad": []}
    with open(path) as f:
        return yaml.safe_load(f) or {"good": [], "bad": []}


def learned_exclusions(agent: str = "woodway", *, limit: int = 25) -> list[str]:
    """Companies Jay / the pipeline rejected, with reasons — highest-value memory."""
    lines = []
    nl = load_negative_list(agent)
    for name in sorted(nl["names"])[:15]:
        lines.append(f"- {name.title()} — negative list / vendor")
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT company, qualification_json, signal FROM leads
            WHERE agent = ? AND status = 'skipped'
              AND company IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    seen = {n.lower() for n in nl["names"]}
    for row in rows:
        company = (row["company"] or "").strip()
        if not company or company.lower() in seen:
            continue
        seen.add(company.lower())
        reason = "rejected"
        qj = row["qualification_json"] or ""
        if "Negative list" in qj:
            reason = "negative list"
        elif row["signal"]:
            reason = (row["signal"] or "")[:80]
        lines.append(f"- {company} — {reason}")
        if len(lines) >= limit:
            break
    return lines


def journal_summary(agent: str = "woodway") -> str:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT summary FROM run_journal
            WHERE agent = ? AND kind = 'summary'
            ORDER BY created_at DESC LIMIT 1
            """,
            (agent,),
        ).fetchone()
    return (row["summary"] if row else "") or ""


def build_cached_working_memory(config: dict, *, task: str = "qualify") -> str:
    """
    Stable block for Anthropic prompt cache — identical across calls until config changes.
    Keep under ~2–3KB so cache stays hot.
    """
    agent = config.get("name") or "woodway"
    icp = config.get("icp") or {}
    prompts = config.get("prompts") or {}
    industries = icp.get("industries") or []
    if isinstance(industries, str):
        industries = [industries]
    titles = icp.get("titles") or []

    parts = [
        f"You work for {config.get('company')} ({config.get('product')}).",
        f"Tagline: {config.get('tagline', '')}",
        "",
        "IDEAL CUSTOMER PROFILE:",
        f"- Industries: {', '.join(industries)}",
        f"- Titles: {', '.join(titles[:8])}",
        f"- Size: prefer "
        f"{icp.get('prefer_employees_min', 1500)}–{icp.get('prefer_employees_max', 10000)} "
        f"(min={icp.get('min_employees', 1000)}; megabrands allowed)",
        f"- Geography: {icp.get('geography', 'global')}",
    ]
    extra = (prompts.get("qualify_extra") or "").strip()
    if extra and task in ("qualify", "extract", "digest"):
        parts += ["", "SCORING NOTES:", extra[:800]]

    excl = learned_exclusions(agent, limit=20)
    if excl:
        parts += ["", "LEARNED EXCLUSIONS (never treat as buyers):", *excl]

    shots = _load_few_shots(agent)
    good = shots.get("good") or []
    bad = shots.get("bad") or []
    if good or bad:
        parts.append("")
        parts.append("FEW-SHOT EXAMPLES:")
        for g in good[:3]:
            parts.append(f"  GOOD: {g.get('company')} — {g.get('why', '')}")
        for b in bad[:3]:
            parts.append(f"  BAD: {b.get('company')} — {b.get('why', '')}")

    if task == "outreach":
        try:
            from .outreach import load_proof_library

            lib = load_proof_library(agent)
            claims = lib.get("claims") or []
            if claims:
                parts.append("")
                parts.append("APPROVED CLAIMS (only use these facts):")
                for c in claims[:8]:
                    parts.append(f"  - {c.get('text')}")
        except Exception:
            pass

    journal = journal_summary(agent)
    if journal:
        parts += ["", "CROSS-RUN LEARNINGS:", journal[:1200]]

    return "\n".join(parts).strip()


def known_funnel_for_discovery(
    agent: str = "woodway",
    *,
    limit: int = 15,
    hint_text: str | None = None,
) -> str:
    """
    Tiny retrieved slice for Claude — never the full catalog.

    Source of truth for 1,000+ accounts is SQLite (post-extract skip).
    The prompt only lists known companies that overlap *this run's* search text,
    so context stays ~200–400 tokens even when the funnel is huge.
    """
    init_db()
    with get_connection() as conn:
        total = conn.execute(
            """
            SELECT COUNT(DISTINCT company_normalized) FROM leads
            WHERE agent = ? AND status NOT IN ('skipped')
              AND company IS NOT NULL AND TRIM(company) != ''
            """,
            (agent,),
        ).fetchone()[0]
        rows = conn.execute(
            """
            SELECT company, contact_name, contact_title, status
            FROM leads
            WHERE agent = ?
              AND company IS NOT NULL AND TRIM(company) != ''
              AND status NOT IN ('skipped')
            ORDER BY updated_at DESC
            LIMIT 800
            """,
            (agent,),
        ).fetchall()
    if not rows:
        return ""

    hint = (hint_text or "").lower()
    by_co: dict[str, dict] = {}
    for r in rows:
        name = (r["company"] or "").strip()
        key = normalize_company(name)
        if not key:
            continue
        rec = by_co.get(key)
        if not rec:
            rec = {"company": name, "people": [], "key": key}
            by_co[key] = rec
        person = (r["contact_name"] or "").strip()
        if person and person not in rec["people"]:
            rec["people"].append(person)

    # Retrieve only names that show up in this run's snippets (or digest list)
    if hint:
        overlapping = []
        for rec in by_co.values():
            name_l = rec["company"].lower()
            if name_l in hint or rec["key"] in hint:
                overlapping.append(rec)
                continue
            # first significant token (skip "the")
            tok = rec["key"].split()[0] if rec["key"] else ""
            if len(tok) >= 5 and tok in hint:
                overlapping.append(rec)
        chosen = overlapping[:limit]
    else:
        chosen = list(by_co.values())[:limit]

    lines = [
        f"FUNNEL MEMORY: {int(total)} companies already stored in the database. "
        f"Extract NEW names only — known duplicates are dropped after this call (do not list them).",
    ]
    if chosen:
        lines.append("Known accounts that appear in THIS batch (skip these):")
        for rec in chosen:
            people = rec["people"][:1]
            extra = f" ({people[0]})" if people else ""
            lines.append(f"- {rec['company']}{extra}")
    return "\n".join(lines)


def build_context(
    company: str,
    *,
    agent: str = "woodway",
    domain: str | None = None,
    max_chars: int = 1200,
) -> str:
    """
    Per-lead retrieval — only this company's DB state (~500–1200 tokens).
    Inject into the dynamic user message, never the cached system block.
    """
    domain = normalize_domain(domain) or resolve_company_domain(company)
    key = normalize_company(company)
    init_db()
    lines: list[str] = [f"DB STATE for {company}" + (f" ({domain})" if domain else "") + ":"]

    with get_connection() as conn:
        leads = conn.execute(
            """
            SELECT contact_name, contact_title, email, linkedin_url, status, score, tier,
                   signal, outreach_subject, contact_tier, source, updated_at
            FROM leads
            WHERE agent = ? AND (
              company_normalized = ?
              OR (? IS NOT NULL AND company_domain = ?)
            )
            ORDER BY updated_at DESC
            LIMIT 5
            """,
            (agent, key, domain, domain),
        ).fetchall()

        for lead in leads:
            contact = lead["contact_name"] or "(no contact yet)"
            title = lead["contact_title"] or ""
            email = lead["email"] or "no email"
            lines.append(
                f"- Lead: {contact}"
                + (f", {title}" if title else "")
                + f" | {email} | status={lead['status']}"
                + (f" score={lead['score']}" if lead["score"] is not None else "")
                + (f" tier={lead['tier']}" if lead["tier"] else "")
            )
            if lead["signal"]:
                lines.append(f"  signal: {(lead['signal'] or '')[:140]}")
            if lead["outreach_subject"]:
                lines.append(f"  prior outreach subject: {lead['outreach_subject']}")

        if domain:
            pat = conn.execute(
                "SELECT pattern, source, sample_email FROM company_email_patterns WHERE domain = ?",
                (domain,),
            ).fetchone()
            if pat:
                lines.append(
                    f"- Email pattern: {pat['pattern']} (via {pat['source']}, sample={pat['sample_email']})"
                )
            fp = conn.execute(
                "SELECT dpo_name, dpo_email, mentions_deidentification FROM privacy_footprint WHERE domain = ?",
                (domain,),
            ).fetchone()
            if fp:
                lines.append(
                    f"- Privacy footprint: DPO={fp['dpo_name'] or 'n/a'}, "
                    f"email={fp['dpo_email'] or 'n/a'}, "
                    f"de-id={'yes' if fp['mentions_deidentification'] else 'no'}"
                )
            sigs = conn.execute(
                """
                SELECT signal_type, snippet, detected_at FROM signals
                WHERE agent = ? AND (company_domain = ? OR company = ?)
                ORDER BY detected_at DESC LIMIT 4
                """,
                (agent, domain, company),
            ).fetchall()
            for s in sigs:
                lines.append(f"- Signal [{s['signal_type']}]: {(s['snippet'] or '')[:100]}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20].rstrip() + "…"
    return text


def append_journal(agent: str, observation: str, *, kind: str = "note"):
    """Append a cross-run observation. Summarize periodically so it stays bounded."""
    if not observation or not observation.strip():
        return
    init_db()
    from .db import _now

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO run_journal (agent, kind, summary, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (agent, kind, observation.strip()[:2000], _now()),
        )
        conn.commit()


def maybe_compress_journal(agent: str = "woodway", *, keep_notes: int = 30):
    """If notes pile up, Haiku-summarize into one summary row and trim old notes."""
    init_db()
    with get_connection() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM run_journal WHERE agent = ? AND kind = 'note'",
            (agent,),
        ).fetchone()[0]
    if n < keep_notes:
        return None

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT summary FROM run_journal
            WHERE agent = ? AND kind IN ('note', 'summary')
            ORDER BY created_at DESC LIMIT 40
            """,
            (agent,),
        ).fetchall()
    blob = "\n".join(f"- {r['summary']}" for r in rows if r["summary"])
    try:
        from .llm import chat

        summary = chat(
            f"Compress these pipeline learnings into ≤12 bullet points for Woodway ICP memory.\n\n{blob[:6000]}",
            system="You summarize B2B pipeline learnings. Output plain bullets only.",
            max_tokens=400,
            temperature=0.2,
            task="critique",
        )
    except Exception as e:
        logger.debug("Journal compress failed: %s", e)
        return None

    from .db import _now

    with get_connection() as conn:
        conn.execute(
            "INSERT INTO run_journal (agent, kind, summary, created_at) VALUES (?, 'summary', ?, ?)",
            (agent, summary.strip()[:2000], _now()),
        )
        # Keep latest summary + recent notes only
        conn.execute(
            """
            DELETE FROM run_journal WHERE agent = ? AND kind = 'note'
              AND id NOT IN (
                SELECT id FROM run_journal WHERE agent = ? AND kind = 'note'
                ORDER BY created_at DESC LIMIT 10
              )
            """,
            (agent, agent),
        )
        conn.commit()
    return summary
