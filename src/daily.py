"""Daily automation run — designed to be invoked by cron.

For each agent:
  1. Contact search (Apollo/PDL) → import new ICP contacts
  2. Process imported → qualify + draft outreach
  3. Create Gmail drafts for hot/warm leads with emails (drafts only — never sends)
Then scan Gmail for replies to previously sent outreach and notify.

Install the cron job with:  ./scripts/setup_cron.sh
Run manually with:          python -m src.daily
"""

import logging
import os
from datetime import datetime, timezone

from . import env  # noqa: F401 — load .env
from .agent import run_process_imported
from .config_check import validate_config
from .contacts import search_and_import_contacts
from .db import create_notification, get_leads, record_run, set_lead_fields

logger = logging.getLogger("daily")

AGENTS = ["woodway", "fonex", "keira"]

CONTACT_LIMIT = int(os.getenv("DAILY_CONTACT_LIMIT", "10"))
PROCESS_LIMIT = int(os.getenv("DAILY_PROCESS_LIMIT", "25"))
DRAFT_MIN_SCORE = int(os.getenv("DAILY_DRAFT_MIN_SCORE", "50"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_gmail_drafts(agent: str) -> tuple[int, int]:
    """Create Gmail drafts for drafted leads that have an email but no Gmail draft yet."""
    from .gmail_api import GmailError, create_draft, gmail_status

    if not gmail_status()["connected"]:
        return 0, 0

    created = 0
    failed = 0
    for lead in get_leads(agent=agent, status="drafted"):
        if not lead.get("email") or lead.get("gmail_draft_id"):
            continue
        if (lead.get("score") or 0) < DRAFT_MIN_SCORE or not lead.get("outreach_body"):
            continue
        try:
            result = create_draft(
                lead["email"],
                lead.get("outreach_subject") or "Quick question",
                lead["outreach_body"],
            )
        except GmailError as e:
            logger.warning("Draft failed for lead %s: %s", lead["id"], e)
            failed += 1
            continue
        set_lead_fields(
            lead["id"],
            gmail_draft_id=result["draft_id"],
            gmail_thread_id=result["thread_id"],
            gmail_message_id=result["message_id"],
        )
        created += 1
        logger.info("Gmail draft created for %s <%s>", lead.get("contact_name"), lead["email"])
    return created, failed


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

    for agent in AGENTS:
        agent_summary = []
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
                agent_summary.append(f"{drafts} Gmail drafts created")
            if draft_failed:
                ok = False
                agent_summary.append(f"{draft_failed} draft failures")
        except Exception as e:
            ok = False
            logger.exception("[%s] Gmail drafting failed", agent)
            agent_summary.append(f"gmail drafts failed: {e}")

        summary_parts.append(f"{agent}: " + ", ".join(agent_summary))

    # Reply scan (read-only)
    try:
        from .gmail_api import gmail_status, scan_replies

        if gmail_status()["connected"]:
            scan = scan_replies()
            summary_parts.append(f"replies: {scan['replies']} new of {scan['checked']} checked")
        else:
            summary_parts.append("replies: gmail not connected")
    except Exception as e:
        ok = False
        logger.exception("reply scan failed")
        summary_parts.append(f"reply scan failed: {e}")

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
