"""Daily automation run — designed to be invoked by cron.

For each agent:
  1. Contact search (Apollo/PDL) → import new ICP contacts
  2. Process imported → qualify + draft outreach
  3. Create Gmail drafts for hot/warm leads with emails (drafts only — never sends)
Then scan Gmail for replies to previously sent outreach and notify.

Install cron only if forced: FORCE_CRON_INSTALL=1 ./scripts/setup_cron.sh
Remove cron with:          ./scripts/remove_cron.sh
Woodway/Keira stay off unless DAILY_RUN_WOODWAY / DAILY_RUN_KEIRA are true.
Run manually with:          python -m src.daily
"""

import logging
import os
from datetime import datetime, timezone

from . import env  # noqa: F401 — load .env
from .agent import run_process_imported
from .config_check import validate_config
from .contacts import search_and_import_contacts
from .db import create_notification, record_run

logger = logging.getLogger("daily")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# Woodway + Keira default OFF — manual pipeline only until cron is re-enabled.
def _daily_agents() -> list[str]:
    agents: list[str] = []
    if _env_bool("DAILY_RUN_WOODWAY", False):
        agents.append("woodway")
    if _env_bool("DAILY_RUN_FONEX", True):
        agents.append("fonex")
    if _env_bool("DAILY_RUN_KEIRA", False):
        agents.append("keira")
    return agents


AGENTS = ["woodway", "fonex", "keira"]  # full set; run_daily uses _daily_agents()

CONTACT_LIMIT = int(os.getenv("DAILY_CONTACT_LIMIT", "50"))
PROCESS_LIMIT = int(os.getenv("DAILY_PROCESS_LIMIT", "50"))
WOODWAY_DAILY_LIMIT = int(os.getenv("DAILY_WOODWAY_LIMIT", str(CONTACT_LIMIT)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_gmail_drafts(agent: str) -> tuple[int, int]:
    """Create mailbox drafts for drafted leads that have an email but no draft yet.

    Woodway prefers Microsoft 365 / Outlook when connected; others prefer Gmail.
    """
    from .woodway_pipeline import create_mailbox_drafts

    prefer_ms = agent == "woodway"
    result = create_mailbox_drafts(agent, prefer_microsoft=prefer_ms)
    return result.get("created", 0), result.get("failed", 0)


def run_daily() -> dict:
    started = _now()
    summary_parts: list[str] = []
    ok = True

    cfg = validate_config()
    for warning in cfg["warnings"]:
        logger.warning("Config warning: %s", warning)
    if not cfg["ok"]:
        summary = "Config invalid: " + "; ".join(cfg["issues"])
        logger.error(summary)
        record_run("daily", ok=False, summary=summary, started_at=started)
        create_notification(f"Daily run skipped — {summary}")
        return {"ok": False, "summary": summary, "config": cfg}

    agents = _daily_agents()
    if not agents:
        summary = (
            "No agents enabled for daily auto-run "
            "(DAILY_RUN_WOODWAY / DAILY_RUN_KEIRA default off; set true to re-enable)"
        )
        logger.warning(summary)
        record_run("daily", ok=True, summary=summary, started_at=started)
        return {"ok": True, "summary": summary, "skipped": True, "agents": []}

    logger.info("Daily agents: %s", ", ".join(agents))
    for agent in agents:
        agent_summary = []

        # Woodway: full company→people→draft pipeline (Anthropic or Actava)
        if agent == "woodway":
            try:
                from .woodway_pipeline import run_woodway_pipeline

                result = run_woodway_pipeline(
                    limit=WOODWAY_DAILY_LIMIT,
                    use_llm=True,
                    create_drafts=True,
                    on_progress=lambda m: logger.info("[%s] %s", agent, m),
                )
                steps = result.get("steps") or {}
                agent_summary.append(
                    f"pipeline: {len((steps.get('digest') or {}).get('companies') or [])} companies, "
                    f"{(steps.get('contacts') or {}).get('imported', 0)} contacts, "
                    f"{(steps.get('process') or {}).get('count', 0)} processed, "
                    f"{(steps.get('drafts') or {}).get('created', 0)} drafts, "
                    f"{(steps.get('sequences') or {}).get('advanced', 0)} sequence touches"
                )
            except Exception as e:
                ok = False
                logger.exception("[%s] woodway pipeline failed", agent)
                agent_summary.append(f"pipeline failed: {e}")
            summary_parts.append(f"{agent}: " + ", ".join(agent_summary))
            continue

        # Keira: company-first M&A intelligence pipeline
        if agent == "keira":
            try:
                from .keira_pipeline import run_keira_pipeline

                result = run_keira_pipeline(
                    limit=CONTACT_LIMIT,
                    use_llm=True,
                    create_drafts=True,
                    on_progress=lambda m: logger.info("[%s] %s", agent, m),
                )
                steps = result.get("steps") or {}
                gates = steps.get("gates") or {}
                rediscovery = steps.get("rediscovery") or {}
                agent_summary.append(
                    f"pipeline: {gates.get('survivors', 0)} survivors "
                    f"({gates.get('rejected', 0)} rejected, "
                    f"{gates.get('memory_skipped', 0)} memory-skipped), "
                    f"rediscovery {rediscovery.get('requeued', 0)}, "
                    f"{(steps.get('process') or {}).get('count', 0)} processed, "
                    f"{(steps.get('drafts') or {}).get('created', 0)} drafts"
                )
            except Exception as e:
                ok = False
                logger.exception("[%s] keira pipeline failed", agent)
                agent_summary.append(f"pipeline failed: {e}")
            summary_parts.append(f"{agent}: " + ", ".join(agent_summary))
            continue

        try:
            contacts = search_and_import_contacts(
                agent, limit=CONTACT_LIMIT, on_progress=lambda m: logger.info("[%s] %s", agent, m)
            )
            imported = contacts.get("imported", 0)
            errors = len(contacts.get("errors") or [])
            part = f"{imported} contacts imported"
            if errors:
                part += f", {errors} errors"
            agent_summary.append(part)
        except Exception as e:
            ok = False
            logger.exception("[%s] contact search failed", agent)
            agent_summary.append(f"contact search failed: {e}")

        try:
            processed = run_process_imported(
                agent, limit=PROCESS_LIMIT,
                on_progress=lambda m: logger.info("[%s] %s", agent, m),
            )
            part = f"{processed['count']} processed"
            if processed.get("failed_count"):
                ok = False
                part += f", {processed['failed_count']} failed"
            agent_summary.append(part)
        except Exception as e:
            ok = False
            logger.exception("[%s] processing failed", agent)
            agent_summary.append(f"processing failed: {e}")

        try:
            drafts, draft_failed = _create_gmail_drafts(agent)
            if drafts:
                agent_summary.append(f"{drafts} mailbox drafts created")
            if draft_failed:
                ok = False
                agent_summary.append(f"{draft_failed} draft failures")
        except Exception as e:
            ok = False
            logger.exception("[%s] drafting failed", agent)
            agent_summary.append(f"drafts failed: {e}")

        summary_parts.append(f"{agent}: " + ", ".join(agent_summary))

    # Reply scan (read-only)
    try:
        from .gmail_api import gmail_status, scan_replies
        from .microsoft_mail_api import microsoft_status, scan_replies as ms_scan_replies

        reply_bits = []
        if gmail_status()["connected"]:
            scan = scan_replies()
            reply_bits.append(f"gmail {scan['replies']} new of {scan['checked']} checked")
        if microsoft_status()["connected"]:
            scan = ms_scan_replies()
            reply_bits.append(f"outlook {scan['replies']} new of {scan['checked']} checked")
        if reply_bits:
            summary_parts.append("replies: " + "; ".join(reply_bits))
        else:
            summary_parts.append("replies: no mailbox connected")
    except Exception as e:
        ok = False
        logger.exception("reply scan failed")
        summary_parts.append(f"reply scan failed: {e}")

    # Advance due sequence touches for all agents
    try:
        from .sequences import advance_due_sequences

        for agent in AGENTS:
            seq = advance_due_sequences(agent=agent, limit=PROCESS_LIMIT, create_drafts=True)
            if seq.get("advanced") or seq.get("linkedin_queued"):
                summary_parts.append(
                    f"{agent} sequences: {seq.get('advanced', 0)} drafted, "
                    f"{seq.get('linkedin_queued', 0)} linkedin"
                )
    except Exception as e:
        logger.warning("sequence advance failed: %s", e)

    # PII retention — purge stale unengaged contacts (RETENTION_MONTHS, default 18)
    try:
        from .compliance import purge_stale_contacts

        purged = purge_stale_contacts()
        if purged:
            summary_parts.append(f"retention purge: {purged} stale contacts")
            logger.info("Purged %s stale contacts (RETENTION_MONTHS)", purged)
    except Exception as e:
        logger.warning("retention purge failed: %s", e)

    summary = " | ".join(summary_parts)
    record_run("daily", ok=ok, summary=summary, started_at=started)
    create_notification(f"Daily run finished — {summary}")
    logger.info("Daily run complete: %s", summary)
    return {"ok": ok, "summary": summary}


def main():
    import argparse
    import fcntl
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="JayAgents daily automation run (contact search → qualify → draft → reply scan)",
        epilog="Install the schedule with: ./scripts/setup_cron.sh",
    )
    parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    lock_path = Path(os.getenv("DAILY_LOCK_PATH", Path(__file__).parent.parent / "logs" / "daily.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.warning("Another daily run is in progress — skipping")
        raise SystemExit(0)

    try:
        result = run_daily()
        raise SystemExit(0 if result["ok"] else 1)
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    main()
