"""LLM layer — pluggable providers.

Providers (cheapest first):
  - groq    Groq cloud, OpenAI-compatible. Free tier (no card): llama-3.3-70b-versatile
            at 30 req/min / 1k req/day. Paid: ~$0.59/M input tokens.
  - openai  OpenAI API. Default gpt-4o-mini (~$0.15/M input, $0.60/M output).
            Also accepts any OpenAI-compatible endpoint via OPENAI_BASE_URL
            (OpenRouter, Together, etc.).
  - ollama  Local, free, no key. Needs `ollama serve` + a pulled model.

Selection: LLM_PROVIDER env var (groq|openai|ollama), or auto-detect:
GROQ_API_KEY set → groq, else OPENAI_API_KEY set → openai, else ollama.
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
    "ollama": "llama3.2",
    "actava": "actava/cura-soar",
}

BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "openai": "https://api.openai.com/v1",
    "actava": "https://inference.actava.ai/v1",
}


def _valid_actava_key() -> bool:
    key = os.getenv("ACTAVA_API_KEY", "").strip().lower()
    return bool(key) and key not in ("your_key_here", "changeme", "xxx", "test")

MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))
RETRY_BASE_DELAY = 1.5  # seconds; doubles per attempt


class LLMError(Exception):
    pass


# Backwards-compatible alias (pre-provider code raised OllamaError)
OllamaError = LLMError


def _timeout() -> float:
    return float(os.getenv("LLM_TIMEOUT", os.getenv("OLLAMA_TIMEOUT", "120")))


def resolve_provider() -> dict:
    """Return {provider, model, base_url, api_key} from env."""
    provider = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if provider not in ("groq", "openai", "ollama", "actava", ""):
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'. Use groq, openai, ollama, or actava.")

    if not provider:
        if os.getenv("GROQ_API_KEY"):
            provider = "groq"
        elif os.getenv("ACTAVA_API_KEY") and _valid_actava_key():
            provider = "actava"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "ollama"

    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise LLMError(
                "GROQ_API_KEY not set. Get a free key (no credit card) at "
                "https://console.groq.com/keys and add it to .env"
            )
        model = os.getenv("GROQ_MODEL", DEFAULT_MODELS["groq"])
        return {"provider": "groq", "model": model, "base_url": BASE_URLS["groq"], "api_key": api_key}

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key:
            raise LLMError("OPENAI_API_KEY not set. Add it to .env or use LLM_PROVIDER=groq/ollama.")
        model = os.getenv("OPENAI_MODEL", DEFAULT_MODELS["openai"])
        base_url = os.getenv("OPENAI_BASE_URL", BASE_URLS["openai"]).rstrip("/")
        return {"provider": "openai", "model": model, "base_url": base_url, "api_key": api_key}

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


def check_llm() -> dict:
    """Health probe. Returns {ok, provider, model, detail}."""
    try:
        cfg = resolve_provider()
    except LLMError as e:
        return {"ok": False, "provider": None, "model": None, "detail": str(e)}

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
                }
        else:
            r = httpx.get(
                f"{cfg['base_url']}/models",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=8.0,
            )
            if r.status_code == 401:
                return {"ok": False, "provider": cfg["provider"], "model": cfg["model"],
                        "detail": f"Invalid {cfg['provider'].upper()} API key"}
            r.raise_for_status()
        return {"ok": True, "provider": cfg["provider"], "model": cfg["model"], "detail": None}
    except httpx.HTTPError as e:
        return {"ok": False, "provider": cfg["provider"], "model": cfg["model"],
                "detail": f"Cannot reach {cfg['provider']}: {e}"}


def ensure_llm() -> dict:
    """Raise LLMError if the configured provider is unusable; return status."""
    status = check_llm()
    if not status["ok"]:
        raise LLMError(status["detail"] or "LLM provider unavailable")
    return status


def chat(
    prompt: str,
    *,
    system: str | None = None,
    json_mode: bool = False,
    max_tokens: int = 1024,
    temperature: float | None = None,
) -> str:
    """Single chat completion. Retries transient failures (429/5xx/network)."""
    cfg = resolve_provider()
    if temperature is None:
        temperature = 0.2 if json_mode else 0.4

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            if cfg["provider"] == "ollama":
                return _chat_ollama(cfg, messages, json_mode, max_tokens, temperature)
            return _chat_openai_compatible(cfg, messages, json_mode, max_tokens, temperature)
        except _RetryableError as e:
            last_error = e
            delay = e.retry_after or RETRY_BASE_DELAY * (2 ** attempt)
            if attempt + 1 < MAX_RETRIES:
                logger.warning("LLM call failed (attempt %d/%d): %s — retrying in %.1fs",
                               attempt + 1, MAX_RETRIES, e, delay)
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
) -> dict:
    """Chat in JSON mode with parse retries."""
    last_error: Exception | None = None
    attempt_prompt = prompt

    for attempt in range(retries):
        try:
            raw = chat(attempt_prompt, system=system, json_mode=True, max_tokens=1024)
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

    # Last resort: decode first {...} object char-by-char
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
