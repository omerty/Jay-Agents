"""Outcomes learning loop — weights from reply/meeting events."""

from __future__ import annotations

import logging
from collections import Counter

from .db import get_connection, init_db

logger = logging.getLogger(__name__)


def get_learning_weights(agent: str = "woodway") -> dict:
    """Recompute weights from outcomes for scoring feedback."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT o.reply_class, o.event, l.industry, l.contact_title, l.source, l.signal
            FROM outcomes o
            JOIN leads l ON l.id = o.lead_id
            WHERE l.agent = ?
            """,
            (agent,),
        ).fetchall()

    pos_ind: Counter = Counter()
    neg_ind: Counter = Counter()
    pos_title: Counter = Counter()
    neg_title: Counter = Counter()
    pos_source: Counter = Counter()
    neg_source: Counter = Counter()

    for row in rows:
        rc = row["reply_class"] or row["event"]
        industry = (row["industry"] or "unknown").lower().strip() or "unknown"
        title = (row["contact_title"] or "unknown").lower().strip() or "unknown"
        # Normalize title to short key
        title_key = title
        for key in ("chief privacy", "data governance", "data protection", "ai governance", "privacy officer"):
            if key in title:
                title_key = key
                break
        source = (row["source"] or "unknown").lower().strip() or "unknown"

        if rc in ("positive", "referral", "meeting", "opportunity"):
            pos_ind[industry] += 1
            pos_title[title_key] += 1
            pos_source[source] += 1
        elif rc in ("negative", "wrong_person", "bounce"):
            neg_ind[industry] += 1
            neg_title[title_key] += 1
            neg_source[source] += 1

    def to_weights(pos: Counter, neg: Counter) -> dict:
        weights = {}
        for key in set(pos) | set(neg):
            p = pos[key]
            n = neg[key]
            weights[key] = round(1.0 + (p - n) * 0.05, 2)
        return weights

    return {
        "industry_weights": to_weights(pos_ind, neg_ind),
        "title_weights": to_weights(pos_title, neg_title),
        "source_weights": to_weights(pos_source, neg_source),
        "positive_count": sum(pos_ind.values()),
        "negative_count": sum(neg_ind.values()),
    }


def apply_learning_boost(score: int, industry: str | None, title: str | None, *, agent: str = "woodway") -> tuple[int, list[str]]:
    """Apply small learned adjustments to an ICP score. Returns (new_score, reasons)."""
    weights = get_learning_weights(agent)
    reasons = []
    delta = 0.0
    ind = (industry or "").lower().strip()
    if ind and ind in (weights.get("industry_weights") or {}):
        w = weights["industry_weights"][ind]
        adj = int(round((w - 1.0) * 20))  # ±1 point per 0.05 weight → ±1
        if adj:
            delta += adj
            reasons.append(f"Learning boost industry={ind} ({adj:+d})")
    title_l = (title or "").lower()
    for key, w in (weights.get("title_weights") or {}).items():
        if key != "unknown" and key in title_l:
            adj = int(round((w - 1.0) * 15))
            if adj:
                delta += adj
                reasons.append(f"Learning boost title={key} ({adj:+d})")
            break
    new_score = max(0, min(100, int(score + delta)))
    return new_score, reasons
