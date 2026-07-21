import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def auth_env(tmp_db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("AUTH_ADMIN_SECRET", "test-admin-secret")
    from src.web import app

    return TestClient(app)


def test_signup_with_invite(auth_env):
    invite = auth_env.post(
        "/api/auth/admin/invite",
        json={"email": "jay@example.com"},
        headers={"Authorization": "Bearer test-admin-secret"},
    )
    assert invite.status_code == 200
    otp = invite.json()["otp"]

    r = auth_env.post(
        "/api/auth/signup",
        json={"email": "jay@example.com", "otp": otp, "password": "secure-pass-1"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "jay@example.com"
    assert auth_env.cookies.get("jayagents_session")

    me = auth_env.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "jay@example.com"


def test_login_after_signup(auth_env):
    invite = auth_env.post(
        "/api/auth/admin/invite",
        json={"email": "login@example.com"},
        headers={"Authorization": "Bearer test-admin-secret"},
    ).json()

    auth_env.post(
        "/api/auth/signup",
        json={"email": "login@example.com", "otp": invite["otp"], "password": "secure-pass-2"},
    )

    auth_env.post("/api/auth/logout")

    bad = auth_env.post(
        "/api/auth/login",
        json={"email": "login@example.com", "password": "wrong-password"},
    )
    assert bad.status_code == 401

    ok = auth_env.post(
        "/api/auth/login",
        json={"email": "login@example.com", "password": "secure-pass-2"},
    )
    assert ok.status_code == 200
    assert auth_env.get("/api/agents").status_code == 200


def test_protected_api_requires_auth(auth_env):
    assert auth_env.get("/api/agents").status_code == 401


def test_public_health_without_auth(auth_env):
    assert auth_env.get("/api/health").status_code == 200


def test_admin_invite_requires_token(auth_env):
    r = auth_env.post("/api/auth/admin/invite", json={"email": "nope@example.com"})
    assert r.status_code == 401


def test_cli_invite(tmp_db, monkeypatch, capsys):
    monkeypatch.setenv("AUTH_SESSION_SECRET", "cli-secret")
    from src import auth

    result = auth.create_invite("cli@example.com")
    assert result["email"] == "cli@example.com"
    assert len(result["otp"]) >= 8

    user = auth.signup("cli@example.com", result["otp"], "cli-password-1")
    assert user["user"]["email"] == "cli@example.com"
