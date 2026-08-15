"""LLM call tuning — token budgets, context truncation, Anthropic prompt caching."""

from __future__ import annotations

import os

# Per-task defaults (Anthropic-optimized: tight output, low temp for JSON)
TASK_DEFAULTS = {
    "qualify": {"max_tokens": 768, "temperature": 0.15, "context_chars": 2400},
    "qualify_pass1": {"max_tokens": 512, "temperature": 0.1, "context_chars": 3000},
    "digest": {"max_tokens": 512, "temperature": 0.1, "context_chars": 4000},
    "outreach": {"max_tokens": 512, "temperature": 0.45, "context_chars": 1400},
    "extract": {"max_tokens": 2048, "temperature": 0.15, "context_chars": 9000},
    "critique": {"max_tokens": 256, "temperature": 0.1, "context_chars": 1200},
    "reply_classify": {"max_tokens": 256, "temperature": 0.1, "context_chars": 2000},
    "keira_analyst": {"max_tokens": 1600, "temperature": 0.15, "context_chars": 5500},
    "keira_critic": {"max_tokens": 700, "temperature": 0.1, "context_chars": 5000},
    "default": {"max_tokens": 1024, "temperature": 0.35, "context_chars": 4000},
}


def model_for_task(task: str) -> str | None:
    """Task-specific model override from env (Haiku for bulk, Sonnet for judgment)."""
    env_map = {
        "extract": "MODEL_EXTRACTION",
        "qualify_pass1": "MODEL_EXTRACTION",
        "critique": "MODEL_CRITIQUE",
        "reply_classify": "MODEL_CRITIQUE",
        "keira_critic": "MODEL_CRITIQUE",
        "qualify": "MODEL_QUALIFY",
        "outreach": "MODEL_OUTREACH",
        "digest": "MODEL_QUALIFY",
        "keira_analyst": "MODEL_QUALIFY",
    }
    key = env_map.get(task)
    if key:
        val = os.getenv(key, "").strip()
        if val:
            return val
    if task in ("extract", "qualify_pass1", "critique", "reply_classify", "keira_critic"):
        return os.getenv("MODEL_EXTRACTION", "claude-haiku-4-5")
    if task in ("qualify", "outreach", "digest", "keira_analyst"):
        return os.getenv("MODEL_QUALIFY", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"))
    return None


def task_defaults(task: str) -> dict:
    return dict(TASK_DEFAULTS.get(task, TASK_DEFAULTS["default"]))


def truncate_context(text: str | None, max_chars: int) -> str | None:
    """Trim long research/context blocks — keeps head + tail for signal."""
    if not text:
        return text
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head - 20
    return f"{text[:head].rstrip()}…\n…{text[-tail:].lstrip()}"


def anthropic_prompt_cache_enabled() -> bool:
    raw = (os.getenv("ANTHROPIC_PROMPT_CACHE") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def research_max_results() -> int:
    return int(os.getenv("RESEARCH_MAX_RESULTS", "12"))


def process_delay_seconds() -> float:
    """Delay between batch lead processing — shorter for paid Claude, longer for Groq free tier."""
    explicit = os.getenv("PROCESS_DELAY")
    if explicit is not None and str(explicit).strip() != "":
        return float(explicit)
    try:
        from .llm import resolve_provider

        provider = resolve_provider()["provider"]
        if provider == "anthropic":
            return float(os.getenv("LLM_CALL_DELAY", "0.3"))
        if provider == "groq":
            return float(os.getenv("LLM_CALL_DELAY", "2.5"))
    except Exception:
        pass
    return float(os.getenv("LLM_CALL_DELAY", "0.5"))
