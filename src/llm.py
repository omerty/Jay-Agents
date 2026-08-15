"""LLM layer — pluggable providers.

Providers:
  - anthropic  Claude via Anthropic Messages API (recommended primary).
  - groq       Groq cloud, OpenAI-compatible. Free tier (good fallback).
  - openai     OpenAI API (or any OpenAI-compatible via OPENAI_BASE_URL).
  - actava     Actava Cura (OpenAI-compatible).
  - ollama     Local, free, no key.

Selection: LLM_PROVIDER env var, or auto-detect:
ANTHROPIC_API_KEY → anthropic, else GROQ_API_KEY → groq, else ACTAVA → actava,
else OPENAI → openai, else ollama.

Backup: LLM_FALLBACK (e.g. groq). When the primary call fails after retries,
the backup is tried once. If unset and a secondary key exists, a sensible
default fallback is chosen (groq↔anthropic).
"""

import json
import logging
import os
import re
import time

import httpx

from . import env  # noqa: F401 — load .env before reading os.environ

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-6",
    "ollama": "llama3.2",
    "actava": "actava/cura-soar",
}

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "actava": "https://inference.actava.ai/v1",
}

KNOWN_PROVIDERS = ("groq", "openai", "anthropic", "ollama", "actava")


def _valid_actava_key() -> bool:
    key = os.getenv("ACTAVA_API_KEY", "").strip().lower()
    return bool(key) and key not in ("your_key_here", "changeme", "xxx", "test")


def _valid_key(value: str | None) -> bool:
    key = (value or "").strip().lower()
    return bool(key) and key not in ("your_key_here", "changeme", "xxx", "test", "")


MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
RETRY_BASE_DELAY = 1.5  # seconds; doubles per attempt


class LLMError(Exception):
    pass


# Backwards-compatible alias (pre-provider code raised OllamaError)
OllamaError = LLMError


def _timeout() -> float:
    return float(os.getenv("LLM_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "120")))


def _provider_config(provider: str) -> dict:
    """Build config dict for a known provider name."""
    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY", "")
        if not _valid_key(api_key):
            raise LLMError(
                "GROQ_API_KEY not set. Get a free key (no credit card) at "
                "https://console.groq.com/keys and add it to .env"
            )
        model = os.getenv("GROQ_MODEL", DEFAULT_MODELS["groq"])
        return {"provider": "groq", "model": model, "base_url": BASE_URLS["groq"], "api_key": api_key}

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not _valid_key(api_key):
            raise LLMError("OPENAI_API_KEY not set. Add it to .env or use LLM_PROVIDER=groq/ollama.")
        model = os.getenv("OPENAI_MODEL", DEFAULT_MODELS["openai"])
        base_url = os.getenv("OPENAI_BASE_URL", BASE_URLS["openai"]).rstrip("/")
        return {"provider": "openai", "model": model, "base_url": base_url, "api_key": api_key}

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not _valid_key(api_key):
            raise LLMError(
                "ANTHROPIC_API_KEY not set. Get a key at https://console.anthropic.com/ "
                "and add it to .env (Claude backup / primary)."
            )
        model = os.getenv("ANTHROPIC_MODEL", DEFAULT_MODELS["anthropic"])
        base_url = os.getenv("ANTHROPIC_BASE_URL", BASE_URLS["anthropic"]).rstrip("/")
        return {
            "provider": "anthropic",
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
        }

    if provider == "actava":
        api_key = os.getenv("ACTAVA_API_KEY", "")
        if not _valid_actava_key():
            raise LLMError(
                "ACTAVA_API_KEY not set. Get a key from app.actava.ai and add it to .env"
            )
        model = os.getenv("ACTAVA_CURA_MODEL", DEFAULT_MODELS["actava"])
        base_url = os.getenv("ACTAVA_CURA_URL", BASE_URLS["actava"]).rstrip("/")
        return {"provider": "actava", "model": model, "base_url": base_url, "api_key": api_key}

    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", DEFAULT_MODELS["ollama"])
    return {"provider": "ollama", "model": model, "base_url": host, "api_key": None}


def resolve_provider() -> dict:
    """Return {provider, model, base_url, api_key} from env."""
    provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if provider and provider not in KNOWN_PROVIDERS:
        raise LLMError(
            f"Unknown LLM_PROVIDER '{provider}'. Use {', '.join(KNOWN_PROVIDERS)}."
        )

    if not provider:
        if _valid_key(os.getenv("ANTHROPIC_API_KEY")):
            provider = "anthropic"
        elif _valid_key(os.getenv("GROQ_API_KEY")):
            provider = "groq"
        elif os.getenv("ACTAVA_API_KEY") and _valid_actava_key():
            provider = "actava"
        elif _valid_key(os.getenv("OPENAI_API_KEY")):
            provider = "openai"
        else:
            provider = "ollama"

    return _provider_config(provider)


def resolve_fallback_provider(primary: str | None = None) -> dict | None:
    """Optional backup provider (e.g. groq when Claude is primary)."""
    raw = (os.getenv("LLM_FALLBACK") or "").strip().lower()
    if raw in ("0", "false", "none", "off"):
        return None
    if not raw:
        # Prefer the other strong cloud provider as automatic backup
        if primary == "anthropic" and _valid_key(os.getenv("GROQ_API_KEY")):
            raw = "groq"
        elif primary != "anthropic" and _valid_key(os.getenv("ANTHROPIC_API_KEY")):
            raw = "anthropic"
        else:
            return None
    if raw not in KNOWN_PROVIDERS:
        logger.warning("Ignoring unknown LLM_FALLBACK=%s", raw)
        return None
    if primary and raw == primary:
        return None
    try:
        return _provider_config(raw)
    except LLMError as e:
        logger.warning("LLM fallback unavailable: %s", e)
        return None


def check_llm() -> dict:
    """Health probe. Returns {ok, provider, model, detail, fallback}."""
    try:
        cfg = resolve_provider()
    except LLMError as e:
        return {"ok": False, "provider": None, "model": None, "detail": str(e), "fallback": None}

    fallback_cfg = resolve_fallback_provider(cfg["provider"])
    fallback_name = fallback_cfg["provider"] if fallback_cfg else None

    try:
        if cfg["provider"] == "ollama":
            r = httpx.get(f"{cfg['base_url']}/api/tags", timeout=5.0)
            r.raise_for_status()
            names = {m["name"] for m in r.json().get("models", [])}
            model = cfg["model"]
            if not any(n == model or n.startswith(f"{model}:") for n in names):
                return {
                    "ok": False, "provider": "ollama", "model": model,
                    "detail": f"Model '{model}' not pulled. Run: ollama pull {model}. "
                              f"Available: {', '.join(sorted(names)) or '(none)'}",
                    "fallback": fallback_name,
                }
        elif cfg["provider"] == "anthropic":
            if not cfg["api_key"].startswith("sk-ant-"):
                return {
                    "ok": False, "provider": "anthropic", "model": cfg["model"],
                    "detail": "ANTHROPIC_API_KEY does not look valid (expected sk-ant-…)",
                    "fallback": fallback_name,
                }
        else:
            r = httpx.get(
                f"{cfg['base_url']}/models",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=8.0,
            )
            if r.status_code == 401:
                return {
                    "ok": False, "provider": cfg["provider"], "model": cfg["model"],
                    "detail": f"Invalid {cfg['provider'].upper()} API key",
                    "fallback": fallback_name,
                }
            r.raise_for_status()
        return {
            "ok": True,
            "provider": cfg["provider"],
            "model": cfg["model"],
            "detail": None,
            "fallback": fallback_name,
        }
    except httpx.HTTPError as e:
        return {
            "ok": False, "provider": cfg["provider"], "model": cfg["model"],
            "detail": f"Cannot reach {cfg['provider']}: {e}",
            "fallback": fallback_name,
        }


def ensure_llm() -> dict:
    """Raise LLMError if the configured provider is unusable; return status."""
    status = check_llm()
    if not status["ok"]:
        if status.get("fallback"):
            return status
        raise LLMError(status["detail"] or "LLM provider unavailable")
    return status


def chat(
    prompt: str,
    *,
    system: str | None = None,
    json_mode: bool = False,
    max_tokens: int = 1024,
    temperature: float | None = None,
    task: str | None = None,
) -> str:
    """Single chat completion. Retries transient failures; then tries LLM_FALLBACK."""
    cfg = resolve_provider()
    if task:
        override = None
        try:
            from .llm_optimize import model_for_task
            override = model_for_task(task)
        except Exception:
            pass
        if override:
            cfg = dict(cfg)
            cfg["model"] = override
    if temperature is None:
        temperature = 0.2 if json_mode else 0.4

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        text, usage = _chat_with_retries(cfg, messages, json_mode, max_tokens, temperature, return_usage=True)
    except LLMError as primary_err:
        fallback = resolve_fallback_provider(cfg["provider"])
        if not fallback:
            raise
        logger.warning(
            "Primary LLM (%s) failed (%s) — trying fallback %s",
            cfg["provider"], primary_err, fallback["provider"],
        )
        text, usage = _chat_with_retries(fallback, messages, json_mode, max_tokens, temperature, return_usage=True)

    if usage:
        try:
            from .run_costs import record_llm_usage
            record_llm_usage(cfg["model"], usage.get("input", 0), usage.get("output", 0), step=task or "chat")
        except Exception:
            pass
    return text


def _chat_with_retries(cfg, messages, json_mode, max_tokens, temperature, *, return_usage: bool = False):
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            if cfg["provider"] == "ollama":
                text = _chat_ollama(cfg, messages, json_mode, max_tokens, temperature)
                return (text, {}) if return_usage else text
            if cfg["provider"] == "anthropic":
                text, usage = _chat_anthropic(cfg, messages, json_mode, max_tokens, temperature, return_usage=True)
                return (text, usage) if return_usage else text
            text = _chat_openai_compatible(cfg, messages, json_mode, max_tokens, temperature)
            return (text, {}) if return_usage else text
        except _RetryableError as e:
            last_error = e
            delay = e.retry_after or RETRY_BASE_DELAY * (2 ** attempt)
            if attempt + 1 < MAX_RETRIES:
                logger.warning(
                    "LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, MAX_RETRIES, e, delay,
                )
                time.sleep(delay)

    raise LLMError(f"LLM request failed after {MAX_RETRIES} attempts: {last_error}")


class _RetryableError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def _raise_for_response(r: httpx.Response, provider: str):
    if r.status_code == 429:
        retry_after = None
        header = r.headers.get("retry-after")
        if header:
            try:
                retry_after = min(float(header), 30.0)
            except ValueError:
                pass
        raise _RetryableError(f"{provider} rate limit (429)", retry_after)
    if r.status_code >= 500:
        raise _RetryableError(f"{provider} server error ({r.status_code})")
    if r.status_code == 401:
        raise LLMError(f"Invalid {provider} API key (401)")
    if r.status_code != 200:
        raise LLMError(f"{provider} request failed ({r.status_code}): {r.text[:400]}")


def _chat_openai_compatible(cfg, messages, json_mode, max_tokens, temperature) -> str:
    body: dict = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    try:
        r = httpx.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json=body,
            timeout=_timeout(),
        )
    except httpx.HTTPError as e:
        raise _RetryableError(f"{cfg['provider']} network error: {e}") from e

    _raise_for_response(r, cfg["provider"])

    choices = r.json().get("choices") or []
    content = (choices[0].get("message", {}).get("content") or "") if choices else ""
    if not content.strip():
        raise LLMError(f"{cfg['provider']} returned an empty response")
    delay = float(os.getenv("LLM_CALL_DELAY", "0"))
    if delay > 0:
        time.sleep(delay)
    return content.strip()


def _chat_anthropic(cfg, messages, json_mode, max_tokens, temperature, *, return_usage: bool = False):
    """Claude Messages API — cached system blocks, tight max_tokens, JSON via instruction."""
    from .llm_optimize import anthropic_prompt_cache_enabled

    system = None
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system = (system + "\n\n" if system else "") + m["content"]
        else:
            role = m["role"] if m["role"] in ("user", "assistant") else "user"
            api_messages.append({"role": role, "content": m["content"]})

    if not api_messages:
        raise LLMError("anthropic requires at least one user message")

    if json_mode:
        hint = (
            "Respond with a single valid JSON object only. "
            "No markdown fences, no commentary before or after the JSON."
        )
        system = f"{system}\n\n{hint}" if system else hint

    effective_max = max(max_tokens, 896) if json_mode else max_tokens

    body: dict = {
        "model": cfg["model"],
        "messages": api_messages,
        "max_tokens": effective_max,
        "temperature": temperature,
    }
    if system:
        if anthropic_prompt_cache_enabled() and len(system) >= 256:
            body["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            body["system"] = system

    try:
        r = httpx.post(
            f"{cfg['base_url']}/messages",
            headers={
                "x-api-key": cfg["api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=_timeout(),
        )
    except httpx.HTTPError as e:
        raise _RetryableError(f"anthropic network error: {e}") from e

    _raise_for_response(r, "anthropic")

    data = r.json()
    usage = data.get("usage") or {}
    cache_read = usage.get("cache_read_input_tokens") or 0
    if cache_read:
        logger.debug("anthropic cache hit: %d tokens read from cache", cache_read)
    if data.get("stop_reason") == "max_tokens":
        logger.warning("anthropic response truncated (max_tokens=%s)", effective_max)

    parts = data.get("content") or []
    text_bits = [p.get("text", "") for p in parts if p.get("type") == "text"]
    content = "".join(text_bits).strip()
    if not content:
        raise LLMError("anthropic returned an empty response")
    delay = float(os.getenv("LLM_CALL_DELAY", "0"))
    if delay > 0:
        time.sleep(delay)
    usage_out = {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }
    if return_usage:
        return content, usage_out
    return content


def _chat_ollama(cfg, messages, json_mode, max_tokens, temperature) -> str:
    body: dict = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    if json_mode:
        body["format"] = "json"

    try:
        r = httpx.post(f"{cfg['base_url']}/api/chat", json=body, timeout=_timeout())
    except httpx.HTTPError as e:
        raise _RetryableError(
            f"Cannot reach Ollama at {cfg['base_url']}: {e}. "
            "Install from https://ollama.com and run: ollama pull llama3.2"
        ) from e

    _raise_for_response(r, "ollama")

    content = r.json().get("message", {}).get("content", "")
    if not content.strip():
        raise LLMError("Ollama returned an empty response")
    delay = float(os.getenv("LLM_CALL_DELAY", "0"))
    if delay > 0:
        time.sleep(delay)
    return content.strip()


def chat_json(
    prompt: str,
    *,
    system: str | None = None,
    retries: int = 2,
    max_tokens: int | None = None,
    temperature: float | None = None,
    task: str | None = None,
) -> dict:
    """Chat in JSON mode with parse retries."""
    from .llm_optimize import task_defaults

    defaults = task_defaults("qualify")
    effective_max = max_tokens if max_tokens is not None else defaults["max_tokens"]
    effective_temp = temperature if temperature is not None else defaults["temperature"]

    last_error: Exception | None = None
    attempt_prompt = prompt

    for attempt in range(retries):
        try:
            raw = chat(
                attempt_prompt,
                system=system,
                json_mode=True,
                max_tokens=effective_max,
                temperature=effective_temp,
                task=task,
            )
            return _parse_json(raw)
        except (LLMError, json.JSONDecodeError) as e:
            last_error = e
            if attempt + 1 >= retries:
                break
            attempt_prompt = (
                f"{prompt}\n\n"
                "IMPORTANT: Your last response was invalid JSON. "
                "Reply with a single valid JSON object only. No markdown, no comments."
            )

    raise LLMError(f"Could not parse JSON from model after {retries} tries: {last_error}")


def _parse_json(text: str) -> dict:
    candidates = _json_candidates(text)
    errors: list[str] = []

    for candidate in candidates:
        for variant in (candidate, _repair_json(candidate)):
            try:
                parsed = json.loads(variant)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError as e:
                errors.append(str(e))

    start = text.find("{")
    if start >= 0:
        decoder = json.JSONDecoder()
        try:
            parsed, _ = decoder.raw_decode(_repair_json(text[start:]))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as e:
            errors.append(str(e))

    preview = text[:500].replace("\n", " ")
    raise LLMError(
        f"No valid JSON object found. Errors: {'; '.join(errors[:2])}. Preview: {preview}"
    )


def _json_candidates(text: str) -> list[str]:
    text = text.strip()
    out = [text]

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        out.insert(0, fenced.group(1).strip())

    bare = re.search(r"\{.*\}", text, re.DOTALL)
    if bare:
        out.append(bare.group(0).strip())

    return out


def _repair_json(text: str) -> str:
    """Fix common LLM JSON mistakes."""
    text = text.strip()
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r",\s*]", "]", text)
    text = re.sub(r"\bNone\b", "null", text)
    text = re.sub(r"\bTrue\b", "true", text)
    text = re.sub(r"\bFalse\b", "false", text)
    return text
