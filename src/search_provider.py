"""Resilient web search — Brave API + DuckDuckGo with SERP cache and circuit breaker."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
from ddgs import DDGS

from .db import get_connection, init_db, _now

logger = logging.getLogger(__name__)

_BRAVE_FAILURES = 0
_DDG_FAILURES = 0
_BRAVE_OPEN_UNTIL = 0.0
_DDG_OPEN_UNTIL = 0.0


def _cache_key(query: str) -> str:
    return hashlib.sha256(query.strip().lower().encode()).hexdigest()


def _ttl_hours() -> int:
    return int(os.getenv("SERP_CACHE_TTL_HOURS", "168"))


def get_serp_cache(query: str) -> list[dict] | None:
    init_db()
    key = _cache_key(query)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT results_json, expires_at FROM serp_cache WHERE query_hash = ?", (key,)
        ).fetchone()
    if not row:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            return None
        return json.loads(row["results_json"])
    except Exception:
        return None


def save_serp_cache(query: str, results: list[dict], *, provider: str):
    init_db()
    key = _cache_key(query)
    exp = (datetime.now(timezone.utc) + timedelta(hours=_ttl_hours())).isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO serp_cache (query_hash, query_text, provider, results_json, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_hash) DO UPDATE SET
              results_json = excluded.results_json,
              provider = excluded.provider,
              expires_at = excluded.expires_at
            """,
            (key, query[:500], provider, json.dumps(results), exp, _now()),
        )
        conn.commit()


def _circuit_open(until: float) -> bool:
    return time.monotonic() < until


def _record_failure(kind: str):
    global _BRAVE_FAILURES, _DDG_FAILURES, _BRAVE_OPEN_UNTIL, _DDG_OPEN_UNTIL
    threshold = int(os.getenv("SEARCH_CIRCUIT_THRESHOLD", "3"))
    cooldown = float(os.getenv("SEARCH_CIRCUIT_COOLDOWN", "120"))
    if kind == "brave":
        _BRAVE_FAILURES += 1
        if _BRAVE_FAILURES >= threshold:
            _BRAVE_OPEN_UNTIL = time.monotonic() + cooldown + random.uniform(0, 30)
            logger.warning("Brave search circuit open for %.0fs", cooldown)
    else:
        _DDG_FAILURES += 1
        if _DDG_FAILURES >= threshold:
            _DDG_OPEN_UNTIL = time.monotonic() + cooldown + random.uniform(0, 30)
            logger.warning("DDG search circuit open for %.0fs", cooldown)


def _record_success(kind: str):
    global _BRAVE_FAILURES, _DDG_FAILURES
    if kind == "brave":
        _BRAVE_FAILURES = 0
    else:
        _DDG_FAILURES = 0


def brave_search(query: str, max_results: int = 5) -> list[dict]:
    api_key = os.getenv("BRAVE_SEARCH_API_KEY", "")
    if not api_key or _circuit_open(_BRAVE_OPEN_UNTIL):
        return []
    try:
        r = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            timeout=15.0,
        )
        if r.status_code == 429:
            _record_failure("brave")
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Brave search failed: %s", e)
        _record_failure("brave")
        return []

    results = []
    for hit in (data.get("web") or {}).get("results") or []:
        results.append({
            "query": query,
            "title": hit.get("title", ""),
            "snippet": hit.get("description", ""),
            "url": hit.get("url", ""),
        })
    _record_success("brave")
    return results[:max_results]


def ddg_search(query: str, max_results: int = 5) -> list[dict]:
    if _circuit_open(_DDG_OPEN_UNTIL):
        return []
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
        _record_success("ddg")
    except Exception as e:
        logger.warning("DDG search failed: %s", e)
        _record_failure("ddg")
    return results


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """
    Cached resilient search. Provider order from SEARCH_PROVIDER env:
    auto (Brave if key, else DDG), brave, ddg.
    """
    cached = get_serp_cache(query)
    if cached is not None:
        return cached[:max_results]

    mode = (os.getenv("SEARCH_PROVIDER") or "auto").lower()
    results: list[dict] = []
    provider = "ddg"

    if mode in ("auto", "brave") and os.getenv("BRAVE_SEARCH_API_KEY"):
        results = brave_search(query, max_results)
        provider = "brave"
    if not results and mode in ("auto", "ddg"):
        results = ddg_search(query, max_results)
        provider = "ddg"
    if not results and mode == "auto" and os.getenv("BRAVE_SEARCH_API_KEY"):
        results = brave_search(query, max_results)
        provider = "brave"

    if results:
        save_serp_cache(query, results, provider=provider)
    return results
