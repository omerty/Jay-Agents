"""Evidence-bound outreach — every product/factual claim must cite proof or signal."""

from __future__ import annotations

import logging
import os
import re

from .outreach import load_proof_library

logger = logging.getLogger(__name__)

# Phrases that usually imply a product/regulatory claim needing a proof id
CLAIMISH = (
    r"\bevidata\b",
    r"\bre-?identification\b",
    r"\banonymi[sz]",
    r"\bexpert determination\b",
    r"\bdefensible metrics\b",
    r"\bregulator",
    r"\b\d+%\b",  # ROI-ish
)


def citation_enforcement_enabled() -> bool:
    return os.getenv("OUTREACH_REQUIRE_CITATIONS", "true").lower() not in ("0", "false", "no")


def allowed_proof_ids(agent: str = "woodway") -> set[str]:
    lib = load_proof_library(agent)
    return {c["id"] for c in (lib.get("claims") or []) if c.get("id")}


def parse_citation_block(raw: dict | list | None) -> list[dict]:
    """Normalize model citation output to [{sentence, source}, ...]."""
    if not raw:
        return []
    if isinstance(raw, dict) and "citations" in raw:
        raw = raw["citations"]
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        sent = (item.get("sentence") or item.get("text") or "").strip()
        src = (item.get("source") or item.get("cite") or item.get("id") or "").strip()
        if sent and src:
            out.append({"sentence": sent, "source": src})
    return out


def validate_citations(
    body: str,
    citations: list[dict],
    *,
    agent: str = "woodway",
    allowed_extra: set[str] | None = None,
) -> dict:
    """
    Reject drafts with uncited claimish sentences or unknown citation sources.
    Returns {ok, issues, citations}.
    """
    if not citation_enforcement_enabled():
        return {"ok": True, "issues": [], "citations": citations, "skipped": True}

    proof_ids = allowed_proof_ids(agent)
    allowed = set(proof_ids)
    allowed |= {f"proof:{pid}" for pid in proof_ids}
    allowed |= {"brief", "privacy", "signal"}  # bare signal ok if signal:N also used
    if allowed_extra:
        allowed |= allowed_extra
        # also allow bare "signal" when signal:N present
        for x in list(allowed_extra):
            if x.startswith("signal:"):
                allowed.add("signal")

    issues: list[str] = []
    cited_sources = set()
    for c in citations:
        src = c["source"]
        cited_sources.add(src)
        # Normalize proof:id
        ok_src = src in allowed or src.replace("proof:", "") in proof_ids
        if src.startswith("signal:"):
            ok_src = src in allowed or "signal" in (allowed_extra or set()) or src in allowed
        if not ok_src and not (src.startswith("signal:") and allowed_extra and src in allowed_extra):
            # signal:N must be in allowed_extra from brief
            if src.startswith("signal:"):
                if not allowed_extra or src not in allowed_extra:
                    issues.append(f"unknown signal cite: {src}")
            elif src.replace("proof:", "") not in proof_ids and src not in allowed:
                issues.append(f"unknown citation source: {src}")

    # Split body into rough sentences (skip subject/sign-off)
    lines = []
    for line in (body or "").splitlines():
        s = line.strip()
        if not s or s.lower().startswith("subject:"):
            continue
        if s.lower().startswith("best") or s.lower().startswith("hi "):
            continue
        lines.append(s)
    blob = " ".join(lines)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", blob) if s.strip()]

    claim_pat = re.compile("|".join(CLAIMISH), re.I)
    cited_text = " ".join(c["sentence"].lower() for c in citations)

    for sent in sentences:
        if not claim_pat.search(sent):
            continue
        # Must appear in citations list (fuzzy: first 40 chars)
        key = sent.lower()[:40]
        if not any(key in c["sentence"].lower() or c["sentence"].lower()[:40] in sent.lower() for c in citations):
            # Soft: if any proof cite exists and sentence is product boilerplate, allow
            if any(s.replace("proof:", "") in proof_ids or s.startswith("proof:") for s in cited_sources):
                if re.search(r"\bevidata\b", sent, re.I) and len(citations) >= 1:
                    continue
            issues.append(f"uncited claimish sentence: {sent[:100]}")

    # Forbidden patterns from proof library
    lib = load_proof_library(agent)
    lower = (body or "").lower()
    if re.search(r"\b\d{2,3}%\b", lower) and "roi" not in " ".join(proof_ids):
        # ROI-ish number with no stats in library
        if not any("stat" in (c.get("tags") or []) for c in (lib.get("claims") or [])):
            issues.append("numeric ROI-like claim without proof_library stats")
    for forbid in lib.get("forbidden") or []:
        # soft check — keywords
        if "roi" in forbid.lower() and re.search(r"\b\d+%\b", lower):
            if "specific ROI" in forbid or "ROI percentages" in forbid:
                issues.append(f"forbidden: {forbid}")

    return {"ok": len(issues) == 0, "issues": issues, "citations": citations}


def extract_citations_via_llm(body: str, *, allowed: set[str], use_llm: bool = True) -> list[dict]:
    """Ask model to annotate an existing draft with citations from an allowed set."""
    if not use_llm or not body.strip():
        return []
    from .llm import chat_json

    allow_list = ", ".join(sorted(allowed)[:40]) or "brief, proof:evidata_core"
    try:
        result = chat_json(
            f"""Annotate this email. For each sentence that makes a factual or product claim,
cite ONE source from the allowed list. Greeting/sign-off need no citation.

ALLOWED SOURCES: {allow_list}

EMAIL:
{body[:2000]}

JSON: {{"citations": [{{"sentence": "...", "source": "proof:evidata_core|signal:123|brief|privacy"}}]}}""",
            system="You annotate outreach with citations only from the allowed list. JSON only.",
            max_tokens=512,
            temperature=0.1,
            task="critique",
        )
        return parse_citation_block(result)
    except Exception as e:
        logger.warning("Citation extract failed: %s", e)
        return []
