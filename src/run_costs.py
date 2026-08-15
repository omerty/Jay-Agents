"""Per-run LLM cost tracking and budget guardrails."""

from __future__ import annotations

import logging
import os
import threading

from .db import record_run_cost, run_cost_summary

logger = logging.getLogger(__name__)

_ctx = threading.local()

# Approximate $/1M tokens (input, output) — Anthropic Sonnet/Haiku ballpark
MODEL_RATES = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (0.8, 4.0),
    "claude-3-5-haiku-latest": (0.8, 4.0),
    "llama-3.3-70b-versatile": (0.0, 0.0),
    "gpt-4o-mini": (0.15, 0.6),
}


def set_run_context(run_id: str | None, step: str | None = None):
    _ctx.run_id = run_id
    _ctx.step = step


def get_run_context() -> tuple[str | None, str | None]:
    return getattr(_ctx, "run_id", None), getattr(_ctx, "step", None)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = MODEL_RATES.get(model, (3.0, 15.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def record_llm_usage(model: str, input_tokens: int, output_tokens: int, *, step: str | None = None):
    run_id, ctx_step = get_run_context()
    if not run_id:
        return
    step = step or ctx_step or "llm"
    cost = estimate_cost(model, input_tokens, output_tokens)
    record_run_cost(run_id, step, model, input_tokens, output_tokens, cost)
    check_budget(run_id)


def max_run_cost_usd() -> float:
    return float(os.getenv("MAX_RUN_COST_USD", "5.0"))


def check_budget(run_id: str) -> None:
    summary = run_cost_summary(run_id)
    total = summary["cost_usd"]
    cap = max_run_cost_usd()
    if total >= cap:
        raise BudgetExceededError(f"Run cost ${total:.4f} exceeded MAX_RUN_COST_USD=${cap:.2f}")


class BudgetExceededError(Exception):
    pass


def cost_dashboard(agent: str = "woodway") -> dict:
    from .db import get_connection, init_db

    init_db()
    summary = run_cost_summary()
    with get_connection() as conn:
        total_leads = conn.execute("SELECT COUNT(*) FROM leads WHERE agent = ?", (agent,)).fetchone()[0]
        qualified = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND score >= 50", (agent,)
        ).fetchone()[0]
        drafted = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND status = 'drafted'", (agent,)
        ).fetchone()[0]
        replied = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE agent = ? AND status = 'replied'", (agent,)
        ).fetchone()[0]
    cost = summary["cost_usd"] or 0.001
    return {
        "total_cost_usd": round(summary["cost_usd"], 4),
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "cost_per_lead": round(cost / max(total_leads, 1), 4),
        "cost_per_qualified": round(cost / max(qualified, 1), 4),
        "cost_per_draft": round(cost / max(drafted, 1), 4),
        "cost_per_reply": round(cost / max(replied, 1), 4),
        "total_leads": total_leads,
        "qualified": qualified,
        "drafted": drafted,
        "replied": replied,
    }
