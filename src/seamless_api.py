"""Seamless.AI REST API client — search (free) + research (credits).

Docs: https://docs.seamless.ai/introduction
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from . import env  # noqa: F401

logger = logging.getLogger(__name__)

BASE_URL = "https://api.seamless.ai/api/client/v1"
POLL_INTERVAL = 3.0
POLL_MAX_WAIT = 90.0


class SeamlessError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SeamlessClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or os.getenv("SEAMLESS_API_KEY", "")).strip()
        if not self.api_key:
            raise SeamlessError(
                "SEAMLESS_API_KEY not set. Create one at "
                "https://login.seamless.ai/settings/public-api"
            )
        self.last_credits: int | None = None

    def _headers(self) -> dict:
        return {"Token": self.api_key, "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = httpx.request(
                method,
                f"{BASE_URL}{path}",
                headers=self._headers(),
                timeout=30.0,
                **kwargs,
            )
        except httpx.HTTPError as e:
            raise SeamlessError(f"Seamless request failed: {e}") from e

        credits_hdr = r.headers.get("X-PublicAPI-Credits")
        if credits_hdr is not None:
            try:
                self.last_credits = int(credits_hdr)
            except ValueError:
                pass

        if r.status_code == 401:
            raise SeamlessError("Invalid SEAMLESS_API_KEY", 401)
        if r.status_code == 429:
            raise SeamlessError("Seamless rate limit exceeded — retry later", 429)
        if r.status_code == 422:
            body = r.json() if r.content else {}
            code = (body or {}).get("code", "")
            if code == "insufficientCredits":
                raise SeamlessError("Seamless credits exhausted", 422)
            raise SeamlessError(f"Seamless rejected request: {body.get('msg', r.text)}", 422)
        if r.status_code >= 400:
            detail = r.text[:300]
            raise SeamlessError(f"Seamless API error {r.status_code}: {detail}", r.status_code)

        if not r.content:
            return {}
        return r.json()

    def search_contacts(self, filters: dict, *, limit: int = 50, next_token: str | None = None) -> dict:
        body = {**filters, "limit": min(limit, 50)}
        if next_token:
            body["nextToken"] = next_token
        return self._request("POST", "/search/contacts", json=body)

    def search_companies(self, filters: dict, *, limit: int = 50, next_token: str | None = None) -> dict:
        body = {**filters, "limit": min(limit, 50)}
        if next_token:
            body["nextToken"] = next_token
        return self._request("POST", "/search/companies", json=body)

    def research_contacts(self, search_result_ids: list[str]) -> list[str]:
        if not search_result_ids:
            return []
        data = self._request(
            "POST",
            "/contacts/research",
            json={"searchResultIds": search_result_ids[:100]},
        )
        return data.get("requestIds") or []

    def research_companies(self, search_result_ids: list[str]) -> list[str]:
        if not search_result_ids:
            return []
        data = self._request(
            "POST",
            "/companies/research",
            json={"searchResultIds": search_result_ids[:100]},
        )
        return data.get("requestIds") or []

    def poll_contact_research(self, request_ids: list[str]) -> list[dict]:
        if not request_ids:
            return []
        data = self._request(
            "GET",
            "/contacts/research/poll",
            params={"requestIds": ",".join(request_ids)},
        )
        return data.get("data") or []

    def poll_company_research(self, request_ids: list[str]) -> list[dict]:
        if not request_ids:
            return []
        data = self._request(
            "GET",
            "/companies/research/poll",
            params={"requestIds": ",".join(request_ids)},
        )
        return data.get("data") or []

    def wait_for_contact_research(self, request_ids: list[str]) -> list[dict]:
        """Poll until all requests are terminal or timeout."""
        pending = set(request_ids)
        results: dict[str, dict] = {}
        deadline = time.monotonic() + POLL_MAX_WAIT
        while pending and time.monotonic() < deadline:
            for item in self.poll_contact_research(list(pending)):
                rid = item.get("requestId")
                status = item.get("status")
                if not rid:
                    continue
                if status in ("done", "error", "missing"):
                    pending.discard(rid)
                    results[rid] = item
                elif status == "researching":
                    results[rid] = item
            if pending:
                time.sleep(POLL_INTERVAL)
        return list(results.values())


def seamless_transport() -> str:
    """rest | mcp — MCP uses JSON-RPC at mcp.seamless.ai (MCP-scoped API key)."""
    return os.getenv("SEAMLESS_TRANSPORT", "rest").strip().lower()


def seamless_available() -> bool:
    key = os.getenv("SEAMLESS_API_KEY", "").strip().lower()
    return bool(key) and key not in ("your_key_here", "changeme", "xxx", "test", "sk_test")


def get_seamless_client():
    """Return REST or MCP client based on SEAMLESS_TRANSPORT."""
    if seamless_transport() == "mcp":
        from .seamless_mcp import SeamlessMcpClient

        return SeamlessMcpClient()
    return SeamlessClient()
