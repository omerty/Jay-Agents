"""Query-pool learning — promote industries/queries that convert, demote noise."""

from __future__ import annotations

import logging
import re
from collections import Counter

from .db import get_connection, init_db
from .outcomes import get_learning_weights

logger = logging.getLogger(__name__)


def _industry_tokens(weights: dict) -> list[tuple[str, float]]:
    items = sorted(
        (weights.get("industry_weights") or {}).items(),
        key=lambda kv: kv[1],
        reverse=True,
    )
    return [(k, float(v)) for k, v in items]


def score_query(query: str, industry_weights: dict[str, float]) -> float:
    """Higher = prefer this query on the next rotation."""
    q = (query or "").lower()
    score = 1.0
    for industry, weight in industry_weights.items():
        if industry == "unknown":
            continue
        # Match industry keyword inside query
        token = industry.split()[0]
        if token and token in q:
            score += (weight - 1.0) * 2.0
    # Soft prefer governance/privacy buyer language
    if any(t in q for t in ("chief privacy", "data governance", "data protection", "de-identification")):
        score += 0.15
    # Soft demote vendor-y language
    if any(t in q for t in ("vendor", "startup", "saas tool")):
        score -= 0.3
    return score


def rank_query_pool(pool: list[str], *, agent: str = "woodway") -> list[str]:
    """Return pool sorted by learned yield (best first), stable for ties."""
    if not pool:
        return []
    weights = get_learning_weights(agent)
    industry_w = {k: float(v) for k, v in (weights.get("industry_weights") or {}).items()}
    title_w = {k: float(v) for k, v in (weights.get("title_weights") or {}).items()}

    # Boost queries that mention winning titles
    scored = []
    for i, q in enumerate(pool):
        s = score_query(q, industry_w)
        ql = q.lower()
        for title, tw in title_w.items():
            if title and title in ql:
                s += (tw - 1.0) * 1.5
        scored.append((s, i, q))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [q for _, _, q in scored]


def record_query_outcome(agent: str, query: str, *, positive: bool):
    """Optional fine-grained query yield counter stored in agent_cursors JSON-ish string."""
    from .db import get_cursor, set_cursor

    # Use hashed buckets via simple counter table
    init_db()
    key = re.sub(r"\s+", " ", (query or "").strip().lower())[:180]
    if not key:
        return
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_yield (
                agent TEXT NOT NULL,
                query_key TEXT NOT NULL,
                positives INTEGER DEFAULT 0,
                negatives INTEGER DEFAULT 0,
                PRIMARY KEY (agent, query_key)
            )
            """
        )
        row = conn.execute(
            "SELECT positives, negatives FROM query_yield WHERE agent = ? AND query_key = ?",
            (agent, key),
        ).fetchone()
        if row:
            p = int(row["positives"]) + (1 if positive else 0)
            n = int(row["negatives"]) + (0 if positive else 1)
            conn.execute(
                "UPDATE query_yield SET positives = ?, negatives = ? WHERE agent = ? AND query_key = ?",
                (p, n, agent, key),
            )
        else:
            conn.execute(
                "INSERT INTO query_yield (agent, query_key, positives, negatives) VALUES (?, ?, ?, ?)",
                (agent, key, 1 if positive else 0, 0 if positive else 1),
            )
        conn.commit()


def query_yield_bonus(agent: str, query: str) -> float:
    init_db()
    key = re.sub(r"\s+", " ", (query or "").strip().lower())[:180]
    try:
        with get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_yield (
                    agent TEXT NOT NULL,
                    query_key TEXT NOT NULL,
                    positives INTEGER DEFAULT 0,
                    negatives INTEGER DEFAULT 0,
                    PRIMARY KEY (agent, query_key)
                )
                """
            )
            row = conn.execute(
                "SELECT positives, negatives FROM query_yield WHERE agent = ? AND query_key = ?",
                (agent, key),
            ).fetchone()
    except Exception:
        return 0.0
    if not row:
        return 0.0
    p, n = int(row["positives"]), int(row["negatives"])
    return round((p - n) * 0.1, 2)
