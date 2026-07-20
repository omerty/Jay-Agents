"""Web dashboard for demoing all three agents.

Run:  python -m src.web   →  http://localhost:8400
"""

import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import env  # noqa: F401 — loads .env
from .agent import load_agent, run_discover_workflow, run_process_imported, run_requalify_all, run_workflow
from .config_check import validate_config
from .contact_channel import enrich_lead
from .contacts import ContactsError, contacts_status, hunter_research_lead, resolve_contacts_provider, search_and_import_contacts
from .seamless import seamless_status
from .seamless_api import seamless_available
from .actava import actava_status_export
from .actava_api import actava_available
from .db import (
    STATUSES,
    export_csv,
    get_connection,
    get_lead,
    get_leads,
    get_notifications,
    init_db,
    last_run,
    mark_notifications_read,
    set_lead_fields,
    stats,
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

# ---------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_FINISHED_JOBS = 50


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


def _finish_job(job_id: str, *, result=None, error=None):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "error" if error else "done"
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
            _finish_job(job_id, result=result)
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
    return {
        "schedule": "daily at 00:00 via cron (scripts/setup_cron.sh)",
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
    return enrich_lead(dict(row))


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
    mode: str  # discover | process_imported | requalify_all | single | contact_search | pdl_search
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
    if use_llm and body.mode in ("discover", "process_imported", "requalify_all", "single"):
        llm = check_llm()
        if not llm["ok"]:
            raise HTTPException(503, f"LLM unavailable: {llm['detail']} — fix it or use Demo mode (mock)")

    # Validate before creating the job — an early HTTPException must not leave
    # a phantom "running" job that blocks the agent
    if body.mode not in ("discover", "process_imported", "requalify_all", "single", "contact_search", "pdl_search"):
        raise HTTPException(400, f"unknown mode: {body.mode}")
    if body.mode == "single" and not (body.prospect and body.prospect.strip()):
        raise HTTPException(400, "prospect required for single mode")
    if body.mode in ("contact_search", "pdl_search"):
        if agent == "keira" and seamless_available():
            pass
        elif agent == "keira" and actava_available():
            pass
        else:
            c = contacts_status()
            if not c["configured"]:
                key = "APOLLO_API_KEY" if c["provider"] == "apollo" else "PDL_API_KEY"
                raise HTTPException(503, f"{key} not set — add it to .env")

    job_id = _new_job(agent, body.mode)
    log = lambda msg: _job_log(job_id, msg)

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
            if provider == "apollo":
                summary += f", {result.get('with_email', 0)} with email (search was free)"
            else:
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
        gmail_thread_id=result["thread_id"],
        gmail_message_id=result["message_id"],
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
    qualify_system: str | None = Field(default=None, max_length=4000)
    qualify_extra: str | None = Field(default=None, max_length=4000)
    outreach_system: str | None = Field(default=None, max_length=4000)
    outreach_extra: str | None = Field(default=None, max_length=4000)


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
            if not gmail_status()["connected"]:
                continue
            result = scan_replies()
            record_run("reply_scan", ok=True,
                       summary=f"{result['replies']} new replies of {result['checked']} checked",
                       started_at=_now())
            if result["replies"]:
                logger.info("Reply scan: %s new replies", result["replies"])
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
