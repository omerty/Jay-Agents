import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from src.web import app

    return TestClient(app)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "llm" in body and "ok" in body["llm"]
    assert "contacts" in body and "provider" in body["contacts"]
    assert "pdl_key" in body


def test_contact_search_requires_key(client, monkeypatch):
    monkeypatch.setenv("WOODWAY_COMPANY_DISCOVERY", "actava")
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    monkeypatch.delenv("PDL_API_KEY", raising=False)
    monkeypatch.delenv("CONTACTS_PROVIDER", raising=False)
    monkeypatch.delenv("ACTAVA_API_KEY", raising=False)
    r = client.post("/api/agents/woodway/run", json={"mode": "contact_search", "mock": True})
    assert r.status_code == 503


def test_list_agents(client):
    r = client.get("/api/agents")
    assert r.status_code == 200
    names = {a["name"] for a in r.json()}
    assert names == {"woodway", "fonex", "keira"}


def test_unknown_agent_404(client):
    assert client.get("/api/agents/nope/leads").status_code == 404
    assert client.post("/api/agents/nope/run", json={"mode": "discover"}).status_code == 404


def test_run_rejects_unknown_mode_without_creating_job(client):
    r = client.post("/api/agents/woodway/run", json={"mode": "bogus", "mock": True})
    assert r.status_code == 400
    # agent must not be left blocked by a phantom job
    r2 = client.post("/api/agents/woodway/run", json={"mode": "bogus", "mock": True})
    assert r2.status_code == 400


def test_run_single_requires_prospect(client):
    r = client.post("/api/agents/woodway/run", json={"mode": "single", "mock": True})
    assert r.status_code == 400


def test_run_limit_bounds(client):
    r = client.post("/api/agents/woodway/run", json={"mode": "discover", "limit": 0, "mock": True})
    assert r.status_code == 422
    r = client.post("/api/agents/woodway/run", json={"mode": "discover", "limit": 501, "mock": True})
    assert r.status_code == 422


def test_run_requalify_all_accepts_mode(client, tmp_db, monkeypatch):
    import src.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "run_requalify_all",
        lambda *a, **k: {
            "processed": [],
            "count": 0,
            "failed": [],
            "failed_count": 0,
            "mode": "requalify_all",
        },
    )
    r = client.post("/api/agents/woodway/run", json={"mode": "requalify_all", "limit": 10, "mock": True})
    assert r.status_code == 200
    assert "job_id" in r.json()


def test_gmail_endpoints_without_credentials(client, tmp_db, monkeypatch):
    from src import gmail_api

    monkeypatch.setattr(gmail_api, "CREDENTIALS_PATH", tmp_db.DB_PATH.parent / "nope.json")
    gmail_api._status_cache["value"] = None

    lead_id, _ = tmp_db.upsert_lead(
        {"company": "Acme", "email": "j@acme.com", "source": "apollo",
         "outreach_subject": "Hi", "outreach_body": "Hello", "status": "drafted"},
        agent="woodway",
    )
    r = client.post(f"/api/leads/{lead_id}/gmail-draft")
    assert r.status_code == 503

    # send without confirm must be rejected regardless of Gmail state
    r = client.post(f"/api/leads/{lead_id}/gmail-send", json={"confirm": False})
    assert r.status_code == 400

    gmail_api._status_cache["value"] = None


def test_gmail_disconnect_endpoint(client, tmp_path, monkeypatch):
    from src import gmail_api

    token = tmp_path / "token.json"
    account = tmp_path / "account.json"
    token.write_text('{"token": "x"}')
    account.write_text('{"email": "user@example.com"}')

    monkeypatch.setattr(gmail_api, "TOKEN_PATH", token)
    monkeypatch.setattr(gmail_api, "ACCOUNT_PATH", account)
    monkeypatch.setattr(gmail_api, "CREDENTIALS_PATH", tmp_path / "nope.json")
    gmail_api._status_cache["value"] = None

    r = client.post("/api/gmail/disconnect")
    assert r.status_code == 200
    body = r.json()
    assert body["connected"] is False
    assert body.get("disconnected") is True
    assert not token.exists()
    assert not account.exists()


def test_gmail_draft_requires_email_and_outreach(client, tmp_db):
    no_email, _ = tmp_db.upsert_lead(
        {"company": "NoEmail Co", "source": "discover", "outreach_body": "Hi"},
        agent="woodway",
    )
    assert client.post(f"/api/leads/{no_email}/gmail-draft").status_code == 400

    no_outreach, _ = tmp_db.upsert_lead(
        {"company": "NoDraft Co", "email": "x@nodraft.com", "source": "apollo"},
        agent="woodway",
    )
    assert client.post(f"/api/leads/{no_outreach}/gmail-draft").status_code == 400


def test_notifications_endpoints(client, tmp_db):
    tmp_db.create_notification("Test reply", agent="woodway", lead_id=1)
    r = client.get("/api/notifications")
    assert r.status_code == 200
    assert r.json()["unread"] == 1

    r = client.post("/api/notifications/mark-read", json={})
    assert r.status_code == 200
    assert client.get("/api/notifications").json()["unread"] == 0


def test_automation_endpoint(client, tmp_db):
    r = client.get("/api/automation")
    assert r.status_code == 200
    body = r.json()
    assert "schedule" in body
    assert body["last_daily_run"] is None

    tmp_db.record_run("daily", ok=True, summary="ok", started_at="2026-01-01T00:00:00")
    assert client.get("/api/automation").json()["last_daily_run"]["summary"] == "ok"


def test_lead_status_validation(client, tmp_db):
    lead_id, _ = tmp_db.upsert_lead({"company": "Acme", "source": "csv"}, agent="woodway")
    r = client.post(f"/api/leads/{lead_id}/status", json={"status": "bogus"})
    assert r.status_code == 422
    r = client.post("/api/leads/999999/status", json={"status": "qualified"})
    assert r.status_code == 404
    r = client.post(f"/api/leads/{lead_id}/status", json={"status": "qualified"})
    assert r.status_code == 200
    assert client.get(f"/api/leads/{lead_id}").json()["status"] == "qualified"


def test_leads_include_contact_channel_fields(client, tmp_db):
    lead_id, _ = tmp_db.upsert_lead(
        {
            "company": "intact public entities",
            "contact_name": "glenn minnis",
            "linkedin_url": "linkedin.com/in/glennminnis",
            "source": "pdl",
        },
        agent="keira",
    )
    detail = client.get(f"/api/leads/{lead_id}").json()
    assert detail["contact_channel"] == "linkedin"
    assert detail["can_gmail"] is False
    assert "LinkedIn" in detail["contact_message"]

    listed = client.get("/api/agents/keira/leads").json()
    match = next(l for l in listed if l["id"] == lead_id)
    assert match["contact_channel"] == "linkedin"


def test_agent_stats_include_linkedin_only(client, tmp_db):
    tmp_db.upsert_lead(
        {
            "company": "intact public entities",
            "contact_name": "glenn minnis",
            "linkedin_url": "linkedin.com/in/glennminnis",
            "source": "pdl",
        },
        agent="keira",
    )
    agents = client.get("/api/agents").json()
    keira = next(a for a in agents if a["name"] == "keira")
    assert keira["stats"]["linkedin_only"] >= 1


def test_hunter_research_requires_key(client, tmp_db, monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    lead_id, _ = tmp_db.upsert_lead(
        {
            "company": "intact public entities",
            "contact_name": "glenn minnis",
            "linkedin_url": "linkedin.com/in/glennminnis",
            "source": "pdl",
        },
        agent="keira",
    )
    r = client.post(f"/api/leads/{lead_id}/hunter-research")
    assert r.status_code == 503


def test_hunter_research_finds_email(client, tmp_db, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    lead_id, _ = tmp_db.upsert_lead(
        {
            "company": "intact public entities",
            "contact_name": "glenn minnis",
            "linkedin_url": "linkedin.com/in/glennminnis",
            "source": "pdl",
        },
        agent="keira",
    )

    import src.contacts as contacts_mod

    monkeypatch.setattr(
        contacts_mod,
        "find_email",
        lambda *a, **k: {"email": "glenn@intact.com", "score": 92, "verified": True},
        raising=False,
    )
    # find_email is imported inside hunter_research_lead from hunter_api
    import src.hunter_api as hunter_mod

    monkeypatch.setattr(
        hunter_mod,
        "find_email",
        lambda *a, **k: {"email": "glenn@intact.com", "score": 92, "verified": True},
    )

    r = client.post(f"/api/leads/{lead_id}/hunter-research")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["email"] == "glenn@intact.com"
    assert body["lead"]["email"] == "glenn@intact.com"
    assert body["lead"]["contact_channel"] == "email"


def test_hunter_research_not_found(client, tmp_db, monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    lead_id, _ = tmp_db.upsert_lead(
        {
            "company": "intact public entities",
            "contact_name": "glenn minnis",
            "source": "pdl",
        },
        agent="keira",
    )
    import src.hunter_api as hunter_mod

    monkeypatch.setattr(hunter_mod, "find_email", lambda *a, **k: None)

    r = client.post(f"/api/leads/{lead_id}/hunter-research")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is False
    assert "LinkedIn" in body["message"]


def test_gmail_send_blocks_already_emailed(client, tmp_db, monkeypatch):
    from src import gmail_api

    monkeypatch.setattr(gmail_api, "gmail_status", lambda: {"connected": True, "email": "me@test.com"})
    monkeypatch.setattr(gmail_api, "send_message", lambda *_a, **_k: {"thread_id": "t", "message_id": "m"})

    lead_id, _ = tmp_db.upsert_lead(
        {
            "company": "Done Co",
            "email": "done@co.com",
            "status": "emailed",
            "outreach_subject": "Hi",
            "outreach_body": "Hello",
            "source": "pdl",
        },
        agent="woodway",
    )
    r = client.post(f"/api/leads/{lead_id}/gmail-send", json={"confirm": True})
    assert r.status_code == 409


def test_gmail_oauth_start_without_credentials(client, monkeypatch, tmp_path):
    from pathlib import Path

    from src import gmail_api

    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(gmail_api, "CREDENTIALS_PATH", tmp_path / "nope.json")
    gmail_api._status_cache["value"] = None
    r = client.get("/api/gmail/oauth/start", follow_redirects=False)
    assert r.status_code == 503


def test_gmail_oauth_start_with_env_client(client, monkeypatch):
    from pathlib import Path

    from src import gmail_api

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(gmail_api, "CREDENTIALS_PATH", Path("/nonexistent/credentials.json"))
    gmail_api._oauth_pending.clear()
    gmail_api._status_cache["value"] = None

    r = client.get("/api/gmail/oauth/start", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "accounts.google.com" in r.headers.get("location", "")


def test_gmail_setup_endpoint(client, monkeypatch, tmp_path):
    from src import gmail_api

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    monkeypatch.setattr(gmail_api, "CREDENTIALS_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(gmail_api, "TOKEN_PATH", tmp_path / "token.json")
    gmail_api._status_cache["value"] = None

    r = client.get("/api/gmail/setup")
    assert r.status_code == 200
    body = r.json()
    assert "redirect_uri" in body
    assert body["redirect_uri"].endswith("/api/gmail/oauth/callback")
    assert body["can_connect"] is True


def test_agent_prompts_get_and_update(client, tmp_path, monkeypatch):
    import src.agent as agent_mod
    import src.prompts as prompts_mod

    agents_dir = tmp_path / "agents" / "woodway"
    agents_dir.mkdir(parents=True)
    (agents_dir / "config.yaml").write_text(
        "name: woodway\ncompany: Woodway\nproduct: EviData\ntagline: Tag\n"
        "icp:\n  industries: [tech]\n  titles: [CEO]\n  company_size: large\n"
        "value_props: [One]\n"
    )
    monkeypatch.setattr(agent_mod, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.setattr(prompts_mod, "AGENTS_DIR", tmp_path / "agents")

    r = client.get("/api/agents/woodway/prompts")
    assert r.status_code == 200
    body = r.json()
    assert "templates" in body and "qualify_user" in body["templates"]

    r = client.put("/api/agents/woodway/prompts", json={"qualify_extra": "Be strict on geography."})
    assert r.status_code == 200
    assert r.json()["values"]["qualify_extra"] == "Be strict on geography."

    assert client.get("/api/agents/nope/prompts").status_code == 404


def test_cancel_running_job(client, tmp_db, monkeypatch):
    import src.agent as agent_mod
    import time

    def slow_requalify(*args, **kwargs):
        emit = kwargs.get("on_progress") or (lambda msg: None)
        for i in range(30):
            emit(f"step {i}")
            time.sleep(0.2)
        return {
            "processed": [],
            "count": 0,
            "failed": [],
            "failed_count": 0,
            "mode": "requalify_all",
        }

    monkeypatch.setattr(agent_mod, "run_requalify_all", slow_requalify)
    started = client.post(
        "/api/agents/woodway/run",
        json={"mode": "requalify_all", "limit": 10, "mock": True},
    )
    assert started.status_code == 200
    job_id = started.json()["job_id"]

    cancel = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel.status_code == 200

    deadline = time.time() + 5
    status = "running"
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        status = job["status"]
        if status != "running":
            break
        time.sleep(0.1)

    assert status == "cancelled"
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
