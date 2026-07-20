"""Actava.ai API client — platform agents + Cura inference.

Platform API (external agent runs):
  POST https://api.actava.ai/v1/agents/{agent_id}/run
  GET  https://api.actava.ai/v1/agents

Cura LLM (OpenAI-compatible):
  POST https://inference.actava.ai/v1/chat/completions
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx

from . import env  # noqa: F401

logger = logging.getLogger(__name__)

PLATFORM_BASE = os.getenv("ACTAVA_PLATFORM_URL", "https://api.actava.ai/v1").rstrip("/")
CURA_BASE = os.getenv("ACTAVA_CURA_URL", "https://inference.actava.ai/v1").rstrip("/")
CURA_MODEL = os.getenv("ACTAVA_CURA_MODEL", "actava/cura-soar")

_PLACEHOLDER_KEYS = frozenset({"your_key_here", "changeme", "xxx", "test", "sk_test"})
_SESSION_RUNNING = frozenset({"pending", "running", "queued", "in_progress"})
_SESSION_FAILED = frozenset({"failed", "error", "cancelled", "canceled", "timeout"})
_actava_status_cache: dict | None = None
_actava_status_at: float = 0.0


class ActavaError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _api_key() -> str:
    return os.getenv("ACTAVA_API_KEY", "").strip()


def _valid_key(key: str) -> bool:
    return bool(key) and key.lower() not in _PLACEHOLDER_KEYS


def actava_available() -> bool:
    return _valid_key(_api_key())


def actava_agent_configured() -> bool:
    return actava_available() and bool(os.getenv("ACTAVA_AGENT_ID", "").strip())


def _platform_headers() -> dict:
    key = _api_key()
    if not _valid_key(key):
        raise ActavaError("ACTAVA_API_KEY not set — create one under Administration → API Keys")
    return {"X-API-Key": key, "Content-Type": "application/json"}


def _request(method: str, url: str, **kwargs) -> dict | list:
    try:
        r = httpx.request(method, url, headers=_platform_headers(), timeout=120.0, **kwargs)
    except httpx.HTTPError as e:
        raise ActavaError(f"Actava request failed: {e}") from e

    if r.status_code == 401:
        raise ActavaError("Invalid ACTAVA_API_KEY (401)", 401)
    if r.status_code == 403:
        raise ActavaError("Actava access denied — check org permissions (403)", 403)
    if r.status_code == 404:
        raise ActavaError("Actava agent or endpoint not found (404)", 404)
    if r.status_code == 429:
        raise ActavaError("Actava rate limit exceeded (429)", 429)
    if r.status_code >= 400:
        detail = r.text[:400]
        raise ActavaError(f"Actava API error {r.status_code}: {detail}", r.status_code)

    if not r.content:
        return {}
    try:
        return r.json()
    except json.JSONDecodeError:
        return {"text": r.text}


def list_agents(*, include_non_live: bool = False) -> list[dict]:
    """List agents available to this API key."""
    data = _request(
        "GET",
        f"{PLATFORM_BASE}/agents",
        params={"include_non_live": str(include_non_live).lower()},
    )
    if isinstance(data, list):
        return data
    return data.get("agents") or data.get("data") or []


def get_session(session_id: str) -> dict:
    """Poll agent run status by session ID."""
    data = _request("GET", f"{PLATFORM_BASE}/sessions/{session_id}")
    return data if isinstance(data, dict) else {"data": data}


def _session_status(session: dict) -> str:
    return str(session.get("status") or session.get("state") or "").strip().lower()


def _session_done(status: str) -> bool:
    if not status:
        return False
    if status in _SESSION_RUNNING:
        return False
    return True


def _session_failed(session: dict) -> bool:
    status = _session_status(session)
    if status in _SESSION_FAILED:
        return True
    if session.get("error_code") or session.get("error_message"):
        return True
    return False


def wait_for_session(
    session_id: str,
    *,
    timeout: float | None = None,
    interval: float | None = None,
    on_progress=None,
) -> dict:
    """Poll GET /sessions/{id} until the run finishes or times out."""
    timeout_s = timeout if timeout is not None else float(os.getenv("ACTAVA_RUN_TIMEOUT", "1800"))
    interval_s = interval if interval is not None else float(os.getenv("ACTAVA_POLL_INTERVAL", "5"))
    heartbeat_s = float(os.getenv("ACTAVA_POLL_HEARTBEAT", "60"))
    deadline = time.monotonic() + timeout_s
    started = time.monotonic()
    last_status = ""
    last_heartbeat = started

    while time.monotonic() < deadline:
        session = get_session(session_id)
        status = _session_status(session)
        if status != last_status:
            last_status = status
            if on_progress and status:
                on_progress(f"Actava session {session_id[:8]}… — {status.upper()}")

        if on_progress and time.monotonic() - last_heartbeat >= heartbeat_s:
            elapsed = int(time.monotonic() - started)
            mins, secs = divmod(elapsed, 60)
            on_progress(
                f"Actava still running… ({mins}m {secs:02d}s elapsed — "
                f"research agents can take 10–20 min)"
            )
            last_heartbeat = time.monotonic()

        if _session_failed(session):
            msg = session.get("error_message") or session.get("error_code") or f"status={status}"
            raise ActavaError(f"Actava agent run failed: {msg}")

        if _session_done(status):
            output = session.get("output_text")
            if isinstance(output, str) and output.strip():
                return {"session_id": session_id, "status": status, "output_text": output.strip()}
            return session

        time.sleep(interval_s)

    raise ActavaError(
        f"Actava agent run timed out after {int(timeout_s)}s (session {session_id}). "
        "The session may still be running in Actava — check Agent Studio or increase ACTAVA_RUN_TIMEOUT."
    )


def run_agent(agent_id: str, message: str, *, extra: dict | None = None) -> dict:
    """Invoke an Actava external agent synchronously."""
    body: dict = {"message": message}
    if extra:
        body.update(extra)
    custom_path = os.getenv("ACTAVA_RUN_PATH", "").strip()
    if custom_path:
        url = f"{PLATFORM_BASE}/{custom_path.format(agent_id=agent_id)}"
    else:
        url = f"{PLATFORM_BASE}/agents/{agent_id}/run"
    data = _request("POST", url, json=body)
    if not isinstance(data, dict):
        return {"content": str(data)}
    return data


def run_agent_and_wait(
    agent_id: str,
    message: str,
    *,
    extra: dict | None = None,
    on_progress=None,
) -> dict:
    """POST /run and poll session until output is ready."""
    result = run_agent(agent_id, message, extra=extra)
    session_id = result.get("session_id")
    if not session_id:
        return result
    if on_progress:
        on_progress(f"Actava run started (session {session_id[:8]}…)")
    return wait_for_session(session_id, on_progress=on_progress)


def extract_response_text(payload: dict) -> str:
    """Pull assistant text from varied Actava / OpenAI response shapes."""
    for key in ("output_text", "output", "response", "content", "message", "text", "result", "final_output"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            nested = val.get("content") or val.get("text") or val.get("message")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()

    choices = payload.get("choices") or []
    if choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

    run = payload.get("run") or payload.get("data") or payload.get("session")
    if isinstance(run, dict):
        return extract_response_text(run)

    return json.dumps(payload) if payload else ""


def cura_chat(
    prompt: str,
    *,
    system: str | None = None,
    json_mode: bool = False,
    max_tokens: int = 2048,
) -> str:
    """Chat completion via Actava Cura inference API."""
    key = _api_key()
    if not _valid_key(key):
        raise ActavaError("ACTAVA_API_KEY not set")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict = {
        "model": CURA_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2 if json_mode else 0.4,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    try:
        r = httpx.post(
            f"{CURA_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=120.0,
        )
    except httpx.HTTPError as e:
        raise ActavaError(f"Cura inference failed: {e}") from e

    if r.status_code == 401:
        raise ActavaError("Invalid ACTAVA_API_KEY for Cura inference (401)", 401)
    if r.status_code >= 400:
        raise ActavaError(f"Cura error {r.status_code}: {r.text[:300]}", r.status_code)

    choices = r.json().get("choices") or []
    content = (choices[0].get("message", {}).get("content") or "") if choices else ""
    if not content.strip():
        raise ActavaError("Cura returned an empty response")
    return content.strip()


def cura_chat_json(prompt: str, *, system: str | None = None) -> dict:
    from .llm import _parse_json

    raw = cura_chat(prompt, system=system, json_mode=True)
    return _parse_json(raw)


def cura_status() -> dict:
    if not actava_available():
        return {"ok": False, "detail": "ACTAVA_API_KEY not set"}
    try:
        r = httpx.get(
            f"{CURA_BASE}/models",
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=10.0,
        )
        if r.status_code == 401:
            return {"ok": False, "detail": "Invalid ACTAVA_API_KEY"}
        r.raise_for_status()
        return {"ok": True, "model": CURA_MODEL, "detail": None}
    except httpx.HTTPError as e:
        return {"ok": False, "detail": str(e)}


def platform_status() -> dict:
    if not actava_available():
        return {"ok": False, "detail": "ACTAVA_API_KEY not set"}
    try:
        agents = list_agents()
        return {"ok": True, "agent_count": len(agents), "detail": None}
    except ActavaError as e:
        return {"ok": False, "detail": str(e)}


def _agent_info(agent_id: str) -> dict | None:
    try:
        for include in (False, True):
            for agent in list_agents(include_non_live=include):
                if agent.get("id") == agent_id:
                    return {
                        "agent_name": agent.get("agent_name"),
                        "is_live": agent.get("is_live"),
                        "agent_stage": agent.get("agent_stage"),
                    }
    except ActavaError:
        return None
    return None


def actava_status() -> dict:
    global _actava_status_cache, _actava_status_at
    ttl = float(os.getenv("ACTAVA_STATUS_CACHE_TTL", "120"))
    now = time.monotonic()
    if _actava_status_cache is not None and now - _actava_status_at < ttl:
        return _actava_status_cache

    agent_id = os.getenv("ACTAVA_AGENT_ID", "").strip()
    info = _agent_info(agent_id) if agent_id else None
    result = {
        "configured": actava_available(),
        "agent_id": agent_id or None,
        "agent_mode": bool(agent_id),
        "agent_name": (info or {}).get("agent_name"),
        "agent_is_live": (info or {}).get("is_live"),
        "agent_stage": (info or {}).get("agent_stage"),
        "cura": cura_status(),
        "platform": platform_status(),
        "mode": "agent" if agent_id else "discover+cura",
    }
    _actava_status_cache = result
    _actava_status_at = now
    return result
