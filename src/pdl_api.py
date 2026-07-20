"""People Data Labs REST API client — person search."""

import logging
import os
import time

import httpx

from . import env  # noqa: F401 — load .env

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.peopledatalabs.com/v5/person/search"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # seconds; doubles per attempt


class PDLError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PDLClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("PDL_API_KEY", "")
        if not self.api_key:
            raise PDLError(
                "PDL_API_KEY not set. Sign up at https://www.peopledatalabs.com (100 free credits/month).\n"
                "Add to .env: PDL_API_KEY=your_key_here"
            )

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }

    def search_people(self, query: dict, *, size: int = 25, scroll_token: str | None = None) -> dict:
        """
        Person Search API — 1 credit per record returned.
        Returns {data: [...], total: N, scroll_token: ...}.
        Retries transient failures (429/5xx/network) with backoff.
        """
        payload: dict = {"query": query, "size": min(max(size, 1), 100)}
        if scroll_token:
            payload["scroll_token"] = scroll_token

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = httpx.post(
                    SEARCH_URL,
                    headers=self._headers(),
                    json=payload,
                    timeout=30.0,
                )
            except httpx.HTTPError as e:
                last_error = PDLError(f"PDL request failed: {e}")
                if attempt + 1 < MAX_RETRIES:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue

            if r.status_code in (429,) or r.status_code >= 500:
                last_error = PDLError(f"PDL transient error ({r.status_code})", r.status_code)
                if attempt + 1 < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("PDL %s — retrying in %.1fs", r.status_code, delay)
                    time.sleep(delay)
                continue

            return self._handle_response(r)

        raise last_error or PDLError("PDL request failed")

    @staticmethod
    def _handle_response(r: httpx.Response) -> dict:
        if r.status_code == 401:
            raise PDLError("Invalid PDL_API_KEY", 401)
        if r.status_code == 402:
            raise PDLError(
                "PDL credits exhausted (402). Free tier = 100 credits/month. "
                "Wait for reset or upgrade at peopledatalabs.com.",
                402,
            )
        if r.status_code == 404:
            # PDL returns 404 when the query matches zero records — not an error
            return {"data": [], "total": 0}
        if r.status_code != 200:
            raise PDLError(f"PDL search failed ({r.status_code}): {r.text[:400]}", r.status_code)

        body = r.json()
        if body.get("error"):
            raise PDLError(f"PDL error: {body['error']}")

        return body
