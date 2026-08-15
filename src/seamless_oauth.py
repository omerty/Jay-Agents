"""Seamless.ai OAuth — use your web account credits via Bearer token (not HTML scraping).

Register OAuth at https://login.seamless.ai/settings/public-api → OAuth Connections.
Set SEAMLESS_CLIENT_ID + SEAMLESS_CLIENT_SECRET in .env (operator, once).

End users click Connect Seamless → sign in → pipeline uses their plan/credits.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx

from . import env  # noqa: F401

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
TOKEN_PATH = Path(os.getenv("SEAMLESS_TOKEN_PATH", ROOT / ".seamless_token.json"))
ACCOUNT_PATH = Path(os.getenv("SEAMLESS_ACCOUNT_PATH", ROOT / ".seamless_account.json"))
AUTH_BASE = "https://login.seamless.ai/oauth/authorize"
API_BASE = os.getenv("SEAMLESS_PLATFORM_URL", "https://api.seamless.ai/api/client/v1").rstrip("/")

_oauth_pending: dict[str, dict] = {}
_status_cache: dict = {"at": 0.0, "value": None}
_STATUS_CACHE_TTL = 60


class SeamlessOAuthError(Exception):
    pass


def _client_id() -> str:
    return os.getenv("SEAMLESS_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("SEAMLESS_CLIENT_SECRET", "").strip()


def oauth_client_ready() -> bool:
    return bool(_client_id() and _client_secret())


def oauth_redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/seamless/oauth/callback"


def _invalidate_status_cache():
    _status_cache["value"] = None


def _load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text())
    except Exception:
        return None


def _save_token(data: dict):
    TOKEN_PATH.write_text(json.dumps(data, indent=2))


def _save_account(extra: dict):
    ACCOUNT_PATH.write_text(json.dumps(extra, indent=2))


def _read_account() -> dict:
    if not ACCOUNT_PATH.exists():
        return {}
    try:
        return json.loads(ACCOUNT_PATH.read_text())
    except Exception:
        return {}


def oauth_connected() -> bool:
    tok = _load_token()
    return bool(tok and (tok.get("access_token") or tok.get("refresh_token")))


def oauth_start(redirect_uri: str) -> tuple[str, str]:
    if not oauth_client_ready():
        raise SeamlessOAuthError(
            "Seamless OAuth not configured — set SEAMLESS_CLIENT_ID and SEAMLESS_CLIENT_SECRET in .env"
        )
    state = secrets.token_urlsafe(24)
    _oauth_pending[state] = {"redirect_uri": redirect_uri}
    from urllib.parse import quote

    url = (
        f"{AUTH_BASE}?client_id={quote(_client_id())}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&state={quote(state)}"
    )
    return url, state


def _exchange_token(body: dict) -> dict:
    try:
        r = httpx.post(f"{API_BASE}/oauth/accessToken", json=body, timeout=30.0)
    except httpx.HTTPError as e:
        raise SeamlessOAuthError(f"Seamless token exchange failed: {e}") from e
    if r.status_code >= 400:
        raise SeamlessOAuthError(f"Seamless token error ({r.status_code}): {r.text[:300]}")
    data = r.json()
    if not data.get("access_token"):
        raise SeamlessOAuthError("Seamless did not return an access_token")
    expires_at = data.get("expires_at")
    if expires_at:
        data["expires_at"] = int(expires_at)
    else:
        data["expires_at"] = int(time.time()) + int(data.get("expires_in") or 3600)
    return data


def oauth_complete(state: str, code: str) -> dict:
    pending = _oauth_pending.pop(state, None)
    if not pending:
        raise SeamlessOAuthError("OAuth session expired — click Connect Seamless again")
    redirect_uri = pending["redirect_uri"]
    token = _exchange_token({
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code": code,
    })
    token["redirect_uri"] = redirect_uri
    _save_token(token)
    _save_account({"connected_at": time.time(), "auth": "oauth"})
    _invalidate_status_cache()
    return token


def _refresh_access_token(refresh_token: str, redirect_uri: str | None = None) -> dict:
    body = {
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if redirect_uri:
        body["redirect_uri"] = redirect_uri
    return _exchange_token(body)


def get_valid_access_token() -> str | None:
    """Return a valid OAuth access token, refreshing if needed."""
    if not oauth_client_ready():
        return None
    tok = _load_token()
    if not tok:
        return None
    access = tok.get("access_token")
    expires_at = int(tok.get("expires_at") or 0)
    if access and expires_at > int(time.time()) + 60:
        return access
    refresh = tok.get("refresh_token")
    if not refresh:
        return access
    try:
        redirect = tok.get("redirect_uri")
        new_tok = _refresh_access_token(refresh, redirect)
        merged = {**tok, **new_tok}
        _save_token(merged)
        return merged.get("access_token")
    except SeamlessOAuthError as e:
        logger.warning("Seamless token refresh failed: %s", e)
        return None


def disconnect():
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
    if ACCOUNT_PATH.exists():
        ACCOUNT_PATH.unlink()
    _oauth_pending.clear()
    _invalidate_status_cache()


def seamless_auth_status() -> dict:
    now = time.time()
    if _status_cache["value"] is not None and now - _status_cache["at"] < _STATUS_CACHE_TTL:
        return _status_cache["value"]

    if not oauth_client_ready():
        result = {
            "connected": False,
            "auth_mode": None,
            "detail": "Seamless OAuth not configured on server (SEAMLESS_CLIENT_ID/SECRET).",
            "needs_operator_setup": True,
            "can_connect": False,
        }
    elif not oauth_connected():
        result = {
            "connected": False,
            "auth_mode": "oauth",
            "detail": "Click Connect Seamless to sign in with your Seamless account.",
            "needs_operator_setup": False,
            "can_connect": True,
        }
    else:
        token_ok = bool(get_valid_access_token())
        result = {
            "connected": token_ok,
            "auth_mode": "oauth",
            "detail": None if token_ok else "Seamless session expired — reconnect.",
            "needs_operator_setup": False,
            "can_connect": not token_ok,
        }

    _status_cache["value"] = result
    _status_cache["at"] = now
    return result
