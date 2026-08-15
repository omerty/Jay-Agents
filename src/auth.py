"""Dashboard authentication — invite OTP signup, bcrypt passwords, DB sessions."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt

from . import env  # noqa: F401 — loads .env for CLI and server
from .db import (
    create_invite_row,
    create_session_row,
    create_user,
    delete_session_row,
    find_valid_invite,
    get_session_row,
    get_user_by_email,
    get_user_by_id,
    mark_invite_used,
    touch_user_login,
)

logger = logging.getLogger(__name__)

SESSION_COOKIE = "jayagents_session"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

PUBLIC_API_PATHS = frozenset({
    "/api/auth/login",
    "/api/auth/signup",
    "/api/health",
    "/api/gmail/oauth/callback",
    "/api/microsoft/oauth/callback",
    "/api/seamless/oauth/callback",
})


class AuthError(Exception):
    pass


def auth_enabled() -> bool:
    explicit = os.getenv("AUTH_ENABLED", "").strip().lower()
    if explicit in ("0", "false", "no"):
        return False
    if explicit in ("1", "true", "yes"):
        return True
    return bool(admin_secret())


def _pepper() -> str:
    secret = (os.getenv("AUTH_SESSION_SECRET") or os.getenv("AUTH_ADMIN_SECRET") or "").strip()
    if not secret or secret.startswith("<"):
        raise AuthError(
            "Auth secrets are not configured — run: openssl rand -hex 32 "
            "and set AUTH_SESSION_SECRET / AUTH_ADMIN_SECRET in .env"
        )
    return secret


def session_ttl_hours() -> int:
    return max(1, int(os.getenv("AUTH_SESSION_TTL_HOURS", "168")))


def invite_ttl_hours() -> int:
    return max(1, int(os.getenv("AUTH_INVITE_TTL_HOURS", "72")))


def cookie_secure() -> bool:
    return os.getenv("AUTH_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def admin_secret() -> str:
    return (os.getenv("AUTH_ADMIN_SECRET") or "").strip()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def hash_otp(otp: str) -> str:
    pepper = _pepper()
    return hashlib.sha256(f"{pepper}:{otp.strip()}".encode("utf-8")).hexdigest()


def generate_otp() -> str:
    return secrets.token_hex(4)


def _normalize_email(email: str) -> str:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Invalid email address")
    return email


def _validate_password(password: str):
    if len(password) < 10:
        raise AuthError("Password must be at least 10 characters")


def _session_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=session_ttl_hours())).isoformat()


def _invite_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=invite_ttl_hours())).isoformat()


def _public_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"]}


def create_invite(email: str) -> dict:
    email = _normalize_email(email)
    if get_user_by_email(email):
        raise AuthError("An account already exists for that email")
    otp = generate_otp()
    invite_id = create_invite_row(email, hash_otp(otp), _invite_expires_at())
    logger.info("Created invite %s for %s", invite_id, email)
    return {
        "email": email,
        "otp": otp,
        "expires_in_hours": invite_ttl_hours(),
    }


def signup(email: str, otp: str, password: str) -> dict:
    email = _normalize_email(email)
    _validate_password(password)
    if get_user_by_email(email):
        raise AuthError("An account already exists — sign in instead")
    invite = find_valid_invite(email, hash_otp(otp))
    if not invite:
        raise AuthError("Invalid or expired invite code")
    user = create_user(email, hash_password(password))
    mark_invite_used(invite["id"])
    session = _create_session(user["id"])
    return {"user": _public_user(user), "session_id": session}


def login(email: str, password: str) -> dict:
    email = _normalize_email(email)
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        raise AuthError("Invalid email or password")
    touch_user_login(user["id"])
    session = _create_session(user["id"])
    return {"user": _public_user(user), "session_id": session}


def change_password(user_id: int, current_password: str, new_password: str):
    user = get_user_by_id(user_id)
    if not user:
        raise AuthError("User not found")
    if not verify_password(current_password, user["password_hash"]):
        raise AuthError("Current password is incorrect")
    _validate_password(new_password)
    from .db import get_connection, init_db

    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.commit()


def logout(session_id: str | None):
    if session_id:
        delete_session_row(session_id)


def user_from_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    row = get_session_row(session_id)
    if not row:
        return None
    user = get_user_by_id(row["user_id"])
    return _public_user(user) if user else None


def user_from_request(request) -> dict | None:
    return user_from_session(request.cookies.get(SESSION_COOKIE))


def is_public_api(path: str) -> bool:
    if path in PUBLIC_API_PATHS:
        return True
    return path.startswith("/api/auth/admin/")


def is_public_page(path: str) -> bool:
    return path in ("/login.html", "/auth.js", "/style.css", "/favicon.ico")


def verify_admin_bearer(authorization: str | None) -> bool:
    secret = admin_secret()
    if not secret:
        return False
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:].strip()
    return secrets.compare_digest(token, secret)


def _create_session(user_id: int) -> str:
    session_id = uuid.uuid4().hex
    create_session_row(session_id, user_id, _session_expires_at())
    return session_id


def set_session_cookie(response, session_id: str):
    max_age = session_ttl_hours() * 3600
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(),
        max_age=max_age,
        path="/",
    )


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE, path="/")


def main():
    import sys

    if len(sys.argv) < 3 or sys.argv[1] != "invite":
        print("Usage: python -m src.auth invite user@example.com")
        raise SystemExit(1)
    try:
        result = create_invite(sys.argv[2])
    except AuthError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from e
    print(f"Invite created for {result['email']}")
    print(f"One-time code (send to user): {result['otp']}")
    print(f"Expires in {result['expires_in_hours']} hours")


if __name__ == "__main__":
    main()
