"""Keira M&A intelligence pipeline — company-first, credits last.

Flow:
  1. Discover candidates (stack Seamless multi-query + Actava + Anthropic/web)
  2. Cheap gates: geo / ownership / owner / size / succession tiers
  3. Budget allocate by ICP fit × timing × confidence
  4. Claude research analyst (top N) → re-qualify
  5. Critic gates drafts only (hard reject still skips)
  6. Seamless contact research for ALL non-rejected gate survivors
  7. Process → mailbox drafts only for draft-approved (human send)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

import yaml

from .db import build_prospect_string, get_leads_by_statuses, upsert_lead
from .keira_budget import allocate_budget, research_priority
from .keira_intelligence import (
    build_company_intelligence,
    build_mna_thesis,
    qualify_keira_intelligence,
)
from .keira_memory import (
    fingerprint_lead,
    remember_research,
    should_skip_research,
    status_from_qualification,
)
from .keira_research import (
    merge_analyst_into_intel,
    run_lead_critic,
    run_succession_analyst,
)

logger = logging.getLogger(__name__)
AGENTS_DIR = Path(__file__).parent.parent / "agents"


def _config() -> dict:
    with open(AGENTS_DIR / "keira" / "config.yaml") as f:
        return yaml.safe_load(f)


def _load_keira_seeds() -> list[dict]:
    path = AGENTS_DIR / "keira" / "seed_companies.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return []
    out: list[dict] = []
    for item in data.get("companies") or []:
        if isinstance(item, str) and item.strip():
            out.append({
                "company": item.strip(),
                "source": "icp_seed",
                "signal": "ICP seed — known Eastern Ontario owner company",
                "status": "imported",
            })
            continue
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("company") or "").strip()
        if not name:
            continue
        out.append({
            "company": name,
            "company_city": item.get("city") or item.get("company_city"),
            "source": "icp_seed",
            "signal": item.get("signal") or "ICP seed — known Eastern Ontario owner company",
            "status": "imported",
        })
    return out


def _research_budget_dead(budget_note: str | None) -> bool:
    from .seamless_budget import (
        credits_used_this_month,
        credits_used_today,
        daily_hard_cap,
        monthly_budget,
        research_enabled,
    )

    if not research_enabled():
        return True
    if credits_used_this_month() >= monthly_budget():
        return True
    if credits_used_today() >= daily_hard_cap():
        return True
    note = (budget_note or "").lower()
    if "disabled" in note or "monthly" in note or "hard cap" in note:
        return True
    return False


def _keira_caps(config: dict, limit: int) -> tuple[int, int, str | None]:
    research = (config.get("keira") or {}).get("research") or {}
    claude_cap = int(
        os.getenv("KEIRA_CLAUDE_RESEARCH_CAP")
        or research.get("max_claude_research")
        or min(limit, 8)
    )
    from .seamless_budget import allocate_research_slots, caps_disabled, max_research_per_run

    if caps_disabled():
        seamless_cap = max(limit * 20, max_research_per_run("keira"), 500)
    else:
        seamless_cap = int(
            os.getenv("KEIRA_SEAMLESS_ENRICH_CAP")
            or research.get("max_seamless_credits")
            or min(limit, 5)
        )
    budget_note: str | None = None
    # Never exceed live Seamless budget guards (no-op when SEAMLESS_CAPS_DISABLED)
    try:
        allowed, note = allocate_research_slots(seamless_cap, agent="keira")
        if allowed <= 0:
            budget_note = note or "no Seamless research budget remaining"
            seamless_cap = 0
        else:
            seamless_cap = min(seamless_cap, allowed)
            if note:
                budget_note = note
    except Exception:
        pass
    return max(0, claude_cap), max(0, seamless_cap), budget_note


def run_keira_pipeline(
    *,
    limit: int = 10,
    use_llm: bool = True,
    create_drafts: bool = True,
    on_progress=None,
) -> dict:
    """Company-first Keira run. Spends Seamless credits only after cheap + critic gates."""
    emit = on_progress or (lambda msg: None)
    agent = "keira"
    config = _config()
    claude_cap, seamless_cap, budget_note = _keira_caps(config, limit)
    summary: dict = {
        "agent": agent,
        "run_id": str(uuid.uuid4())[:8],
        "steps": {},
        "ok": True,
        "alerts": [],
    }
    if seamless_cap == 0 and budget_note:
        from .seamless_budget import format_budget_alert_message, notify_seamless_budget_exhausted

        alert = notify_seamless_budget_exhausted(
            budget_note, agent=agent, context="pipeline start",
        )
        summary["alerts"].append(alert)
        summary["budget_alert"] = alert
        emit(format_budget_alert_message(budget_note, agent=agent))
        emit(f"Seamless research budget empty at start — {budget_note}")

    from .pipeline_preflight import PipelinePreflightError, assert_mailbox_ready

    try:
        mb = assert_mailbox_ready(create_drafts=create_drafts)
        if mb.get("ready"):
            emit("Preflight — mailbox ready for drafts")
    except PipelinePreflightError as e:
        emit(f"Preflight abort — {e}")
        summary["ok"] = False
        summary["error"] = str(e)
        summary["preflight"] = {"mailbox": False, "detail": str(e)}
        return summary

    # ---- 1. Discovery (stack Seamless + Actava + Anthropic/web) ----
    emit("Step 1/7 — Discovering Eastern Ontario owner-operated companies…")
    from .seamless_api import seamless_available
    from .actava_api import actava_available

    research_cfg = (config.get("keira") or {}).get("research") or {}
    stack_actava = research_cfg.get("stack_actava", True)
    if isinstance(stack_actava, str):
        stack_actava = stack_actava.lower() not in ("0", "false", "no")
    stack_anthropic = research_cfg.get("stack_anthropic", True)
    if isinstance(stack_anthropic, str):
        stack_anthropic = stack_anthropic.lower() not in ("0", "false", "no")
    # Env overrides
    if os.getenv("KEIRA_STACK_ACTAVA", "").strip():
        stack_actava = os.getenv("KEIRA_STACK_ACTAVA", "true").lower() not in ("0", "false", "no")
    if os.getenv("KEIRA_STACK_ANTHROPIC", "").strip():
        stack_anthropic = os.getenv("KEIRA_STACK_ANTHROPIC", "true").lower() not in ("0", "false", "no")

    discovered: list[dict] = []
    discovery_meta: dict = {"providers": [], "sources": {}}
    target_n = max(limit * 8, 40)

    def _merge_candidates(rows: list[dict], *, source: str) -> int:
        nonlocal discovered
        seen = {(d.get("company") or "").lower() for d in discovered}
        added = 0
        for row in rows:
            co = (row.get("company") or "").strip()
            if not co:
                continue
            key = co.lower()
            if key in seen:
                continue
            seen.add(key)
            item = dict(row)
            item.setdefault("source", source)
            discovered.append(item)
            added += 1
        return added

    # Prefer Actava first — highest-quality Eastern Ontario owner research
    if actava_available() and stack_actava:
        from .actava import search_and_import_actava

        emit("Discovery — Actava primary (Keira ICP prompt)…")
        try:
            actava_meta = search_and_import_actava(
                agent, limit=max(limit * 2, 15), on_progress=emit,
            )
            actava_rows = list(actava_meta.get("companies") or [])
            for c in actava_rows:
                c.setdefault("source", "actava")
                # Ensure signal text carries through for succession gates
                if c.get("signal") and not str(c.get("signal")).startswith("Actava"):
                    c["signal"] = f"Actava — {c['signal']}"
            n = _merge_candidates(actava_rows, source="actava")
            discovery_meta["providers"].append("actava")
            discovery_meta["sources"]["actava"] = {
                "candidates": n,
                "imported": actava_meta.get("imported"),
                "searched": actava_meta.get("searched"),
                "mode": actava_meta.get("mode"),
            }
        except Exception as e:
            logger.warning("Actava discovery failed: %s", e)
            discovery_meta["sources"]["actava"] = {"error": str(e)}

    if seamless_available():
        from .seamless import discover_keira_candidates

        emit("Discovery — Seamless free multi-query (widen pool)…")
        seamless_meta = discover_keira_candidates(
            agent, limit=target_n, on_progress=emit,
        )
        n = _merge_candidates(seamless_meta.get("candidates") or [], source="seamless")
        discovery_meta["providers"].append("seamless")
        discovery_meta["sources"]["seamless"] = {
            "candidates": n,
            "searched": seamless_meta.get("searched"),
            "mode": seamless_meta.get("mode"),
            "queries": seamless_meta.get("queries"),
        }
        discovery_meta = {
            **discovery_meta,
            **{k: seamless_meta.get(k) for k in ("credits_used",) if k in seamless_meta},
        }

    # Anthropic/web when the *new* unique pool is thin (already-in-DB rows hide live yield)
    from .db import company_seen

    fresh = [
        d for d in discovered
        if d.get("company") and not company_seen(d["company"], agent, domain=d.get("company_domain"))
    ]
    need_web = stack_anthropic and (len(fresh) < max(limit, 6) or not discovered)
    if need_web:
        from .discover import search_and_import_companies

        emit(
            "Discovery — stacking Anthropic/web company search…"
            if discovered
            else "Discovery — falling back to web + Claude company search…"
        )
        try:
            web_meta = search_and_import_companies(
                agent, limit=max(limit * 2, 15), on_progress=emit,
            )
            web_rows = web_meta.get("companies") or []
            for c in web_rows:
                c.setdefault("source", "discover")
            n = _merge_candidates(web_rows, source="discover")
            discovery_meta["providers"].append("anthropic_web")
            discovery_meta["sources"]["anthropic_web"] = {
                "candidates": n,
                "imported": web_meta.get("imported"),
            }
        except Exception as e:
            logger.warning("Anthropic/web discovery failed: %s", e)
            discovery_meta["sources"]["anthropic_web"] = {"error": str(e)}

    # Prefer Actava-sourced rows first for gate/budget ordering
    from .keira_intelligence import safe_int

    discovered.sort(
        key=lambda r: (
            0 if str(r.get("source") or "").startswith("actava") else 1,
            -safe_int(r.get("score"), 0),
        )
    )

    discovery_meta["provider"] = "+".join(discovery_meta["providers"]) or "none"
    discovery_meta["mode"] = "stacked" if len(discovery_meta["providers"]) > 1 else (
        discovery_meta["providers"][0] if discovery_meta["providers"] else "none"
    )

    summary["steps"]["discovery"] = {
        "provider": discovery_meta.get("provider"),
        "mode": discovery_meta.get("mode"),
        "candidates": len(discovered),
        "sources": discovery_meta.get("sources"),
        "raw": {
            k: discovery_meta.get(k)
            for k in ("imported", "searched", "credits_used", "mode")
            if k in discovery_meta
        },
    }
    emit(
        f"Discovery — {len(discovered)} candidates from "
        f"{discovery_meta.get('provider') or 'none'} for cheap gates"
    )

    live_n = len(discovered)
    if live_n < max(3, limit // 2):
        seeds = _load_keira_seeds()
        if seeds:
            emit(f"Discovery thin ({live_n}) — seeding {len(seeds)} known ICP accounts…")
            n = _merge_candidates(seeds, source="icp_seed")
            discovery_meta["sources"]["icp_seed"] = {"candidates": n}
            if n:
                discovery_meta["providers"].append("icp_seed")
            summary["steps"]["discovery"]["seeded"] = n
            summary["steps"]["discovery"]["candidates"] = len(discovered)

    # Always merge existing imported Keira leads (e.g. Actava-only button runs)
    existing_imported = [
        dict(r) for r in get_leads_by_statuses(
            ["imported", "awaiting_contact"], agent=agent, limit=max(limit * 3, 30),
        )
    ]
    seen = {(d.get("company") or "").lower() for d in discovered}
    merged_existing = 0
    for row in existing_imported:
        key = (row.get("company") or "").lower()
        if key and key not in seen:
            discovered.append(row)
            seen.add(key)
            merged_existing += 1
    if merged_existing:
        emit(f"Merged {merged_existing} existing imported lead(s) into gate queue")
        summary["steps"]["discovery"]["merged_existing"] = merged_existing
        summary["steps"]["discovery"]["candidates"] = len(discovered)

    # ---- 1b. Optional event rediscovery (known companies due / changed) ----
    rediscovery_step: dict = {"skipped": True}
    if os.getenv("KEIRA_EVENT_REDISCOVERY", "true").lower() not in ("0", "false", "no"):
        try:
            from .keira_events import run_keira_rediscovery

            emit("Step 1b/7 — Event rediscovery / re-score…")
            rediscovery_step = run_keira_rediscovery(
                limit=max(limit, 15), config=config, on_progress=emit,
            )
            # Merge requeued imported leads into discovery pool
            extra = [
                dict(r) for r in get_leads_by_statuses(
                    ["imported"], agent=agent, limit=limit,
                )
                if (r.get("source") or "") == "keira_rediscovery"
            ]
            seen = {(d.get("company") or "").lower() for d in discovered}
            for row in extra:
                if (row.get("company") or "").lower() not in seen:
                    discovered.append(row)
            rediscovery_step["merged"] = len(extra)
        except Exception as e:
            logger.warning("Keira rediscovery failed: %s", e)
            rediscovery_step = {"error": str(e), "skipped": False}
    summary["steps"]["rediscovery"] = rediscovery_step

    # ---- 2. Cheap intelligence gates ----
    emit("Step 2/7 — Company / owner / geo / succession gates…")
    survivors: list[dict] = []
    rejected = 0
    memory_skipped = 0
    # Companies rediscovery just requeued must not be memory-skipped in this same run.
    force_research = {
        (d.get("company") or "").lower()
        for d in (rediscovery_step.get("details") or [])
        if d.get("requeued") and d.get("company")
    }

    for raw in discovered:
        if not raw.get("company"):
            continue
        fp = fingerprint_lead(raw)
        skip = should_skip_research(raw["company"], fingerprint=fp)
        if skip.get("skip") and (raw["company"] or "").lower() not in force_research:
            memory_skipped += 1
            continue

        intel = build_company_intelligence(raw)
        q = qualify_keira_intelligence(intel, config=config)
        priority = research_priority(q, intel)
        q.update(priority)
        thesis = build_mna_thesis(intel, q)

        if q.get("rejected"):
            record = _lead_record(raw, intel, q, thesis, status="skipped")
            rejected += 1
            upsert_lead(record, agent=agent)
            st, reason = status_from_qualification(q)
            remember_research(
                raw["company"], status=st, reason=reason, fingerprint=fp,
                research_priority=int(q.get("research_priority") or 0),
            )
            continue

        survivors.append({
            **_lead_record(raw, intel, q, thesis, status="imported"),
            "_intel": intel,
            "_qual": q,
            "_thesis": thesis,
            "_priority": priority,
            "_fingerprint": fp,
        })

    summary["steps"]["gates"] = {
        "input": len(discovered),
        "survivors": len(survivors),
        "rejected": rejected,
        "memory_skipped": memory_skipped,
    }
    emit(
        f"Gates — {len(survivors)} survivors ({rejected} rejected, "
        f"{memory_skipped} memory-skipped)"
    )

    # Persist survivors then fill owner emails for free (site + patterns) before critic
    cheap_names = []
    for row in survivors:
        rec = {k: v for k, v in row.items() if not str(k).startswith("_")}
        upsert_lead(rec, agent=agent)
        if rec.get("company") and rec.get("contact_name"):
            cheap_names.append(rec["company"])
    cheap_email: dict = {"skipped": True}
    if cheap_names:
        emit("Step 2b/7 — Free owner email fill (site + patterns)…")
        from .contact_enrich import enrich_imported_contacts

        cheap_email = enrich_imported_contacts(
            agent, list(dict.fromkeys(cheap_names)), on_progress=emit,
        )
        cheap_email["skipped"] = False
    summary["steps"]["cheap_email"] = cheap_email

    # ---- 3. Budget allocate ----
    emit("Step 3/7 — Allocating Claude / Seamless research budget…")
    budget = allocate_budget(
        survivors, claude_cap=claude_cap, seamless_cap=seamless_cap,
    )
    summary["steps"]["budget"] = {
        "claude_queue": len(budget["claude_queue"]),
        "seamless_planned": len(budget["seamless_queue"]),
        "deferred": len(budget["deferred"]),
        "claude_cap": claude_cap,
        "seamless_cap": seamless_cap,
    }
    emit(
        f"Budget — Claude deep-research {len(budget['claude_queue'])}/{claude_cap}, "
        f"Seamless plan {seamless_cap}"
    )

    # Persist deferred as imported/skipped watchlist (no credits)
    for row in budget["deferred"]:
        rec = {k: v for k, v in row.items() if not str(k).startswith("_")}
        rec["status"] = "imported"
        upsert_lead(rec, agent=agent)

    # ---- 4. Claude analyst on top EV ----
    emit("Step 4/7 — Claude succession analyst…")
    analyzed: list[dict] = []
    analyst_errors = 0
    for row in budget["claude_queue"]:
        intel = row["_intel"]
        lead = {k: v for k, v in row.items() if not str(k).startswith("_")}
        analyst = run_succession_analyst(intel, lead=lead, use_llm=use_llm)
        if analyst.get("error"):
            analyst_errors += 1
        intel2 = merge_analyst_into_intel(intel, analyst)
        q2 = qualify_keira_intelligence(intel2, config=config)
        # Prefer analyst why_now when stronger
        if analyst.get("why_now") and not q2.get("why_now"):
            q2["why_now"] = analyst["why_now"]
        if analyst.get("mna_thesis"):
            thesis = build_mna_thesis(intel2, q2)
            thesis["thesis"] = analyst["mna_thesis"]
        else:
            thesis = build_mna_thesis(intel2, q2)
        if analyst.get("recommendation") == "reject":
            q2["rejected"] = True
            q2["outreach_ok"] = False
            q2["status"] = "reject"
            q2.setdefault("reject_reasons", []).append("Claude analyst: reject")
        priority = research_priority(q2, intel2)
        q2.update(priority)
        analyzed.append({
            **_lead_record(lead, intel2, q2, thesis, status="imported"),
            "_intel": intel2,
            "_qual": q2,
            "_thesis": thesis,
            "_priority": priority,
            "_analyst": analyst,
        })

    summary["steps"]["analyst"] = {
        "reviewed": len(analyzed),
        "errors": analyst_errors,
    }

    # ---- 5. Critic gate (drafts only; Seamless is not gated here) ----
    emit("Step 5/7 — Critic gate (drafts); Seamless contacts all non-rejected…")
    enrich_min_tier = int(
        os.getenv("KEIRA_ENRICH_MIN_TIER")
        or research_cfg.get("enrich_min_succession_tier")
        or 1
    )
    draft_min_tier = int(
        os.getenv("KEIRA_DRAFT_MIN_TIER")
        or research_cfg.get("draft_min_succession_tier")
        or 2
    )
    approved: list[dict] = []  # critic enrich-ok (stats / memory only)
    draft_approved_companies: set[str] = set()
    critic_reject = 0
    research_hold = 0
    soft_enrich_only = 0
    for row in analyzed:
        if row["_qual"].get("rejected"):
            rec = {k: v for k, v in row.items() if not str(k).startswith("_")}
            rec["status"] = "skipped"
            upsert_lead(rec, agent=agent)
            critic_reject += 1
            continue
        critic = run_lead_critic(
            intel=row["_intel"],
            qualification=row["_qual"],
            thesis=row.get("_thesis"),
            use_llm=use_llm,
            enrich_min_tier=enrich_min_tier,
            draft_min_tier=draft_min_tier,
        )
        row["_critic"] = critic
        # Attach critic to qualification_json
        payload = json.loads(row.get("qualification_json") or "{}")
        payload["critic"] = critic
        payload["analyst"] = {
            k: row["_analyst"].get(k)
            for k in ("mode", "recommendation", "why_now", "key_uncertainty", "mna_thesis")
            if row.get("_analyst")
        }
        row["qualification_json"] = json.dumps(payload)

        if critic.get("approved_for_enrich") or critic.get("approved"):
            approved.append(row)
            if critic.get("approved_for_draft"):
                draft_approved_companies.add((row.get("company") or "").lower())
            elif critic.get("recommendation") == "enrich":
                soft_enrich_only += 1
            st, reason = status_from_qualification(row["_qual"], critic=critic)
            remember_research(
                row["company"],
                status=st,
                reason=reason,
                fingerprint=row.get("_fingerprint") or fingerprint_lead(row),
                research_priority=int((row.get("_priority") or {}).get("research_priority") or 0),
                meta={
                    "critic": critic.get("recommendation"),
                    "approved_for_draft": bool(critic.get("approved_for_draft")),
                },
            )
        elif critic.get("recommendation") == "reject" or critic.get("hard_reject"):
            rec = {k: v for k, v in row.items() if not str(k).startswith("_")}
            rec["status"] = "skipped"
            upsert_lead(rec, agent=agent)
            critic_reject += 1
            remember_research(
                row["company"],
                status="rejected",
                reason="; ".join((critic.get("issues") or [])[:2]) or "critic reject",
                fingerprint=row.get("_fingerprint"),
                research_priority=0,
            )
        else:
            # Soft hold: still eligible for Seamless contacts; drafts stay blocked
            rec = {k: v for k, v in row.items() if not str(k).startswith("_")}
            rec["status"] = "imported"
            upsert_lead(rec, agent=agent)
            research_hold += 1
            remember_research(
                row["company"],
                status="research_later",
                reason="; ".join((critic.get("issues") or [])[:2]) or "research hold",
                fingerprint=row.get("_fingerprint"),
                research_priority=int((row.get("_priority") or {}).get("research_priority") or 0),
            )

    # Seamless for every non-rejected gate survivor (analyzed + Claude-deferred).
    # Critic still controls drafts only.
    seamless_pool: list[dict] = []
    seen_seamless: set[str] = set()

    def _eligible_for_seamless(row: dict) -> bool:
        co = (row.get("company") or "").strip()
        key = co.lower()
        if not co or key in seen_seamless:
            return False
        if (row.get("_qual") or {}).get("rejected"):
            return False
        critic = row.get("_critic") or {}
        if critic.get("hard_reject") or critic.get("recommendation") == "reject":
            return False
        return True

    for row in analyzed:
        if _eligible_for_seamless(row):
            seen_seamless.add((row.get("company") or "").strip().lower())
            seamless_pool.append(row)
    for row in survivors:
        if _eligible_for_seamless(row):
            seen_seamless.add((row.get("company") or "").strip().lower())
            seamless_pool.append(row)

    seamless_pool.sort(
        key=lambda r: (r.get("_priority") or {}).get("research_priority") or r.get("score") or 0,
        reverse=True,
    )
    if seamless_cap > 0:
        to_enrich = seamless_pool[:seamless_cap]
        contact_force_web = False
    elif seamless_pool and not _research_budget_dead(budget_note):
        # Per-run clamp hit — keep Seamless; allocate_research_slots may grant run overage
        to_enrich = seamless_pool[: min(len(seamless_pool), 20)]
        contact_force_web = False
        emit(
            f"Per-run Seamless cap empty — still trying Seamless for {len(to_enrich)} "
            "leads (run overage / remaining daily)"
        )
    else:
        # Credits truly exhausted (disabled / monthly / daily hard cap) — free web fallback
        to_enrich = seamless_pool[: min(3, len(seamless_pool))]
        contact_force_web = bool(to_enrich)
        if seamless_pool:
            from .seamless_budget import format_budget_alert_message, notify_seamless_budget_exhausted

            alert = notify_seamless_budget_exhausted(
                budget_note or "Seamless research budget exhausted mid-run",
                agent=agent,
                context=f"{len(seamless_pool)} leads waiting for contacts",
            )
            summary.setdefault("alerts", []).append(alert)
            summary["budget_alert"] = alert
            emit(format_budget_alert_message(alert["reason"], agent=agent))
        if contact_force_web:
            emit(
                "Seamless research budget empty — web contact fallback for "
                f"{len(to_enrich)} of {len(seamless_pool)} leads"
            )

    summary["steps"]["critic"] = {
        "approved": len(approved),
        "approved_for_enrich": len(approved),
        "approved_for_draft": len(draft_approved_companies),
        "soft_enrich_only": soft_enrich_only,
        "enrich_queue": len(to_enrich),
        "seamless_pool": len(seamless_pool),
        "enrich_min_tier": enrich_min_tier,
        "draft_min_tier": draft_min_tier,
        "rejected": critic_reject,
        "research_hold": research_hold,
        "web_contact_fallback": contact_force_web,
    }
    emit(
        f"Critic — {len(approved)} enrich-ok ({soft_enrich_only} soft), "
        f"{len(draft_approved_companies)} draft-ok; Seamless pool {len(seamless_pool)} "
        f"(capped {len(to_enrich)}), {critic_reject} rejected, {research_hold} hold"
    )

    for row in to_enrich:
        upsert_lead({k: v for k, v in row.items() if not str(k).startswith("_")}, agent=agent)

    # ---- 6. Contact discovery (Seamless for all non-rejected leads) ----
    from .keira_contacts import (
        discover_keira_contacts,
        keira_contact_discovery_mode,
    )

    contact_mode = "web" if contact_force_web else keira_contact_discovery_mode()
    emit(f"Step 6/7 — Finding contacts via {contact_mode} for {len(to_enrich)} leads…")

    company_names = list(dict.fromkeys(
        [r["company"] for r in to_enrich if r.get("company")]
    ))

    enrich_step: dict = {"enriched": 0, "skipped": True}
    if company_names:
        try:
            enrich_step = discover_keira_contacts(
                agent,
                company_names,
                limit=max(seamless_cap, len(company_names)),
                config=config,
                skip_existing=False,
                on_progress=emit,
                force_mode="web" if contact_force_web else None,
            )
            # Bubble Seamless budget alerts from contact step into pipeline result
            for alert in (enrich_step.get("alerts") or []):
                summary.setdefault("alerts", []).append(alert)
            nested = (enrich_step.get("people_search") or {})
            if nested.get("budget_alert"):
                summary.setdefault("alerts", []).append(nested["budget_alert"])
                summary["budget_alert"] = nested["budget_alert"]
            elif enrich_step.get("budget_alert"):
                summary["budget_alert"] = enrich_step["budget_alert"]
        except Exception as e:
            logger.warning("Keira contact discovery failed: %s", e)
            enrich_step = {"error": str(e), "imported": 0, "updated": 0}
    else:
        enrich_step = {"imported": 0, "note": "no companies needing contacts", "skipped": True}
    summary["steps"]["enrich"] = enrich_step
    summary["steps"]["contacts"] = enrich_step

    # ---- 7. Process + drafts ----
    emit("Step 7/7 — Qualifying + confidential outreach drafts…")
    from .agent import run_process_imported

    processed = run_process_imported(
        agent,
        limit=max(limit, len(company_names) or limit),
        use_llm=use_llm,
        require_identity=True,
        on_progress=emit,
    )
    summary["steps"]["process"] = {"count": processed.get("count") or 0}

    drafts = {"created": 0}
    if create_drafts:
        from .woodway_pipeline import create_mailbox_drafts

        # Hard draft gate: mailbox drafts only for critic approved_for_draft companies
        drafts = create_mailbox_drafts(
            agent,
            prefer_microsoft=False,
            companies=draft_approved_companies or None,
            require_company_allowlist=True,
        )
        drafts["draft_gate"] = {
            "approved_for_draft": len(draft_approved_companies),
            "companies": sorted(c for c in draft_approved_companies if c),
        }
    summary["steps"]["drafts"] = drafts

    emit(
        f"Keira pipeline done — contacts +{enrich_step.get('imported', 0)}/"
        f"~{enrich_step.get('updated', 0)}, "
        f"processed {summary['steps']['process']['count']}, "
        f"drafts {drafts.get('created', 0)}"
    )

    try:
        from .funnel_metrics import compute_funnel_from_summary, persist_funnel

        funnel = compute_funnel_from_summary(summary)
        persist_funnel(agent, summary["run_id"], funnel)
        summary["funnel"] = funnel
        emit(
            f"Funnel — keep={funnel.get('kept')} email={funnel.get('with_email')} "
            f"drafted={funnel.get('drafted')}"
        )
    except Exception as e:
        logger.warning("Keira funnel metrics failed: %s", e)

    return summary


def _lead_record(raw: dict, intel: dict, q: dict, thesis: dict, *, status: str) -> dict:
    record = {
        **{k: v for k, v in raw.items() if not str(k).startswith("_")},
        "company": intel["company"]["name"],
        "company_domain": intel["company"].get("domain") or raw.get("company_domain"),
        "contact_name": intel["ownership"].get("owner_name") or raw.get("contact_name"),
        "contact_title": intel["ownership"].get("title") or raw.get("contact_title"),
        "employee_count": intel["company"].get("employees") or raw.get("employee_count"),
        "industry": intel["company"].get("industry") or raw.get("industry"),
        "signal": raw.get("signal") or (q.get("why_now") or "")[:240],
        "source": raw.get("source") or "keira_pipeline",
        "score": q["score"],
        "tier": q["tier"],
        "status": status,
        "qualification_json": json.dumps({
            "keira": q,
            "intelligence": {
                "geo": intel.get("geo"),
                "ownership": intel.get("ownership"),
                "succession": intel.get("succession"),
                "size": intel.get("size"),
                "analyst": intel.get("analyst"),
            },
            "mna_thesis": thesis,
            "priority": {
                "lead_priority": q.get("lead_priority"),
                "research_priority": q.get("research_priority"),
                "icp_fit": q.get("icp_fit"),
                "timing": q.get("timing"),
                "evidence_confidence": q.get("evidence_confidence"),
                "outreach_priority": q.get("outreach_priority"),
            },
        }),
    }
    record["prospect"] = build_prospect_string(record)
    return record
