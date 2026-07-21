import logging
import os
import time
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

AGENTS_DIR = Path(__file__).parent.parent / "agents"

PROCESS_DELAY = float(os.getenv("PROCESS_DELAY", os.getenv("LLM_CALL_DELAY", "2.5")))


def _interruptible_sleep(seconds: float, emit):
    """Sleep in short chunks so cancel requests are picked up promptly."""
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        emit("")
        time.sleep(min(0.5, end - time.monotonic()))


def load_agent(name: str) -> dict:
    config_path = AGENTS_DIR / name / "config.yaml"
    if not config_path.exists():
        available = [p.name for p in AGENTS_DIR.iterdir() if p.is_dir()]
        raise ValueError(f"Unknown agent '{name}'. Available: {', '.join(available)}")
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}

    prompts_path = config_path.parent / "prompts.yaml"
    if prompts_path.exists():
        with open(prompts_path) as f:
            override = yaml.safe_load(f) or {}
        if override:
            cfg["prompts"] = {**(cfg.get("prompts") or {}), **override}
    return cfg


def run_workflow(
    agent_name: str,
    prospect: str,
    *,
    use_llm: bool = True,
    use_research: bool = True,
    contact: dict | None = None,
) -> dict:
    from .pipeline import process_lead

    _, company = _split_prospect(prospect)
    lead = {"company": company, "prospect": prospect, "source": "manual", **(contact or {})}
    config = load_agent(agent_name)
    result = process_lead(config, lead, use_llm=use_llm, use_research=use_research, agent_name=agent_name)
    result["agent"] = config
    return result


def _split_prospect(prospect: str) -> tuple[str, str]:
    if " at " in prospect.lower():
        idx = prospect.lower().rfind(" at ")
        return prospect[:idx].strip(), prospect[idx + 4:].strip()
    return "", prospect.strip()


def run_discover_workflow(
    agent_name: str,
    *,
    limit: int = 5,
    use_llm: bool = True,
    max_queries: int = 8,
    draft_outreach_for_top: bool = True,
    on_progress=None,
) -> dict:
    from .discover import discover_prospects

    config = load_agent(agent_name)
    discover_cfg = config.get("discover", {})
    max_queries = discover_cfg.get("max_queries", max_queries)

    discovery = discover_prospects(
        config,
        limit=limit,
        use_llm=use_llm,
        max_queries=max_queries,
        agent_name=agent_name,
        save_to_db=True,
        draft_outreach_for_top=draft_outreach_for_top,
        on_progress=on_progress,
    )

    top_outreach = None
    if discovery["qualified"]:
        top_outreach = discovery["qualified"][0].get("outreach")

    return {
        "agent": config,
        "mode": "discover",
        "discovery": discovery,
        "top_outreach": top_outreach,
        "skipped_duplicates": discovery.get("skipped_duplicates", 0),
    }


def run_process_imported(
    agent_name: str,
    *,
    limit: int = 10,
    use_llm: bool = True,
    on_progress=None,
) -> dict:
    from .db import get_leads_by_statuses
    from .pipeline import process_lead

    emit = on_progress or (lambda msg: None)
    config = load_agent(agent_name)
    pending = get_leads_by_statuses(["imported"], agent=agent_name, limit=limit)
    emit(f"{len(pending)} imported contacts pending")

    processed = []
    failed = []
    for i, row in enumerate(pending, 1):
        lead = dict(row)
        who = lead.get("contact_name") or lead.get("company", "lead")
        emit(f"Qualifying {who} ({i}/{len(pending)})…")
        try:
            result = process_lead(
                config, lead,
                use_llm=use_llm,
                draft_outreach=True,
                agent_name=agent_name,
                save=True,
            )
            processed.append({**lead, **result})
        except Exception as e:
            logger.exception("Failed to process lead %s (%s)", lead.get("id"), who)
            failed.append({"lead_id": lead.get("id"), "company": lead.get("company"), "error": str(e)})
            emit(f"Failed {who}: {e}")
        if i < len(pending) and PROCESS_DELAY > 0:
            _interruptible_sleep(PROCESS_DELAY, emit)

    processed.sort(key=lambda x: x["qualification"]["score"], reverse=True)
    return {
        "agent": config,
        "mode": "process_imported",
        "processed": processed,
        "count": len(processed),
        "failed": failed,
        "failed_count": len(failed),
    }


REQUALIFY_STATUSES = ("discovered", "imported", "qualified", "drafted")
REQUALIFY_DELAY = float(os.getenv("REQUALIFY_DELAY", "2.5"))


def run_requalify_all(
    agent_name: str,
    *,
    limit: int = 500,
    use_llm: bool = True,
    use_research: bool = True,
    draft_outreach: bool = True,
    on_progress=None,
) -> dict:
    """Re-run qualify + outreach for existing leads (skips emailed/replied/skipped)."""
    from .db import get_leads_by_statuses
    from .pipeline import process_lead

    emit = on_progress or (lambda msg: None)
    config = load_agent(agent_name)
    pending = get_leads_by_statuses(list(REQUALIFY_STATUSES), agent=agent_name, limit=limit)
    emit(f"{len(pending)} leads to re-qualify")

    processed = []
    failed = []
    for i, row in enumerate(pending, 1):
        lead = dict(row)
        who = lead.get("contact_name") or lead.get("company", "lead")
        emit(f"Re-qualifying {who} ({i}/{len(pending)})…")
        try:
            result = process_lead(
                config,
                lead,
                use_llm=use_llm,
                use_research=use_research,
                draft_outreach=draft_outreach,
                agent_name=agent_name,
                save=True,
            )
            processed.append({**lead, **result})
        except Exception as e:
            logger.exception("Failed to re-qualify lead %s (%s)", lead.get("id"), who)
            failed.append({"lead_id": lead.get("id"), "company": lead.get("company"), "error": str(e)})
            emit(f"Failed {who}: {e}")
        if i < len(pending) and REQUALIFY_DELAY > 0:
            _interruptible_sleep(REQUALIFY_DELAY, emit)

    processed.sort(key=lambda x: x["qualification"]["score"], reverse=True)
    return {
        "agent": config,
        "mode": "requalify_all",
        "processed": processed,
        "count": len(processed),
        "failed": failed,
        "failed_count": len(failed),
    }
