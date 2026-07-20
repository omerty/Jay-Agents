"""Gmail integration — drafts, confirmed sending, and reply scanning.

Safety model:
  - The agent only CREATES drafts automatically. Nothing is ever sent
    without an explicit confirmation from the dashboard.
  - Reply scanning is read-only: it looks at threads we sent and flags
    leads that answered, creating a dashboard notification.

Setup — two roles:

  OPERATOR (you, once per deployment):
    Set GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET in server env, OR place credentials.json
    on the server. End users never see or touch these.

  END USER (each person using the dashboard):
    Click "Connect Gmail" → sign in with Google → done.
"""

import base64
import json
import logging
import os
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from . import env  # noqa: F401 — load .env

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

ROOT = Path(__file__).parent.parent
CREDENTIALS_PATH = Path(os.getenv("GOOGLE_CREDENTIALS_PATH", ROOT / "credentials.json"))
TOKEN_PATH = Path(os.getenv("GMAIL_TOKEN_PATH", ROOT / ".gmail_token.json"))
ACCOUNT_PATH = Path(os.getenv("GMAIL_ACCOUNT_PATH", ROOT / ".gmail_account.json"))


class GmailError(Exception):
    pass


def _client_config() -> dict | None:
    """OAuth app identity — from env vars (production) or credentials.json (local dev)."""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": [],
            }
        }
    if CREDENTIALS_PATH.exists():
        with open(CREDENTIALS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    return None


def oauth_client_ready() -> bool:
    return _client_config() is not None


def _make_oauth_flow(*, redirect_uri: str):
    from google_auth_oauthlib.flow import Flow

    cfg = _client_config()
    if not cfg:
        raise GmailError(
            "Gmail OAuth is not configured on this server. "
            "The operator must set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET "
            "in the server environment (recommended), or add credentials.json."
        )
    return Flow.from_client_config(cfg, scopes=SCOPES, redirect_uri=redirect_uri)


def _status_not_configured() -> dict:
    return {
        "connected": False,
        "email": None,
        "detail": "Gmail isn't set up on this server yet — ask your administrator.",
        "needs_operator_setup": True,
        "needs_credentials": True,  # backward compat
        "can_connect": False,
    }


# ---------------------------------------------------------------- auth


def _load_credentials():
    """Return valid Google credentials or None (never triggers a browser flow)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise GmailError(
            "Google API libraries not installed. Run: pip install -r requirements.txt"
        ) from e

    if not TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds
        except Exception as e:
            logger.warning("Gmail token refresh failed: %s", e)
    return None


def connect_interactive():
    """Run the OAuth browser flow and store the token. Call from a terminal."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        raise GmailError(
            "Google API libraries not installed. Run: pip install -r requirements.txt"
        ) from e

    cfg = _client_config()
    if not cfg:
        raise GmailError(
            "Gmail OAuth not configured. Set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET "
            "or add credentials.json — then use the dashboard Connect Gmail button."
        )

    if "installed" in cfg:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    elif CREDENTIALS_PATH.exists():
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    else:
        raise GmailError(
            "CLI connect requires credentials.json (Desktop app). "
            "With GOOGLE_CLIENT_ID/SECRET env vars, use the dashboard Connect Gmail button."
        )

    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    _invalidate_status_cache()
    return gmail_status()


def _service():
    creds = _load_credentials()
    if not creds:
        raise GmailError(
            "Gmail not connected. Run: python -m src.gmail_api connect"
        )
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


_status_cache: dict = {"at": 0.0, "value": None}
_STATUS_CACHE_TTL = 60  # seconds — avoid hitting the Gmail API on every health poll

_oauth_pending: dict[str, object] = {}


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


def _gmail_error_detail(exc: Exception) -> tuple[str, bool]:
    """Return (user-facing detail, needs_api_enable)."""
    msg = str(exc)
    if (
        "accessNotConfigured" in msg
        or "Gmail API has not been used" in msg
        or "Gmail API is disabled" in msg
    ):
        return (
            "Signed in with Google, but Gmail API is not enabled in your Cloud project. "
            "Open Google Cloud Console → APIs & Services → Library → enable Gmail API, "
            "wait a few minutes, then click Refresh status.",
            True,
        )
    if "Unable to find the server" in msg:
        return ("Cannot reach Gmail API — check this server's network/DNS.", False)
    short = msg if len(msg) <= 220 else msg[:217] + "..."
    return (f"Gmail API error: {short}", False)


def _authenticated_status(*, email: str | None, detail: str, needs_api_enable: bool) -> dict:
    return {
        "connected": False,
        "authenticated": True,
        "has_token": True,
        "email": email,
        "detail": detail,
        "needs_api_enable": needs_api_enable,
        "needs_operator_setup": False,
        "needs_credentials": False,
        "can_connect": not needs_api_enable,
    }


def oauth_redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/gmail/oauth/callback"


def oauth_start(redirect_uri: str) -> tuple[str, str]:
    """Begin browser OAuth. Returns (authorization_url, state)."""
    try:
        flow = _make_oauth_flow(redirect_uri=redirect_uri)
    except GmailError:
        raise
    except ImportError as e:
        raise GmailError(
            "Google API libraries not installed. Run: pip install -r requirements.txt"
        ) from e

    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    _oauth_pending[state] = flow
    return auth_url, state


def oauth_complete(state: str, code: str) -> dict:
    """Finish browser OAuth after Google redirects back with an auth code."""
    flow = _oauth_pending.pop(state, None)
    if not flow:
        raise GmailError("OAuth session expired — click Connect Gmail again")

    flow.fetch_token(code=code)
    TOKEN_PATH.write_text(flow.credentials.to_json())
    _invalidate_status_cache()
    return gmail_status(force=True)


def disconnect_gmail() -> dict:
    """Remove stored OAuth tokens so the user can sign in with a different account."""
    removed = []
    for path in (TOKEN_PATH, ACCOUNT_PATH):
        try:
            if path.exists():
                path.unlink()
                removed.append(path.name)
        except OSError as e:
            logger.warning("Could not remove %s: %s", path, e)
            raise GmailError(f"Could not disconnect Gmail ({path.name})") from e
    _oauth_pending.clear()
    _invalidate_status_cache()
    status = gmail_status(force=True)
    status["disconnected"] = True
    status["removed"] = removed
    return status


def gmail_status(*, force: bool = False) -> dict:
    """Health info: {connected, email, detail}. Cached for 60s."""
    import time

    if not force and _status_cache["value"] is not None:
        if time.monotonic() - _status_cache["at"] < _STATUS_CACHE_TTL:
            return _status_cache["value"]
    value = _gmail_status_uncached()
    _status_cache["at"] = time.monotonic()
    _status_cache["value"] = value
    return value


def _gmail_status_uncached() -> dict:
    if not oauth_client_ready():
        return _status_not_configured()
    try:
        creds = _load_credentials()
    except GmailError as e:
        return {
            "connected": False, "email": None, "detail": str(e),
            "needs_operator_setup": False, "needs_credentials": False, "can_connect": False,
        }
    if not creds:
        return {
            "connected": False, "email": None,
            "detail": "Click Connect Gmail to sign in with your Google account.",
            "needs_operator_setup": False, "needs_credentials": False, "can_connect": True,
        }
    try:
        profile = _service().users().getProfile(userId="me").execute()
        email = profile.get("emailAddress")
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
    except Exception as e:
        detail, needs_api_enable = _gmail_error_detail(e)
        return _authenticated_status(
            email=_read_account_email(),
            detail=detail,
            needs_api_enable=needs_api_enable,
        )


# ---------------------------------------------------------------- drafts & send


def build_mime(to: str, subject: str, body: str, sender: str | None = None) -> str:
    """Build a base64url-encoded RFC 2822 message."""
    msg = EmailMessage()
    msg["To"] = to
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(strip_subject_line(body))
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def gmail_draft_url(draft_id: str) -> str:
    """Deep link to open a draft in Gmail web UI."""
    return f"https://mail.google.com/mail/u/0/#drafts?compose={draft_id}"


def _connected_sender() -> str | None:
    status = gmail_status()
    if status.get("connected") and status.get("email"):
        return status["email"]
    return None


def create_draft(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft (NOT sent). Returns {draft_id, message_id, thread_id, sender, gmail_url}."""
    if not to or "@" not in to:
        raise GmailError(f"Invalid recipient email: {to!r}")
    sender = _connected_sender()
    if not sender:
        raise GmailError("Gmail not connected — sign in from the dashboard first")
    svc = _service()
    raw = build_mime(to, subject, body, sender=sender)
    draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    message = draft.get("message") or {}
    draft_id = draft["id"]
    return {
        "draft_id": draft_id,
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        "sender": sender,
        "gmail_url": gmail_draft_url(draft_id),
    }


def strip_subject_line(body: str) -> str:
    """Outreach bodies sometimes start with 'Subject: …' — drop it for the email body."""
    lines = body.strip().split("\n")
    if lines and lines[0].strip().lower().startswith("subject:"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines)


def send_draft(draft_id: str) -> dict:
    """Send an existing draft — only call after explicit user confirmation."""
    svc = _service()
    sent = svc.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}


def send_message(to: str, subject: str, body: str) -> dict:
    """Compose and send directly — only call after explicit user confirmation."""
    if not to or "@" not in to:
        raise GmailError(f"Invalid recipient email: {to!r}")
    svc = _service()
    sender = _connected_sender()
    raw = build_mime(to, subject, body, sender=sender)
    sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId")}


# ---------------------------------------------------------------- reply scanning


def thread_has_reply(thread: dict, our_email: str, our_message_id: str | None = None) -> bool:
    """True if the thread contains a message NOT sent by us (i.e. a reply)."""
    ours = (our_email or "").lower()
    for msg in thread.get("messages", []):
        if our_message_id and msg.get("id") == our_message_id:
            continue
        headers = {
            h["name"].lower(): h["value"]
            for h in (msg.get("payload") or {}).get("headers", [])
        }
        sender = parseaddr(headers.get("from", ""))[1].lower()
        if sender and sender != ours:
            return True
    return False


def scan_replies() -> dict:
    """
    Check Gmail threads for replies to outreach we sent.
    Marks leads 'replied' and creates a notification for each new reply.
    Returns {checked, replies, notified: [lead descriptions]}.
    """
    from .db import (
        create_notification,
        get_connection,
        init_db,
        set_lead_fields,
    )

    status = gmail_status()
    if not status["connected"]:
        raise GmailError(status["detail"] or "Gmail not connected")
    our_email = status["email"]

    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, agent, company, contact_name, gmail_thread_id, gmail_message_id "
            "FROM leads WHERE status = 'emailed' AND gmail_thread_id IS NOT NULL"
        ).fetchall()

    svc = _service()
    checked = 0
    notified: list[str] = []

    for row in rows:
        checked += 1
        try:
            thread = (
                svc.users().threads()
                .get(userId="me", id=row["gmail_thread_id"], format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
        except Exception as e:
            logger.warning("Could not fetch thread %s: %s", row["gmail_thread_id"], e)
            continue

        if thread_has_reply(thread, our_email, row["gmail_message_id"]):
            who = row["contact_name"] or row["company"]
            set_lead_fields(row["id"], status="replied")
            create_notification(
                f"{who} ({row['company']}) replied to your outreach email",
                agent=row["agent"],
                lead_id=row["id"],
            )
            notified.append(who)
            logger.info("Reply detected from %s (lead %s)", who, row["id"])

    return {"checked": checked, "replies": len(notified), "notified": notified}


# ---------------------------------------------------------------- CLI


def main():
    import argparse

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Gmail integration for JayAgents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("connect", help="Run the OAuth flow (opens a browser)")
    sub.add_parser("status", help="Show connection status")
    sub.add_parser("scan", help="Scan for replies to sent outreach")
    args = parser.parse_args()

    if args.command == "connect":
        s = connect_interactive()
        print(f"Connected as {s['email']}" if s["connected"] else f"Failed: {s['detail']}")
    elif args.command == "status":
        s = gmail_status()
        print(f"Connected as {s['email']}" if s["connected"] else f"Not connected: {s['detail']}")
    elif args.command == "scan":
        result = scan_replies()
        print(f"Checked {result['checked']} threads — {result['replies']} new replies")
        for who in result["notified"]:
            print(f"  • {who} replied")


if __name__ == "__main__":
    main()
