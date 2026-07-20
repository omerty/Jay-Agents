"""Apollo.io REST API client.

People Search (`mixed_people/api_search`) is FREE — it consumes no credits —
but does not return email addresses. People Enrichment (`people/match`)
reveals emails at 1 credit per match.

Requires a master API key: app.apollo.io → Settings → API Keys.
"""

import logging
import os
import time

import httpx

from . import env  # noqa: F401 — load .env

logger = logging.getLogger(__name__)

BASE_URL = "https://api.apollo.io/api/v1"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


class ApolloError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ApolloClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("APOLLO_API_KEY", "")
        if not self.api_key:
            raise ApolloError(
                "APOLLO_API_KEY not set. Create a master API key at "
                "app.apollo.io → Settings → API Keys, then add to .env: APOLLO_API_KEY=..."
            )

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self.api_key,
        }

    def _post(self, path: str, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = httpx.post(
                    f"{BASE_URL}/{path}",
                    headers=self._headers(),
                    json=payload,
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                last_error = ApolloError(f"Apollo request failed: {e}")
                if attempt + 1 < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue

            if r.status_code == 429 or r.status_code >= 500:
                last_error = ApolloError(f"Apollo transient error ({r.status_code})", r.status_code)
                if attempt + 1 < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("Apollo %s — retrying in %.1fs", r.status_code, delay)
                    time.sleep(delay)
                continue

            if r.status_code == 401:
                raise ApolloError("Invalid APOLLO_API_KEY", 401)
            if r.status_code == 403:
                raise ApolloError(
                    "Apollo API key lacks access to this endpoint. "
                    "Make sure it is a MASTER key (Settings → API Keys → master key).",
                    403,
                )
            if r.status_code == 422:
                raise ApolloError(f"Apollo rejected the search filters: {r.text[:400]}", 422)
            if r.status_code != 200:
                raise ApolloError(f"Apollo request failed ({r.status_code}): {r.text[:400]}", r.status_code)

            return r.json()

        raise last_error or ApolloError("Apollo request failed")

    def search_people(
        self,
        *,
        person_titles: list[str] | None = None,
        person_seniorities: list[str] | None = None,
        person_locations: list[str] | None = None,
        organization_locations: list[str] | None = None,
        organization_num_employees_ranges: list[str] | None = None,
        q_keywords: str | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """
        People Search — free, no credits. Emails are NOT included in results.
        Returns {people: [...], pagination: {...}}.
        """
        payload: dict = {"page": page, "per_page": min(max(per_page, 1), 100)}
        if person_titles:
            payload["person_titles"] = person_titles
        if person_seniorities:
            payload["person_seniorities"] = person_seniorities
        if person_locations:
            payload["person_locations"] = person_locations
        if organization_locations:
            payload["organization_locations"] = organization_locations
        if organization_num_employees_ranges:
            payload["organization_num_employees_ranges"] = organization_num_employees_ranges
        if q_keywords:
            payload["q_keywords"] = q_keywords

        return self._post("mixed_people/api_search", payload)

    def enrich_person(self, person_id: str) -> dict | None:
        """
        People Enrichment — reveals work email. Costs 1 Apollo credit per match.
        Returns the enriched person dict or None if no match.
        """
        body = self._post("people/match", {
            "id": person_id,
            "reveal_personal_emails": False,
        })
        return body.get("person")
