"""Web dashboard for demoing all three agents.

Run:  python -m src.web   →  http://localhost:8400
"""

import logging
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import env  # noqa: F401 — loads .env
from . import auth
from .agent import load_agent, run_discover_workflow, run_process_imported, run_requalify_all, run_workflow
from .config_check import validate_config
from .contact_channel import enrich_lead
from .contacts import ContactsError, contacts_status, hunter_research_lead, resolve_contacts_provider, search_and_import_contacts
from .seamless import seamless_status
from .seamless_api import seamless_available
from .seamless_oauth import (
    SeamlessOAuthError,
    disconnect as disconnect_seamless,
    oauth_client_ready as seamless_oauth_client_ready,
    oauth_complete as seamless_oauth_complete,
    oauth_redirect_uri as seamless_oauth_redirect_uri,
    oauth_start as seamless_oauth_start,
)
from .actava import actava_status_export
from .actava_api import actava_available
from .woodway_pipeline import woodway_company_discovery_mode
from .db import (
    STATUSES,
    add_suppression,
    export_csv,
    get_connection,
    get_evidence,
    get_lead,
    get_leads,
    get_notifications,
    get_review_queue,
    init_db,
    last_run,
    list_signals,
    list_suppression,
    mark_notifications_read,
    set_lead_fields,
    stats,
    today_stats,
    update_lead_status,
)
from .gmail_api import (
    CREDENTIALS_PATH,
    GmailError,
    create_draft,
    disconnect_gmail,
    gmail_status,
    oauth_complete,
    oauth_redirect_uri,
    oauth_start,
    scan_replies,
    send_draft,
    send_message,
)
from .microsoft_mail_api import (
    MicrosoftMailError,
    create_draft as ms_create_draft,
    disconnect_microsoft,
    microsoft_status,
    oauth_complete as ms_oauth_complete,
    oauth_redirect_uri as ms_oauth_redirect_uri,
    oauth_start as ms_oauth_start,
    scan_replies as ms_scan_replies,
    send_draft as ms_send_draft,
    send_message as ms_send_message,
)
from .llm import check_llm
from .prompts import get_prompt_settings, save_prompt_settings

logger = logging.getLogger(__name__)

_health_cache: dict | None = None
_health_cache_at: float = 0.0


def _health_cache_ttl() -> float:
    return float(os.getenv("HEALTH_CACHE_TTL", "120"))


def _build_health_payload() -> dict:
    llm = check_llm()
    contacts = contacts_status()
    cfg = validate_config()
    return {
        "llm": llm,
        "contacts": contacts,
        "seamless": seamless_status(),
        "actava": actava_status_export(),
        "gmail": gmail_status(),
        "microsoft": microsoft_status(),
        "config_ok": cfg["ok"],
        "config_issues": cfg["issues"],
        "config_warnings": cfg["warnings"],
        # kept for backward compatibility
        "pdl_key": bool(os.environ.get("PDL_API_KEY")),
    }


@asynccontextmanager
async def _lifespan(app):
    cfg = validate_config()
    if cfg["issues"]:
        logger.warning("Startup config issues: %s", "; ".join(cfg["issues"]))
    for w in cfg["warnings"]:
        logger.info("Startup config warning: %s", w)
    _start_reply_scanner()
    yield


app = FastAPI(title="JayAgents Dashboard", lifespan=_lifespan)

WEB_DIR = Path(__file__).parent.parent / "web"
AGENT_NAMES = ["woodway", "fonex", "keira"]


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not auth.auth_enabled():
        return await call_next(request)

    path = request.url.path.rstrip("/") or "/"

    if path.startswith("/api/"):
        if auth.is_public_api(path):
            return await call_next(request)
        user = auth.user_from_request(request)
        if not user:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        request.state.user = user
        return await call_next(request)

    if path in ("", "/", "/index.html") and request.method == "GET":
        if not auth.user_from_request(request):
            return RedirectResponse("/login.html")

    return await call_next(request)

# ---------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_FINISHED_JOBS = 50


class JobCancelled(Exception):
    """Raised when a background job receives a cancel request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prune_jobs():
    """Keep memory bounded — drop oldest finished jobs (call with JOBS_LOCK held)."""
    finished = [j for j in JOBS.values() if j["status"] != "running"]
    if len(finished) <= MAX_FINISHED_JOBS:
        return
    finished.sort(key=lambda j: j["finished_at"] or "")
    for job in finished[: len(finished) - MAX_FINISHED_JOBS]:
        JOBS.pop(job["id"], None)


def _new_job(agent: str, kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        _prune_jobs()
        JOBS[job_id] = {
            "id": job_id,
            "agent": agent,
            "kind": kind,
            "status": "running",
            "cancel_requested": False,
            "log": [],
            "result": None,
            "error": None,
            "started_at": _now(),
            "finished_at": None,
        }
    return job_id


def _job_log(job_id: str, msg: str):
    with JOBS_LOCK:
        JOBS[job_id]["log"].append({"t": _now(), "msg": msg})


def _check_cancelled(job_id: str):
    with JOBS_LOCK:
        if JOBS.get(job_id, {}).get("cancel_requested"):
            raise JobCancelled()


def _make_job_logger(job_id: str):
    def log(msg: str = ""):
        _check_cancelled(job_id)
        if msg:
            _job_log(job_id, msg)

    return log


def _finish_job(job_id: str, *, result=None, error=None, cancelled: bool = False):
    with JOBS_LOCK:
        job = JOBS[job_id]
        if cancelled:
            job["status"] = "cancelled"
        elif error:
            job["status"] = "error"
        else:
            job["status"] = "done"
        job["result"] = result
        job["error"] = error
        job["finished_at"] = _now()


def _agent_running(agent: str) -> bool:
    with JOBS_LOCK:
        return any(j["agent"] == agent and j["status"] == "running" for j in JOBS.values())


def _run_in_thread(job_id: str, fn):
    def runner():
        try:
            result = fn()
            _check_cancelled(job_id)
            _finish_job(job_id, result=result)
        except JobCancelled:
            _job_log(job_id, "Cancelled by user")
            _finish_job(job_id, cancelled=True)
        except Exception as e:  # surface to UI
            logger.exception("Job %s failed", job_id)
            _job_log(job_id, f"Error: {e}")
            _finish_job(job_id, error=str(e))

    threading.Thread(target=runner, daemon=True).start()


# ---------------------------------------------------------------- helpers


def _agent_summary(name: str) -> dict:
    cfg = load_agent(name)
    icp = cfg.get("icp", {})
    industries = icp.get("industries", [])
    if isinstance(industries, str):
        industries = [industries]
    geo = icp.get("geography")
    if isinstance(geo, dict):
        geo = list(geo.values())
    elif isinstance(geo, str):
        geo = [geo]
    return {
        "name": name,
        "company": cfg.get("company"),
        "product": cfg.get("product"),
        "tagline": cfg.get("tagline"),
        "industries": industries,
        "titles": icp.get("titles", []),
        "geography": geo or [],
        "value_props": cfg.get("value_props", []),
        "disqualifiers": cfg.get("disqualifiers", []),
        "stats": stats(agent=name),
    }


# ---------------------------------------------------------------- API


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    otp: str = Field(min_length=4, max_length=64)
    password: str = Field(min_length=10, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@app.get("/api/auth/me")
def auth_me(request: Request):
    user = auth.user_from_request(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


@app.post("/api/auth/login")
def auth_login(body: LoginRequest):
    try:
        result = auth.login(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(401, str(e)) from e
    response = JSONResponse({"ok": True, "user": result["user"]})
    auth.set_session_cookie(response, result["session_id"])
    return response


@app.post("/api/auth/signup")
def auth_signup(body: SignupRequest):
    try:
        result = auth.signup(body.email, body.otp, body.password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e
    response = JSONResponse({"ok": True, "user": result["user"]})
    auth.set_session_cookie(response, result["session_id"])
    return response


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    auth.logout(request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response)
    return response


@app.post("/api/auth/password")
def auth_change_password(body: ChangePasswordRequest, request: Request):
    user = auth.user_from_request(request)
    if not user:
        raise HTTPException(401, "Not signed in")
    try:
        auth.change_password(user["id"], body.current_password, body.new_password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.post("/api/auth/admin/invite")
def auth_admin_invite(body: InviteRequest, request: Request):
    if not auth.verify_admin_bearer(request.headers.get("authorization")):
        raise HTTPException(401, "Invalid admin token")
    try:
        result = auth.create_invite(body.email)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "ok": True,
        "email": result["email"],
        "otp": result["otp"],
        "expires_in_hours": result["expires_in_hours"],
    }


@app.get("/api/health")
def health(refresh: bool = False):
    global _health_cache, _health_cache_at
    now = time.monotonic()
    if not refresh and _health_cache is not None and now - _health_cache_at < _health_cache_ttl():
        return _health_cache
    payload = _build_health_payload()
    _health_cache = payload
    _health_cache_at = now
    return payload


@app.get("/api/automation")
def automation():
    """Schedule status for the dashboard: last daily run + reply-scan info."""
    daily = last_run("daily")
    scan = last_run("reply_scan")
    hour = int(os.getenv("DAILY_CRON_HOUR", "0"))
    return {
        "schedule": (
            "manual only — Woodway/Keira auto-run off "
            "(DAILY_RUN_WOODWAY / DAILY_RUN_KEIRA default false; "
            "remove cron with scripts/remove_cron.sh)"
        ),
        "cron_hour": hour,
        "daily_run_woodway": os.getenv("DAILY_RUN_WOODWAY", "false"),
        "daily_run_keira": os.getenv("DAILY_RUN_KEIRA", "false"),
        "last_daily_run": daily,
        "last_reply_scan": scan,
    }


@app.get("/api/agents")
def list_agents():
    return [_agent_summary(n) for n in AGENT_NAMES]


@app.get("/api/agents/{agent}/leads")
def agent_leads(agent: str, status: str | None = None):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    return [enrich_lead(l) for l in get_leads(agent=agent, status=status)]


@app.get("/api/leads/{lead_id}")
def lead_detail(lead_id: int):
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "lead not found")
    lead = enrich_lead(dict(row))
    lead["evidence"] = get_evidence(entity_id=lead_id) or get_evidence(company=lead.get("company"))
    from .sequences import get_sequence_steps
    lead["sequence_steps"] = get_sequence_steps(lead_id)
    return lead


class StatusUpdate(BaseModel):
    status: str


@app.post("/api/leads/{lead_id}/status")
def set_lead_status(lead_id: int, body: StatusUpdate):
    if body.status not in STATUSES:
        raise HTTPException(422, f"invalid status — must be one of: {', '.join(STATUSES)}")
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "lead not found")
    update_lead_status(lead_id, body.status)
    return {"ok": True}


class RunRequest(BaseModel):
    mode: str  # discover | process_imported | requalify_all | single | contact_search | pdl_search | woodway_pipeline | keira_pipeline | recontact
    prospect: str | None = Field(default=None, max_length=300)
    limit: int = Field(default=5, ge=1, le=500)
    mock: bool = False


@app.post("/api/agents/{agent}/run")
def run_agent(agent: str, body: RunRequest):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    if _agent_running(agent):
        raise HTTPException(409, "a job is already running for this agent")

    use_llm = not body.mock
    if use_llm and body.mode in (
        "discover", "process_imported", "requalify_all", "single",
        "woodway_pipeline", "keira_pipeline",
    ):
        llm = check_llm()
        if not llm["ok"] and not llm.get("fallback"):
            raise HTTPException(503, f"LLM unavailable: {llm['detail']} — fix it or use Demo mode (mock)")

    # Validate before creating the job — an early HTTPException must not leave
    # a phantom "running" job that blocks the agent
    if body.mode not in (
        "discover", "process_imported", "requalify_all", "single",
        "contact_search", "pdl_search", "woodway_pipeline", "keira_pipeline",
        "recontact",
    ):
        raise HTTPException(400, f"unknown mode: {body.mode}")
    if body.mode == "single" and not (body.prospect and body.prospect.strip()):
        raise HTTPException(400, "prospect required for single mode")
    if body.mode == "recontact":
        if agent not in ("woodway", "keira"):
            raise HTTPException(400, "recontact is only available for Woodway and Keira")
    if body.mode == "woodway_pipeline":
        if agent != "woodway":
            raise HTTPException(400, "woodway_pipeline is only available for Woodway")
        if woodway_company_discovery_mode() == "actava" and not actava_available():
            raise HTTPException(
                503,
                "ACTAVA_API_KEY not set — set WOODWAY_COMPANY_DISCOVERY=anthropic (default) or add Actava key",
            )
    if body.mode == "keira_pipeline":
        if agent != "keira":
            raise HTTPException(400, "keira_pipeline is only available for Keira")
    if body.mode in ("contact_search", "pdl_search"):
        if agent == "woodway" and (
            woodway_company_discovery_mode() == "anthropic" or actava_available()
        ):
            pass  # Woodway contact button runs the full pipeline
        elif agent == "keira" and (seamless_available() or actava_available()):
            pass  # Keira contact button runs keira_pipeline
        else:
            c = contacts_status()
            if not c["configured"]:
                key = "APOLLO_API_KEY" if c["provider"] == "apollo" else "PDL_API_KEY"
                raise HTTPException(503, f"{key} not set — add it to .env")

    job_id = _new_job(agent, body.mode)
    log = _make_job_logger(job_id)

    if body.mode == "discover":
        def work():
            result = run_discover_workflow(
                agent, limit=body.limit, use_llm=use_llm, on_progress=log,
            )
            d = result["discovery"]
            top = [
                {
                    "company": q["company"],
                    "score": q["qualification"]["score"],
                    "tier": q["qualification"]["tier"],
                    "signal": q.get("signal"),
                    "lead_id": q.get("lead_id"),
                }
                for q in d["qualified"]
            ]
            log(f"Done — {len(top)} new prospects qualified")
            return {
                "hits": len(d["hits"]),
                "extracted": len(d["leads"]),
                "skipped_duplicates": d.get("skipped_duplicates", 0),
                "qualified": top,
            }

    elif body.mode == "process_imported":
        def work():
            result = run_process_imported(
                agent, limit=body.limit, use_llm=use_llm, on_progress=log,
            )
            rows = [
                {
                    "company": p.get("company"),
                    "contact_name": p.get("contact_name"),
                    "score": p["qualification"]["score"],
                    "tier": p["qualification"]["tier"],
                    "lead_id": p.get("lead_id") or p.get("id"),
                }
                for p in result["processed"]
            ]
            log(f"Done — {result['count']} contacts processed")
            return {"processed": rows, "count": result["count"]}

    elif body.mode == "requalify_all":
        def work():
            result = run_requalify_all(
                agent, limit=body.limit, use_llm=use_llm, on_progress=log,
            )
            rows = [
                {
                    "company": p.get("company"),
                    "contact_name": p.get("contact_name"),
                    "score": p["qualification"]["score"],
                    "tier": p["qualification"]["tier"],
                    "lead_id": p.get("lead_id") or p.get("id"),
                }
                for p in result["processed"]
            ]
            failed = result.get("failed_count", 0)
            log(f"Done — {result['count']} leads re-qualified" + (f", {failed} failed" if failed else ""))
            return {
                "processed": rows,
                "count": result["count"],
                "failed_count": failed,
            }

    elif body.mode == "single":
        def work():
            log(f"Researching & qualifying: {body.prospect}")
            result = run_workflow(agent, body.prospect, use_llm=use_llm)
            q = result["qualification"]
            log(f"Scored {q['score']}/100 ({q['tier'].upper()})")
            outreach = result.get("outreach")
            if outreach:
                log("Outreach draft generated")
            return {
                "prospect": result["prospect"],
                "qualification": q,
                "outreach": outreach,
                "lead_id": result.get("lead_id"),
            }

    elif body.mode == "woodway_pipeline" or (
        body.mode in ("contact_search", "pdl_search") and agent == "woodway" and (
            woodway_company_discovery_mode() == "anthropic" or actava_available()
        )
    ):
        def work():
            from .woodway_pipeline import run_woodway_pipeline, woodway_contact_discovery_mode

            contact_mode = woodway_contact_discovery_mode()
            contact_label = {
                "seamless": "Seamless contacts",
                "web": "web contacts",
                "paid": "paid contacts",
            }.get(contact_mode, "contacts")
            log(
                f"Woodway pipeline — Claude companies → {contact_label} "
                f"→ qualify → Outlook drafts…"
            )
            result = run_woodway_pipeline(
                limit=body.limit if body.limit and body.limit != 5 else 50,
                use_llm=use_llm,
                create_drafts=True,
                on_progress=log,
            )
            drafts = (result.get("steps") or {}).get("drafts") or {}
            log(
                f"Pipeline complete — {drafts.get('created', 0)} "
                f"{drafts.get('provider') or 'mailbox'} draft(s) ready to send"
            )
            return result

    elif body.mode == "keira_pipeline" or (
        body.mode in ("contact_search", "pdl_search")
        and agent == "keira"
        and (seamless_available() or actava_available())
    ):
        def work():
            from .keira_pipeline import run_keira_pipeline

            log("Keira pipeline — company-first gates → Seamless contacts for survivors → drafts…")
            result = run_keira_pipeline(
                limit=body.limit,
                use_llm=use_llm,
                create_drafts=True,
                on_progress=log,
            )
            drafts = (result.get("steps") or {}).get("drafts") or {}
            gates = (result.get("steps") or {}).get("gates") or {}
            log(
                f"Pipeline complete — {gates.get('survivors', 0)} survivors, "
                f"{drafts.get('created', 0)} draft(s)"
            )
            return result

    elif body.mode == "recontact":
        def work():
            limit = body.limit if body.limit and body.limit != 5 else 50
            if agent == "woodway":
                from .woodway_pipeline import recontact_awaiting

                log(f"Re-contact — Seamless/web for awaiting/missing-email companies (up to {limit})…")
                result = recontact_awaiting(
                    agent="woodway",
                    limit=limit,
                    skip_existing=False,
                    on_progress=log,
                )
            else:
                from .keira_contacts import recontact_awaiting

                log(f"Re-contact — Seamless/web for awaiting/missing-email companies (up to {limit})…")
                result = recontact_awaiting(
                    agent="keira",
                    limit=limit,
                    skip_existing=False,
                    on_progress=log,
                )
            if result.get("skipped"):
                log("Done — no awaiting_contact companies found")
            else:
                log(
                    f"Done — {result.get('awaiting', 0)} companies, "
                    f"+{result.get('imported', 0)} imported / "
                    f"~{result.get('updated', 0)} updated"
                )
            return result

    else:  # contact_search (pdl_search kept as legacy alias)
        def work():
            if agent == "keira" and seamless_available():
                from .seamless import search_and_import_seamless

                budget = seamless_status()
                emit_budget = budget.get("credits_remaining_budget", "?")
                log(f"Seamless search for Keira (budget {emit_budget} credits remaining this month)…")
                result = search_and_import_seamless(agent, limit=body.limit, on_progress=log)
                summary = (
                    f"Done — {result.get('imported', 0)} imported, "
                    f"{result.get('with_email', 0)} with email "
                    f"({result.get('credits_used', 0)} Seamless credits used)"
                )
                log(summary)
                return result

            if agent == "keira" and actava_available():
                from .actava import search_and_import_actava

                status = actava_status_export()
                mode = status.get("mode", "actava")
                log(f"Actava prospect search for Keira ({mode})…")
                result = search_and_import_actava(agent, limit=body.limit, on_progress=log)
                log(
                    f"Done — {result.get('imported', 0)} imported "
                    f"via Actava {result.get('mode', mode)}"
                )
                return result

            provider = resolve_contacts_provider()
            log(f"Contact search via {provider} (up to {body.limit} contacts)…")
            result = search_and_import_contacts(agent, limit=body.limit, on_progress=log)
            summary = (
                f"Done — {result.get('imported', 0)} imported, "
                f"{result.get('skipped', 0)} skipped"
            )
            if result.get("with_email") is not None:
                summary += f", {result['with_email']} with email"
            if provider == "apollo":
                summary += " (search was free)"
            elif result.get("credits_used") is not None:
                summary += f" ({result.get('credits_used', 0)} PDL credits used)"
            log(summary)
            return result

    _run_in_thread(job_id, work)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        return dict(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "unknown job")
        if job["status"] != "running":
            raise HTTPException(409, f"job is already {job['status']}")
        job["cancel_requested"] = True
    _job_log(job_id, "Cancel requested…")
    return {"ok": True, "job_id": job_id}


# ---------------------------------------------------------------- gmail


def _lead_or_404(lead_id: int) -> dict:
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    return lead


@app.post("/api/leads/{lead_id}/gmail-draft")
def gmail_create_draft(lead_id: int):
    """Create a Gmail draft for this lead's outreach. Never sends."""
    lead = _lead_or_404(lead_id)
    if not lead.get("email"):
        raise HTTPException(400, "lead has no email address")
    if not lead.get("outreach_body"):
        raise HTTPException(400, "lead has no outreach draft — process it first")
    try:
        result = create_draft(
            lead["email"],
            lead.get("outreach_subject") or "Quick question",
            lead["outreach_body"],
        )
    except GmailError as e:
        raise HTTPException(503, str(e))
    set_lead_fields(
        lead_id,
        gmail_draft_id=result["draft_id"],
        gmail_thread_id=result["thread_id"],
        gmail_message_id=result["message_id"],
        mail_provider="gmail",
    )
    return {"ok": True, **result}


@app.post("/api/leads/{lead_id}/hunter-research")
def hunter_research(lead_id: int):
    """On-demand Hunter email lookup for one lead. Not guaranteed; uses 1 Hunter credit."""
    _lead_or_404(lead_id)
    try:
        result = hunter_research_lead(lead_id)
    except ContactsError as e:
        msg = str(e)
        code = 503 if "not configured" in msg.lower() else 400
        raise HTTPException(code, msg)
    lead = enrich_lead(get_lead(lead_id))
    return {**result, "lead": lead}


class SendRequest(BaseModel):
    confirm: bool = False


@app.post("/api/leads/{lead_id}/gmail-send")
def gmail_send(lead_id: int, body: SendRequest):
    """Send the outreach email. Requires explicit confirm=true — the safety gate."""
    if not body.confirm:
        raise HTTPException(400, "confirmation required — pass {\"confirm\": true}")
    lead = _lead_or_404(lead_id)
    if lead.get("status") in ("emailed", "replied"):
        raise HTTPException(409, f"lead already {lead['status']} — cannot send again")
    if not lead.get("email"):
        raise HTTPException(400, "lead has no email address")
    try:
        if lead.get("gmail_draft_id"):
            result = send_draft(lead["gmail_draft_id"])
        else:
            if not lead.get("outreach_body"):
                raise HTTPException(400, "lead has no outreach draft — process it first")
            result = send_message(
                lead["email"],
                lead.get("outreach_subject") or "Quick question",
                lead["outreach_body"],
            )
    except GmailError as e:
        raise HTTPException(503, str(e))
    set_lead_fields(
        lead_id,
        status="emailed",
        emailed_at=_now(),
        gmail_thread_id=result["thread_id"] or lead.get("gmail_thread_id"),
        gmail_message_id=result["message_id"] or lead.get("gmail_message_id"),
        mail_provider="gmail",
    )
    logger.info("Email sent to lead %s (%s)", lead_id, lead["email"])
    return {"ok": True, **result}


@app.post("/api/gmail/scan-replies")
def gmail_scan_replies():
    try:
        result = scan_replies()
    except GmailError as e:
        raise HTTPException(503, str(e))
    from .db import record_run

    record_run("reply_scan", ok=True,
               summary=f"{result['replies']} new replies of {result['checked']} checked",
               started_at=_now())
    return result


@app.post("/api/gmail/disconnect")
def gmail_disconnect():
    """Remove stored Gmail OAuth tokens so the user can connect a different account."""
    try:
        return disconnect_gmail()
    except GmailError as e:
        raise HTTPException(500, str(e))


@app.get("/api/gmail/oauth/start")
def gmail_oauth_start(request: Request):
    """Redirect to Google sign-in — completes via /api/gmail/oauth/callback."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = oauth_redirect_uri(base)
    try:
        auth_url, _ = oauth_start(redirect_uri)
    except GmailError as e:
        raise HTTPException(503, str(e))
    return RedirectResponse(auth_url)


@app.get("/api/gmail/oauth/callback")
def gmail_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return RedirectResponse("/?gmail=denied")
    if not code or not state:
        raise HTTPException(400, "missing OAuth code or state")
    try:
        oauth_complete(state, code)
    except GmailError:
        return RedirectResponse("/?gmail=error")
    return RedirectResponse("/?gmail=connected")


@app.get("/api/gmail/setup")
def gmail_setup(request: Request):
    """OAuth redirect URI to register in Google Cloud Console."""
    base = str(request.base_url).rstrip("/")
    status = gmail_status(force=True)
    return {
        **status,
        "redirect_uri": oauth_redirect_uri(base),
        "credentials_path": CREDENTIALS_PATH.name,
    }


# ---------------------------------------------------------------- microsoft / outlook


@app.post("/api/leads/{lead_id}/microsoft-draft")
def microsoft_create_draft(lead_id: int):
    """Create an Outlook / Microsoft 365 draft for this lead's outreach. Never sends."""
    lead = _lead_or_404(lead_id)
    if not lead.get("email"):
        raise HTTPException(400, "lead has no email address")
    if not lead.get("outreach_body"):
        raise HTTPException(400, "lead has no outreach draft — process it first")
    try:
        result = ms_create_draft(
            lead["email"],
            lead.get("outreach_subject") or "Quick question",
            lead["outreach_body"],
        )
    except MicrosoftMailError as e:
        raise HTTPException(503, str(e))
    set_lead_fields(
        lead_id,
        gmail_draft_id=result["draft_id"],
        gmail_thread_id=result["thread_id"],
        gmail_message_id=result["message_id"],
        mail_provider="microsoft",
    )
    return {"ok": True, **result}


@app.post("/api/leads/{lead_id}/microsoft-send")
def microsoft_send(lead_id: int, body: SendRequest):
    """Send the outreach email via Microsoft. Requires confirm=true — the safety gate."""
    if not body.confirm:
        raise HTTPException(400, "confirmation required — pass {\"confirm\": true}")
    lead = _lead_or_404(lead_id)
    if lead.get("status") in ("emailed", "replied"):
        raise HTTPException(409, f"lead already {lead['status']} — cannot send again")
    if not lead.get("email"):
        raise HTTPException(400, "lead has no email address")
    try:
        if lead.get("gmail_draft_id") and lead.get("mail_provider") == "microsoft":
            result = ms_send_draft(lead["gmail_draft_id"])
        else:
            if not lead.get("outreach_body"):
                raise HTTPException(400, "lead has no outreach draft — process it first")
            result = ms_send_message(
                lead["email"],
                lead.get("outreach_subject") or "Quick question",
                lead["outreach_body"],
            )
    except MicrosoftMailError as e:
        raise HTTPException(503, str(e))
    set_lead_fields(
        lead_id,
        status="emailed",
        emailed_at=_now(),
        gmail_thread_id=result.get("thread_id") or lead.get("gmail_thread_id"),
        gmail_message_id=result.get("message_id") or lead.get("gmail_message_id"),
        mail_provider="microsoft",
    )
    logger.info("Microsoft email sent to lead %s (%s)", lead_id, lead["email"])
    return {"ok": True, **result}


@app.post("/api/microsoft/scan-replies")
def microsoft_scan_replies():
    try:
        result = ms_scan_replies()
    except MicrosoftMailError as e:
        raise HTTPException(503, str(e))
    from .db import record_run

    record_run(
        "reply_scan_microsoft",
        ok=True,
        summary=f"{result['replies']} new replies of {result['checked']} checked",
        started_at=_now(),
    )
    return result


@app.post("/api/microsoft/disconnect")
def microsoft_disconnect():
    """Remove stored Microsoft OAuth tokens so the user can connect a different account."""
    try:
        return disconnect_microsoft()
    except MicrosoftMailError as e:
        raise HTTPException(500, str(e))


@app.get("/api/microsoft/oauth/start")
def microsoft_oauth_start(request: Request):
    """Redirect to Microsoft sign-in — completes via /api/microsoft/oauth/callback."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = ms_oauth_redirect_uri(base)
    try:
        auth_url, _ = ms_oauth_start(redirect_uri)
    except MicrosoftMailError as e:
        raise HTTPException(503, str(e))
    return RedirectResponse(auth_url)


@app.get("/api/microsoft/oauth/callback")
def microsoft_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return RedirectResponse("/?microsoft=denied")
    if not code or not state:
        raise HTTPException(400, "missing OAuth code or state")
    try:
        ms_oauth_complete(state, code)
    except MicrosoftMailError:
        return RedirectResponse("/?microsoft=error")
    return RedirectResponse("/?microsoft=connected")


@app.get("/api/microsoft/setup")
def microsoft_setup(request: Request):
    """OAuth redirect URI to register in Azure AD app registration."""
    base = str(request.base_url).rstrip("/")
    status = microsoft_status(force=True)
    return {
        **status,
        "redirect_uri": ms_oauth_redirect_uri(base),
        "tenant": (os.getenv("MICROSOFT_TENANT_ID") or "common").strip() or "common",
    }


@app.post("/api/seamless/disconnect")
def seamless_disconnect():
    try:
        disconnect_seamless()
        return {"ok": True, "connected": False, "disconnected": True}
    except SeamlessOAuthError as e:
        raise HTTPException(500, str(e))


@app.get("/api/seamless/oauth/start")
def seamless_oauth_start_route(request: Request):
    """Redirect to Seamless sign-in — uses your web account credits via OAuth."""
    base = str(request.base_url).rstrip("/")
    redirect_uri = seamless_oauth_redirect_uri(base)
    try:
        auth_url, _ = seamless_oauth_start(redirect_uri)
    except SeamlessOAuthError as e:
        raise HTTPException(503, str(e))
    return RedirectResponse(auth_url)


@app.get("/api/seamless/oauth/callback")
def seamless_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    if error:
        return RedirectResponse("/?seamless=denied")
    if not code or not state:
        raise HTTPException(400, "missing OAuth code or state")
    try:
        seamless_oauth_complete(state, code)
    except SeamlessOAuthError:
        return RedirectResponse("/?seamless=error")
    return RedirectResponse("/?seamless=connected")


@app.get("/api/seamless/setup")
def seamless_setup_route(request: Request):
    base = str(request.base_url).rstrip("/")
    status = seamless_status()
    return {
        **status,
        "redirect_uri": seamless_oauth_redirect_uri(base),
        "oauth_configured": seamless_oauth_client_ready(),
        "can_connect": seamless_oauth_client_ready() and not status.get("connected"),
    }


# ---------------------------------------------------------------- notifications


@app.get("/api/notifications")
def notifications(unread_only: bool = False):
    items = get_notifications(unread_only=unread_only)
    unread = sum(1 for n in items if not n["read"]) if not unread_only else len(items)
    return {"notifications": items, "unread": unread}


class MarkReadRequest(BaseModel):
    ids: list[int] | None = None  # None = mark all


@app.post("/api/notifications/mark-read")
def notifications_mark_read(body: MarkReadRequest):
    mark_notifications_read(body.ids)
    return {"ok": True}


@app.get("/api/agents/{agent}/export.csv")
def export(agent: str):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    tmp = Path(tempfile.gettempdir()) / f"{agent}-leads.csv"
    export_csv(tmp, agent=agent)
    return FileResponse(tmp, media_type="text/csv", filename=f"{agent}-leads.csv")


class PromptUpdate(BaseModel):
    qualify_system: str | None = Field(default=None, max_length=12000)
    qualify_extra: str | None = Field(default=None, max_length=8000)
    outreach_system: str | None = Field(default=None, max_length=12000)
    outreach_extra: str | None = Field(default=None, max_length=8000)
    analyst_system: str | None = Field(default=None, max_length=12000)
    critic_system: str | None = Field(default=None, max_length=12000)


@app.get("/api/agents/{agent}/prompts")
def agent_prompts(agent: str):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    return get_prompt_settings(agent)


@app.put("/api/agents/{agent}/prompts")
def update_agent_prompts(agent: str, body: PromptUpdate):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    return save_prompt_settings(agent, body.model_dump())


@app.get("/api/agents/{agent}/today")
def agent_today(agent: str):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    from .run_costs import cost_dashboard
    return {"today": today_stats(agent), "costs": cost_dashboard(agent)}


@app.get("/api/agents/{agent}/signals")
def agent_signals(agent: str, days: int = 7):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    from .signals import SIGNAL_TYPES
    rows = list_signals(agent, days=days, limit=100)
    for r in rows:
        meta = SIGNAL_TYPES.get(r.get("signal_type", ""), {})
        r["label"] = meta.get("label", r.get("signal_type"))
    return {"signals": rows, "count": len(rows)}


@app.get("/api/agents/{agent}/review-queue")
def agent_review_queue(agent: str, limit: int = 50):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    return [enrich_lead(l) for l in get_review_queue(agent, limit=limit)]


class ReviewAction(BaseModel):
    action: str  # approve | reject | regenerate


@app.post("/api/leads/{lead_id}/review")
def lead_review_action(lead_id: int, body: ReviewAction):
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    if body.action == "approve":
        update_lead_status(lead_id, "drafted")
    elif body.action == "reject":
        update_lead_status(lead_id, "skipped")
    elif body.action == "regenerate":
        from .agent import load_agent
        from .pipeline import process_lead
        cfg = load_agent(lead["agent"])
        process_lead(cfg, dict(lead), save=True, agent_name=lead["agent"])
    else:
        raise HTTPException(422, "action must be approve, reject, or regenerate")
    return {"ok": True}


@app.get("/api/agents/{agent}/suppression")
def agent_suppression_list(agent: str):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    return {"entries": list_suppression(agent)}


class SuppressionAdd(BaseModel):
    identifier: str
    scope: str = "email"
    reason: str = ""


@app.post("/api/agents/{agent}/suppression")
def agent_suppression_add(agent: str, body: SuppressionAdd):
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    add_suppression(body.identifier, scope=body.scope, reason=body.reason, agent=agent)
    return {"ok": True}


@app.get("/api/agents/{agent}/negative-list")
def agent_negative_list(agent: str):
    from .negative_list import load_negative_list
    return load_negative_list(agent)


class NegativeListUpdate(BaseModel):
    names: list[str] = []
    patterns: list[str] = []


@app.put("/api/agents/{agent}/negative-list")
def update_negative_list(agent: str, body: NegativeListUpdate):
    import yaml
    path = Path(__file__).parent.parent / "agents" / agent / "config.yaml"
    if not path.exists():
        raise HTTPException(404, "unknown agent")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    cfg["negative_list"] = {"names": body.names, "patterns": body.patterns}
    with open(path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    return {"ok": True}


@app.get("/api/compliance/privacy-posture")
def privacy_posture():
    from fastapi.responses import PlainTextResponse
    from .compliance import privacy_posture_markdown
    return PlainTextResponse(privacy_posture_markdown(), media_type="text/markdown")


class DsarRequest(BaseModel):
    email: str
    action: str = "export"  # export | delete


@app.post("/api/compliance/dsar")
def compliance_dsar(body: DsarRequest):
    from .compliance import dsar_delete, dsar_export
    if body.action == "delete":
        n = dsar_delete(body.email)
        return {"deleted": n}
    return dsar_export(body.email)


@app.post("/api/leads/{lead_id}/actava-deep-dive")
def actava_deep_dive(lead_id: int):
    """Run Actava deep report on a high-priority account."""
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    if not actava_available():
        raise HTTPException(503, "Actava not configured")
    from .actava import run_actava_for_company
    try:
        report = run_actava_for_company(lead["company"], agent=lead.get("agent", "woodway"))
    except Exception as e:
        raise HTTPException(502, str(e)) from e
    set_lead_fields(lead_id, qualification_json=json.dumps(report) if isinstance(report, dict) else str(report)[:8000])
    return {"ok": True, "report": report}


class ClassifyReplyRequest(BaseModel):
    body: str = ""


@app.post("/api/leads/{lead_id}/classify-reply")
def classify_lead_reply(lead_id: int, body: ClassifyReplyRequest):
    from .reply_classify import process_reply_for_lead
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    return process_reply_for_lead(lead_id, body.body, agent=lead.get("agent", "woodway"))


@app.get("/api/agents/{agent}/funnel")
def agent_funnel(agent: str):
    from .funnel_metrics import latest_funnel, live_funnel_snapshot
    return {
        "live": live_funnel_snapshot(agent),
        "runs": latest_funnel(agent, limit=10),
    }


class QaRequest(BaseModel):
    action: str
    notes: str | None = None
    edited_subject: str | None = None
    edited_body: str | None = None


@app.get("/api/agents/{agent}/qa-queue")
def agent_qa_queue(agent: str, limit: int = 25):
    from .qa import list_qa_queue
    return {"leads": list_qa_queue(agent, limit=limit)}


@app.post("/api/leads/{lead_id}/qa")
def lead_qa(lead_id: int, body: QaRequest):
    from .qa import record_qa
    lead = get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    result = record_qa(
        lead_id,
        body.action,
        agent=lead.get("agent", "woodway"),
        notes=body.notes,
        edited_subject=body.edited_subject,
        edited_body=body.edited_body,
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error") or "qa failed")
    return result


# Static frontend (mounted last so /api wins)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="static")


# ---------------------------------------------------------------- background reply scanner

REPLY_SCAN_MINUTES = int(os.getenv("REPLY_SCAN_MINUTES", "10"))
_scanner_started = threading.Event()


def _reply_scan_loop():
    import time

    from .db import record_run

    while True:
        time.sleep(REPLY_SCAN_MINUTES * 60)
        try:
            if gmail_status()["connected"]:
                result = scan_replies()
                record_run("reply_scan", ok=True,
                           summary=f"{result['replies']} new replies of {result['checked']} checked",
                           started_at=_now())
                if result["replies"]:
                    logger.info("Gmail reply scan: %s new replies", result["replies"])
            if microsoft_status()["connected"]:
                ms_result = ms_scan_replies()
                record_run(
                    "reply_scan_microsoft",
                    ok=True,
                    summary=f"{ms_result['replies']} new replies of {ms_result['checked']} checked",
                    started_at=_now(),
                )
                if ms_result["replies"]:
                    logger.info("Microsoft reply scan: %s new replies", ms_result["replies"])
        except Exception:
            logger.exception("Background reply scan failed")


def _start_reply_scanner():
    if REPLY_SCAN_MINUTES > 0 and not _scanner_started.is_set():
        _scanner_started.set()
        threading.Thread(target=_reply_scan_loop, daemon=True, name="reply-scanner").start()
        logger.info("Reply scanner running every %d min", REPLY_SCAN_MINUTES)


def main():
    import uvicorn

    class _QuietAccessLogFilter(logging.Filter):
        _SKIP = (
            "GET /api/health",
            "GET /api/notifications",
            "GET /api/automation",
            "GET /api/jobs/",
            "GET /style.css",
            "GET /app.js",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            return not any(skip in msg for skip in self._SKIP)

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessLogFilter())
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8400"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
