"""Hunter.io API client — email finder (free tier: 25 searches/month)."""

import logging
import os

import httpx

from . import env  # noqa: F401 — load .env

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hunter.io/v2"


class HunterError(Exception):
    pass


def hunter_available() -> bool:
    return bool(os.getenv("HUNTER_API_KEY"))


def find_email(
    first_name: str,
    last_name: str,
    *,
    domain: str | None = None,
    company: str | None = None,
    min_score: int = 70,
) -> dict | None:
    """
    Find the most likely work email for a person at a company/domain.
    Returns {email, score, verified} or None. Costs 1 Hunter search credit.
    """
    api_key = os.getenv("HUNTER_API_KEY", "")
    if not api_key or not (first_name and last_name) or not (domain or company):
        return None

    params = {"first_name": first_name, "last_name": last_name, "api_key": api_key}
    if domain:
        params["domain"] = domain
    else:
        params["company"] = company

    try:
        r = httpx.get(f"{BASE_URL}/email-finder", params=params, timeout=15.0)
    except httpx.HTTPError as e:
        logger.warning("Hunter request failed: %s", e)
        return None

    if r.status_code == 401:
        logger.warning("Invalid HUNTER_API_KEY")
        return None
    if r.status_code == 429:
        logger.warning("Hunter quota exhausted (free tier = 25 searches/month)")
        return None
    if r.status_code != 200:
        logger.warning("Hunter email-finder returned %s", r.status_code)
        return None

    data = (r.json() or {}).get("data") or {}
    email = data.get("email")
    score = data.get("score") or 0
    if not email or score < min_score:
        return None
    return {
        "email": email,
        "score": score,
        "verified": (data.get("verification") or {}).get("status") == "valid",
    }
