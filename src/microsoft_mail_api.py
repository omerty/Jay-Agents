"""Microsoft 365 / Outlook mail — drafts, confirmed sending, and reply scanning.

Mirrors gmail_api.py so Gmail and Outlook can both be connected.

Safety model:
  - The agent only CREATES drafts automatically. Nothing is ever sent
    without an explicit confirmation from the dashboard.
  - Reply scanning is read-only: it looks at conversations we sent and flags
    leads that answered, creating a dashboard notification.

Setup — two roles:

  OPERATOR (you, once per deployment):
    Register an Azure AD app and set MICROSOFT_CLIENT_ID + MICROSOFT_CLIENT_SECRET
    (and optionally MICROSOFT_TENANT_ID) in server env. End users never see these.

  END USER (each person using the dashboard):
    Click "Connect Microsoft Email" → sign in with Microsoft → done.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

import httpx

from . import env  # noqa: F401 — load .env
from .gmail_api import strip_subject_line

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Delegated scopes — Mail.ReadWrite covers drafts; Mail.Send for confirmed send.
SCOPES = [
    "openid",
    "profile",
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
]

ROOT = Path(__file__).parent.parent
TOKEN_PATH = Path(os.getenv("MICROSOFT_TOKEN_PATH", ROOT / ".microsoft_token.json"))
ACCOUNT_PATH = Path(os.getenv("MICROSOFT_ACCOUNT_PATH", ROOT / ".microsoft_account.json"))


class MicrosoftMailError(Exception):
    pass


def _tenant() -> str:
    return (os.getenv("MICROSOFT_TENANT_ID") or "common").strip() or "common"


def _client_id() -> str:
    return os.getenv("MICROSOFT_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()


def oauth_client_ready() -> bool:
    return bool(_client_id() and _client_secret())


def _status_not_configured() -> dict:
    return {
        "connected": False,
        "email": None,
        "detail": "Microsoft Email isn't set up on this server yet — ask your administrator.",
        "needs_operator_setup": True,
        "needs_credentials": True,
        "can_connect": False,
    }


def _authority() -> str:
    return f"https://login.microsoftonline.com/{_tenant()}"


def oauth_redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/microsoft/oauth/callback"


_status_cache: dict = {"at": 0.0, "value": None}
_STATUS_CACHE_TTL = 60

# state → {redirect_uri, code_verifier} for PKCE-less confidential client (secret)
_oauth_pending: dict[str, dict] = {}


def _invalidate_status_cache():
    _status_cache["value"] = None


def _save_account_email(email: str | None) -> None:
    if not email:
        return
    ACCOUNT_PATH.write_text(json.dumps({"email": email}))


def _read_account_email() -> str | None:
    if not ACCOUNT_PATH.exists():
        return None
    try:
        data = json.loads(ACCOUNT_PATH.read_text())
        email = (data.get("email") or "").strip()
        return email or None
    except Exception:
        return None


def _save_token(token: dict) -> None:
    expires_in = token.get("expires_in")
    if expires_in and "expires_at" not in token:
        import time

        try:
            token = {**token, "expires_at": time.time() + float(expires_in) - 60}
        except (TypeError, ValueError):
            pass
    TOKEN_PATH.write_text(json.dumps(token))
    _invalidate_status_cache()


def _load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_PATH.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _msal_app():
    try:
        from msal import ConfidentialClientApplication
    except ImportError as e:
        raise MicrosoftMailError(
            "MSAL library not installed. Run: pip install -r requirements.txt"
        ) from e

    if not oauth_client_ready():
        raise MicrosoftMailError(
            "Microsoft OAuth is not configured on this server. "
            "The operator must set MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET."
        )
    return ConfidentialClientApplication(
        _client_id(),
        authority=_authority(),
        client_credential=_client_secret(),
    )


def oauth_start(redirect_uri: str) -> tuple[str, str]:
    """Begin browser OAuth. Returns (authorization_url, state)."""
    app = _msal_app()
    state = secrets.token_urlsafe(24)
    auth_url = app.get_authorization_request_url(
        scopes=SCOPES,
        state=state,
        redirect_uri=redirect_uri,
        prompt="select_account",
    )
    _oauth_pending[state] = {"redirect_uri": redirect_uri}
    return auth_url, state


def oauth_complete(state: str, code: str) -> dict:
    """Finish browser OAuth after Microsoft redirects back with an auth code."""
    pending = _oauth_pending.pop(state, None)
    if not pending:
        raise MicrosoftMailError("OAuth session expired — click Connect Microsoft Email again")

    app = _msal_app()
    result = app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=pending["redirect_uri"],
    )
    if "access_token" not in result:
        err = result.get("error_description") or result.get("error") or "token exchange failed"
        raise MicrosoftMailError(f"Microsoft sign-in failed: {err}")

    _save_token({
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token"),
        "expires_in": result.get("expires_in"),
        "id_token": result.get("id_token"),
        "token_type": result.get("token_type", "Bearer"),
        "scope": result.get("scope"),
    })
    return microsoft_status(force=True)


def disconnect_microsoft() -> dict:
    """Remove stored OAuth tokens so the user can sign in with a different account."""
    removed = []
    for path in (TOKEN_PATH, ACCOUNT_PATH):
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError as e:
            logger.warning("Could not remove %s: %s", path, e)
            raise MicrosoftMailError(f"Could not disconnect Microsoft Email ({path.name})") from e
    _oauth_pending.clear()
    _invalidate_status_cache()
    status = microsoft_status(force=True)
    status["disconnected"] = True
    status["removed"] = removed
    return status


def _token_still_valid(stored: dict) -> bool:
    import time

    expires_at = stored.get("expires_at")
    if not expires_at or not stored.get("access_token"):
        return False
    try:
        return time.time() < float(expires_at)
    except (TypeError, ValueError):
        return False


def _acquire_token() -> str | None:
    """Return a valid access token, refreshing if needed. Never opens a browser."""
    stored = _load_token()
    if not stored:
        return None

    if _token_still_valid(stored):
        return stored["access_token"]

    app = _msal_app()
    refresh = stored.get("refresh_token")
    if refresh:
        result = app.acquire_token_by_refresh_token(refresh, scopes=SCOPES)
        if "access_token" in result:
            _save_token({
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token") or refresh,
                "expires_in": result.get("expires_in"),
                "id_token": result.get("id_token"),
                "token_type": result.get("token_type", "Bearer"),
                "scope": result.get("scope"),
            })
            return result["access_token"]
        logger.warning(
            "Microsoft token refresh failed: %s",
            result.get("error_description") or result.get("error"),
        )

    return stored.get("access_token") or None


def _graph_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _graph_request(method: str, path: str, *, json_body: dict | None = None) -> dict | None:
    token = _acquire_token()
    if not token:
        raise MicrosoftMailError(
            "Microsoft Email not connected. Sign in from the dashboard first."
        )
    url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.request(method, url, headers=_graph_headers(token), json=json_body)
    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        detail = _graph_error_detail(resp)
        raise MicrosoftMailError(detail)
    if not resp.content:
        return None
    return resp.json()


def _graph_error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = err.get("message") or data.get("message") or resp.text
    except Exception:
        msg = resp.text or f"HTTP {resp.status_code}"
    short = msg if len(msg) <= 220 else msg[:217] + "..."
    code = ""
    try:
        code = (resp.json().get("error") or {}).get("code") or ""
    except Exception:
        pass
    if code in ("MailboxNotEnabledForRESTAPI", "ResourceNotFound") or "MailboxNotEnabled" in short:
        return (
            "Signed in with Microsoft, but this account has no Exchange Online mailbox. "
            "Use a Microsoft 365 / Outlook mailbox, then reconnect."
        )
    if resp.status_code in (401, 403):
        return f"Microsoft Graph permission error: {short}"
    return f"Microsoft Graph error ({resp.status_code}): {short}"


def microsoft_status(*, force: bool = False) -> dict:
    """Health info: {connected, email, detail}. Cached for 60s."""
    import time

    if not force and _status_cache["value"] is not None:
        if time.monotonic() - _status_cache["at"] < _STATUS_CACHE_TTL:
            return _status_cache["value"]
    value = _microsoft_status_uncached()
    _status_cache["at"] = time.monotonic()
    _status_cache["value"] = value
    return value


def _microsoft_status_uncached() -> dict:
    if not oauth_client_ready():
        return _status_not_configured()
    try:
        token = _acquire_token()
    except MicrosoftMailError as e:
        return {
            "connected": False,
            "email": None,
            "detail": str(e),
            "needs_operator_setup": False,
            "needs_credentials": False,
            "can_connect": False,
        }
    if not token:
        return {
            "connected": False,
            "email": None,
            "detail": "Click Connect Microsoft Email to sign in with your Microsoft account.",
            "needs_operator_setup": False,
            "needs_credentials": False,
            "can_connect": True,
        }
    try:
        me = _graph_request("GET", "/me?$select=mail,userPrincipalName,displayName")
        email = (me or {}).get("mail") or (me or {}).get("userPrincipalName")
        _save_account_email(email)
        return {
            "connected": True,
            "authenticated": True,
            "has_token": True,
            "email": email,
            "detail": None,
            "needs_api_enable": False,
            "needs_operator_setup": False,
            "needs_credentials": False,
            "can_connect": False,
        }
    except MicrosoftMailError as e:
        return {
            "connected": False,
            "authenticated": True,
            "has_token": True,
            "email": _read_account_email(),
            "detail": str(e),
            "needs_api_enable": False,
            "needs_operator_setup": False,
            "needs_credentials": False,
            "can_connect": True,
        }


def _connected_sender() -> str | None:
    status = microsoft_status()
    if status.get("connected") and status.get("email"):
        return status["email"]
    return None


def outlook_draft_url(message_id: str, web_link: str | None = None) -> str:
    """Deep link to open a draft in Outlook on the web."""
    if web_link:
        return web_link
    # ItemID must be URL-encoded; Graph ids often contain +/=
    return f"https://outlook.office.com/mail/deeplink/read/{message_id}"


def create_draft(to: str, subject: str, body: str) -> dict:
    """Create an Outlook draft (NOT sent). Returns draft/message/conversation ids + URL."""
    if not to or "@" not in to:
        raise MicrosoftMailError(f"Invalid recipient email: {to!r}")
    sender = _connected_sender()
    if not sender:
        raise MicrosoftMailError("Microsoft Email not connected — sign in from the dashboard first")

    payload = {
        "subject": subject,
        "body": {
            "contentType": "Text",
            "content": strip_subject_line(body),
        },
        "toRecipients": [
            {"emailAddress": {"address": to}},
        ],
    }
    draft = _graph_request("POST", "/me/messages", json_body=payload) or {}
    draft_id = draft.get("id")
    if not draft_id:
        raise MicrosoftMailError("Microsoft Graph did not return a draft id")
    web_link = draft.get("webLink")
    return {
        "draft_id": draft_id,
        "message_id": draft_id,
        "thread_id": draft.get("conversationId"),
        "sender": sender,
        "outlook_url": outlook_draft_url(draft_id, web_link),
        "provider": "microsoft",
    }


def send_draft(draft_id: str) -> dict:
    """Send an existing draft — only via explicit dashboard confirm (web.py).

    Must never be imported by pipeline / sequences / daily modules.
    """
    _graph_request("POST", f"/me/messages/{draft_id}/send")
    # After send, message moves out of drafts; re-fetch by id may 404.
    # Return the draft_id as message_id; conversation id must already be on the lead.
    return {"message_id": draft_id, "thread_id": None}


def send_message(to: str, subject: str, body: str) -> dict:
    """Compose and send directly — only via explicit dashboard confirm (web.py).

    Must never be imported by pipeline / sequences / daily modules.
    """
    if not to or "@" not in to:
        raise MicrosoftMailError(f"Invalid recipient email: {to!r}")
    sender = _connected_sender()
    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "Text",
                "content": strip_subject_line(body),
            },
            "toRecipients": [
                {"emailAddress": {"address": to}},
            ],
        },
        "saveToSentItems": True,
    }
    _graph_request("POST", "/me/sendMail", json_body=payload)
    return {"message_id": None, "thread_id": None, "sender": sender}


def conversation_has_reply(messages: list[dict], our_email: str, our_message_id: str | None = None) -> bool:
    """True if the conversation contains a message NOT sent by us."""
    ours = (our_email or "").lower()
    for msg in messages:
        if our_message_id and msg.get("id") == our_message_id:
            continue
        from_addr = (
            ((msg.get("from") or {}).get("emailAddress") or {}).get("address") or ""
        ).lower()
        if from_addr and from_addr != ours:
            return True
    return False


def fetch_reply_body(conversation_id: str, our_email: str, our_message_id: str | None = None) -> str:
    """Fetch the latest inbound reply body from an Outlook conversation."""
    safe_id = conversation_id.replace("'", "''")
    try:
        data = _graph_request(
            "GET",
            f"/me/messages?$select=id,from,conversationId,body,bodyPreview"
            f"&$filter=conversationId eq '{safe_id}'"
            f"&$orderby=receivedDateTime desc"
            f"&$top=20",
        ) or {}
    except Exception as e:
        logger.warning("Could not fetch conversation body %s: %s", conversation_id, e)
        return ""

    ours = (our_email or "").lower()
    for msg in data.get("value") or []:
        if our_message_id and msg.get("id") == our_message_id:
            continue
        from_addr = (
            ((msg.get("from") or {}).get("emailAddress") or {}).get("address") or ""
        ).lower()
        if not from_addr or from_addr == ours:
            continue
        body = msg.get("body") or {}
        content = (body.get("content") or "").strip()
        if body.get("contentType") == "html" and content:
            import re
            content = re.sub(r"<[^>]+>", " ", content)
        if not content:
            content = (msg.get("bodyPreview") or "").strip()
        if content:
            return content
    return ""


def scan_replies() -> dict:
    """
    Check Outlook conversations for replies to outreach we sent.
    Marks leads 'replied' and creates a notification for each new reply.
    Returns {checked, replies, notified: [lead descriptions]}.
    """
    from .db import (
        create_notification,
        get_connection,
        init_db,
        set_lead_fields,
    )

    status = microsoft_status()
    if not status["connected"]:
        raise MicrosoftMailError(status["detail"] or "Microsoft Email not connected")
    our_email = status["email"]

    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, agent, company, contact_name, gmail_thread_id, gmail_message_id "
            "FROM leads WHERE status = 'emailed' AND gmail_thread_id IS NOT NULL "
            "AND COALESCE(mail_provider, 'gmail') = 'microsoft'"
        ).fetchall()

    checked = 0
    notified: list[str] = []

    for row in rows:
        checked += 1
        conv_id = row["gmail_thread_id"]
        try:
            # Filter conversationId — escape single quotes for OData
            safe_id = conv_id.replace("'", "''")
            data = _graph_request(
                "GET",
                f"/me/messages?$select=id,from,conversationId"
                f"&$filter=conversationId eq '{safe_id}'"
                f"&$top=50",
            ) or {}
            messages = data.get("value") or []
        except Exception as e:
            logger.warning("Could not fetch conversation %s: %s", conv_id, e)
            continue

        if conversation_has_reply(messages, our_email, row["gmail_message_id"]):
            who = row["contact_name"] or row["company"]
            reply_body = fetch_reply_body(conv_id, our_email, row["gmail_message_id"])
            set_lead_fields(row["id"], status="replied")
            try:
                from .reply_classify import process_reply_for_lead
                process_reply_for_lead(row["id"], reply_body, agent=row["agent"])
            except Exception as e:
                logger.warning("Reply classification failed for lead %s: %s", row["id"], e)
            create_notification(
                f"{who} ({row['company']}) replied to your outreach email",
                agent=row["agent"],
                lead_id=row["id"],
            )
            notified.append(who)
            logger.info("Reply detected from %s (lead %s)", who, row["id"])

    return {"checked": checked, "replies": len(notified), "notified": notified}


def main():
    import argparse

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Microsoft 365 mail integration for JayAgents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show connection status")
    sub.add_parser("scan", help="Scan for replies to sent outreach")
    args = parser.parse_args()

    if args.command == "status":
        s = microsoft_status(force=True)
        print(f"Connected as {s['email']}" if s["connected"] else f"Not connected: {s['detail']}")
    elif args.command == "scan":
        result = scan_replies()
        print(f"Checked {result['checked']} conversations — {result['replies']} new replies")
        for who in result["notified"]:
            print(f"  • {who} replied")


if __name__ == "__main__":
    main()
