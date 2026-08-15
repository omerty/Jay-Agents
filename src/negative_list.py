"""Hard exclusion list — competitors, vendors, law firms, media, etc."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

AGENTS_DIR = Path(__file__).parent.parent / "agents"

# Privacy-tech competitors and common DDG noise (Woodway-focused)
DEFAULT_EXCLUDE_PATTERNS = [
    r"\bonetrust\b",
    r"\bbigid\b",
    r"\bprivitar\b",
    r"\bimmuta\b",
    r"\btrustarc\b",
    r"\bwirewheel\b",
    r"\bsecuriti\b",
    r"\biapp\b",
    r"\bprivacy analytics\b",
    r"\bprivac(y|ies)\s+(law|legal|attorney|firm)\b",
    r"\b(deloitte|pwc|kpmg|ey|accenture)\b",
    r"\b(conference|summit|webinar|podcast)\b",
    r"\b(news|media|journal|magazine)\b",
    r"\b(university|college)\b",
    r"\bconsulting\b.*\b(privacy|data)\b",
    r"\b(epic systems|cerner|oracle health|axtria|veeva|iqvia)\b",
    r"\b(health\s*it|ehr vendor|pharma data vendor)\b",
    r"\b(e[- ]?commerce|online retail)\b",
    r"\bcarrier global\b",
    r"\bhvac\b",
]

DEFAULT_EXCLUDE_NAMES = frozenset({
    "onetrust", "bigid", "privitar", "immuta", "trustarc", "iapp",
    "privacy analytics", "wirewheel", "securiti", "anonymization.io",
    # Health-IT / pharma-data vendors (sell to buyers, not buyers)
    "epic systems", "epic", "cerner", "oracle health", "axtria", "veeva",
    "iqvia", "medidata", "flatiron", "health catalyst",
    # E-commerce / retail not in ICP
    "thg", "the hut group",
    # HVAC / industrial false positives from "carrier" snippet noise
    "carrier", "carrier global", "carrier corporation",
    # Consultancies below enterprise floor
    "erni", "ernst & young", "ey",
})


def _compile(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p, re.I))
        except re.error:
            continue
    return out


def load_negative_list(agent: str = "woodway") -> dict:
    path = AGENTS_DIR / agent / "config.yaml"
    if not path.exists():
        return {"patterns": DEFAULT_EXCLUDE_PATTERNS, "names": set(DEFAULT_EXCLUDE_NAMES)}
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    neg = cfg.get("negative_list") or {}
    patterns = list(DEFAULT_EXCLUDE_PATTERNS)
    patterns.extend(neg.get("patterns") or [])
    names = set(DEFAULT_EXCLUDE_NAMES)
    names.update(n.lower() for n in (neg.get("names") or []))
    names.update(n.lower() for n in (cfg.get("blocklist") or []))
    return {"patterns": patterns, "names": names}


def is_excluded(company: str, *, agent: str = "woodway", extra_text: str = "") -> str | None:
    """Return exclusion reason or None if OK."""
    if not company:
        return "empty company"
    text = f"{company} {extra_text}".lower()
    nl = load_negative_list(agent)
    key = company.lower().strip()
    # Strip punctuation / legal suffixes so "Axtria, Inc." matches "axtria"
    key_simple = re.sub(r"[,.]", " ", key)
    key_simple = re.sub(
        r"\s+(inc|ltd|llc|corp|corporation|limited|group|global|co)\.?\s*$",
        "",
        key_simple,
    ).strip()
    key_simple = re.sub(r"\s+", " ", key_simple)
    if key in nl["names"] or key_simple in nl["names"]:
        return "blocklist"
    for pat in _compile(nl["patterns"]):
        if pat.search(text) or pat.search(key_simple):
            return f"pattern:{pat.pattern[:40]}"
    return None
