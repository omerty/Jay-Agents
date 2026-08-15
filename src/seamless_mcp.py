"""Seamless.AI MCP client — search/research via JSON-RPC tools.

Docs: https://docs.seamless.ai/mcp-docs
Server: https://mcp.seamless.ai/mcp
Auth: Token header (MCP-scoped API key from Settings → Public API Connections)
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

from . import env  # noqa: F401
from .seamless_api import SeamlessError

logger = logging.getLogger(__name__)

MCP_URL = os.getenv("SEAMLESS_MCP_URL", "https://mcp.seamless.ai/mcp").rstrip("/")
POLL_INTERVAL = 3.0
POLL_MAX_WAIT = 90.0


class SeamlessMcpClient:
    """Call Seamless MCP tools (same pipeline as REST, different transport)."""

    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or os.getenv("SEAMLESS_API_KEY", "")).strip()
        if not self.api_key:
            raise SeamlessError(
                "SEAMLESS_API_KEY not set. Create an MCP-scoped key at "
                "https://login.seamless.ai/settings/public-api (check MCP scope)."
            )
        self.last_credits: int | None = None
        self._rpc_id = 0

    def _headers(self) -> dict:
        # Seamless MCP requires both Accept types (HTTP 406 otherwise)
        return {
            "Token": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _parse_tool_payload(self, result: dict) -> dict | list:
        if result.get("structuredContent") is not None:
            sc = result["structuredContent"]
            return sc if isinstance(sc, (dict, list)) else {"data": sc}

        texts = [
            block.get("text", "")
            for block in (result.get("content") or [])
            if block.get("type") == "text" and block.get("text")
        ]
        if not texts:
            return {}

        raw = texts[-1].strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}

        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"data": parsed}
        return {"data": parsed}

    def _call_tool(self, name: str, arguments: dict | None = None, *, timeout: float = 120.0) -> dict | list:
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
            "id": self._next_id(),
        }
        try:
            r = httpx.post(MCP_URL, headers=self._headers(), json=body, timeout=timeout)
        except httpx.HTTPError as e:
            raise SeamlessError(f"Seamless MCP request failed: {e}") from e

        if r.status_code == 401:
            raise SeamlessError("Invalid SEAMLESS_API_KEY for MCP (401)", 401)
        if r.status_code == 429:
            raise SeamlessError("Seamless MCP rate limit exceeded (429)", 429)
        if r.status_code >= 400:
            raise SeamlessError(f"Seamless MCP HTTP {r.status_code}: {r.text[:300]}", r.status_code)

        try:
            envelope = r.json()
        except json.JSONDecodeError as e:
            raise SeamlessError(f"Seamless MCP returned invalid JSON: {r.text[:200]}") from e

        if envelope.get("error"):
            err = envelope["error"]
            msg = err.get("message") or str(err)
            if "not enabled" in msg.lower():
                raise SeamlessError(
                    "MCP access not enabled on your Seamless account — contact your admin or Seamless support."
                )
            raise SeamlessError(f"Seamless MCP error: {msg}")

        result = envelope.get("result") or {}
        if result.get("isError"):
            detail = self._parse_tool_payload(result)
            msg = detail.get("msg") or detail.get("message") or detail.get("text") or str(detail)
            if "insufficient" in str(msg).lower():
                raise SeamlessError("Seamless credits exhausted", 422)
            raise SeamlessError(f"Seamless MCP tool failed: {msg}")

        payload = self._parse_tool_payload(result)
        self._sync_credits_from_payload(payload)
        return payload

    def _sync_credits_from_payload(self, payload: dict | list) -> None:
        if not isinstance(payload, dict):
            return
        for key in ("credits", "creditsRemaining", "remainingCredits", "creditBalance"):
            val = payload.get(key)
            if isinstance(val, int):
                self.last_credits = val
                return
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("credits", "creditsRemaining", "remainingCredits"):
                val = data.get(key)
                if isinstance(val, int):
                    self.last_credits = val
                    return

    def refresh_credits(self) -> int | None:
        payload = self._call_tool("get_credits", {}, timeout=30.0)
        if isinstance(payload, dict):
            for key in ("credits", "creditsRemaining", "remainingCredits", "balance"):
                val = payload.get(key)
                if isinstance(val, int):
                    self.last_credits = val
                    return val
            data = payload.get("data")
            if isinstance(data, dict):
                val = data.get("credits") or data.get("creditsRemaining")
                if isinstance(val, int):
                    self.last_credits = val
                    return val
        return self.last_credits

    def search_contacts(self, filters: dict, *, limit: int = 50, next_token: str | None = None) -> dict:
        args = {k: v for k, v in filters.items() if k != "limit"}
        args["limit"] = min(limit, 50)
        if next_token:
            args["nextToken"] = next_token
        payload = self._call_tool("search_contacts", args, timeout=60.0)
        return self._normalize_search_payload(payload)

    def search_companies(self, filters: dict, *, limit: int = 50, next_token: str | None = None) -> dict:
        args = {k: v for k, v in filters.items() if k != "limit"}
        args["limit"] = min(limit, 50)
        if next_token:
            args["nextToken"] = next_token
        payload = self._call_tool("search_companies", args, timeout=60.0)
        return self._normalize_search_payload(payload)

    @staticmethod
    def _normalize_search_payload(payload: dict | list) -> dict:
        """Map MCP `results` shape onto REST-compatible `{data: [...]}`."""
        if isinstance(payload, list):
            return {"data": payload}
        if not isinstance(payload, dict):
            return {"data": []}
        if payload.get("data") is not None:
            return payload
        if payload.get("results") is not None:
            out = dict(payload)
            out["data"] = out.pop("results")
            # Map pagination next token if present
            pag = out.get("pagination") or {}
            if isinstance(pag, dict) and pag.get("nextToken") and "nextToken" not in out:
                out["nextToken"] = pag["nextToken"]
            return out
        return payload

    def research_contacts(self, search_result_ids: list[str]) -> list[str]:
        if not search_result_ids:
            return []
        payload = self._call_tool(
            "research_contacts",
            {"searchResultIds": search_result_ids[:100], "waitForResults": False},
            timeout=60.0,
        )
        if not isinstance(payload, dict):
            return []
        ids = payload.get("requestIds") or payload.get("request_ids")
        if isinstance(ids, list):
            return ids
        data = payload.get("data")
        if isinstance(data, dict):
            ids = data.get("requestIds") or data.get("request_ids")
            if isinstance(ids, list):
                return ids
        return []

    def poll_contact_research(self, request_ids: list[str]) -> list[dict]:
        if not request_ids:
            return []
        payload = self._call_tool(
            "poll_contact_research",
            {"requestIds": request_ids},
            timeout=60.0,
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("data") or payload.get("results") or []
        return []

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

    def research_and_wait_contacts(self, search_result_ids: list[str]) -> list[dict]:
        """MCP shortcut — research with waitForResults=true."""
        if not search_result_ids:
            return []
        payload = self._call_tool(
            "research_contacts",
            {"searchResultIds": search_result_ids[:100], "waitForResults": True},
            timeout=POLL_MAX_WAIT + 30,
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                inner = data.get("data") or data.get("results")
                if isinstance(inner, list):
                    return inner
            results = payload.get("results")
            if isinstance(results, list):
                return results
        return self.wait_for_contact_research(self.research_contacts(search_result_ids))
