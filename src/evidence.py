"""Evidence-or-drop — every persisted field needs source URL + snippet."""

from __future__ import annotations

import logging
from typing import Any

from .db import get_evidence, save_evidence

logger = logging.getLogger(__name__)


def _scalar_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _evidence_ok(field: dict | str | int | float | None) -> tuple[str | None, str | None, str | None]:
    """Parse {value, src, snippet} or plain string (no evidence)."""
    if field is None:
        return None, None, None
    if isinstance(field, str):
        return field.strip() or None, None, None
    if isinstance(field, (int, float)):
        return str(field), None, None
    if not isinstance(field, dict):
        return _scalar_str(field) or None, None, None
    val = _scalar_str(field.get("value"))
    src = _scalar_str(field.get("src") or field.get("source_url"))
    snip = _scalar_str(field.get("snippet"))
    if val and src and snip:
        return val, src, snip
    if val:
        logger.debug("Dropping field without evidence: %s", val[:40])
    return None, None, None


def filter_evidence_or_drop(raw: dict, fields: list[str]) -> dict:
    """Keep only fields with valid evidence objects."""
    out = {}
    for key in fields:
        val, src, snip = _evidence_ok(raw.get(key))
        if val and src and snip:
            out[key] = {"value": val, "src": src, "snippet": snip}
    # Plain company name may be top-level string
    if raw.get("company") and isinstance(raw["company"], str):
        out["company"] = raw["company"]
    elif isinstance(raw.get("company"), dict):
        val, src, snip = _evidence_ok(raw["company"])
        if val:
            out["company"] = val
            if src and snip:
                out["_company_evidence"] = {"src": src, "snippet": snip}
    return out


def persist_entity_evidence(
    entity_type: str,
    cleaned: dict,
    *,
    entity_id: int | None = None,
    company: str | None = None,
) -> list[int]:
    """Save evidence rows for each evidenced field."""
    ids = []
    co = company or cleaned.get("company")
    if isinstance(co, dict):
        co = co.get("value")
    for field, data in cleaned.items():
        if field.startswith("_") or field == "company":
            continue
        if isinstance(data, dict) and data.get("value"):
            eid = save_evidence(
                entity_type,
                field,
                data["value"],
                entity_id=entity_id,
                company=co,
                source_url=data.get("src"),
                snippet=data.get("snippet"),
            )
            if eid:
                ids.append(eid)
    ev = cleaned.get("_company_evidence")
    if ev and co:
        eid = save_evidence(
            "company", "company", co,
            entity_id=entity_id, company=co,
            source_url=ev.get("src"), snippet=ev.get("snippet"),
        )
        if eid:
            ids.append(eid)
    return ids


def flatten_for_record(cleaned: dict) -> dict:
    """Convert evidenced dict to flat lead fields."""
    out: dict[str, Any] = {}
    for key, data in cleaned.items():
        if key.startswith("_"):
            continue
        if isinstance(data, dict) and "value" in data:
            out[key] = data["value"]
        elif isinstance(data, str):
            out[key] = data
    return out
