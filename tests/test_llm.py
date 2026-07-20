import json

import pytest


def test_parse_json_plain(reload_llm):
    llm = reload_llm()
    assert llm._parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced(reload_llm):
    llm = reload_llm()
    text = 'Here you go:\n```json\n{"score": 80, "tier": "hot"}\n```'
    assert llm._parse_json(text) == {"score": 80, "tier": "hot"}


def test_parse_json_repairs_trailing_comma_and_python_literals(reload_llm):
    llm = reload_llm()
    assert llm._parse_json('{"a": True, "b": None,}') == {"a": True, "b": None}


def test_parse_json_raises_on_garbage(reload_llm):
    llm = reload_llm()
    with pytest.raises(llm.LLMError):
        llm._parse_json("no json here at all")


def test_provider_autodetect_groq(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("GROQ_API_KEY", "gk_test")
    cfg = llm.resolve_provider()
    assert cfg["provider"] == "groq"
    assert cfg["model"] == "llama-3.3-70b-versatile"
    assert "groq.com" in cfg["base_url"]


def test_provider_autodetect_openai(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("OPENAI_API_KEY", "sk_test")
    cfg = llm.resolve_provider()
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "gpt-4o-mini"


def test_provider_fallback_ollama(reload_llm):
    llm = reload_llm()
    cfg = llm.resolve_provider()
    assert cfg["provider"] == "ollama"
    assert cfg["api_key"] is None


def test_provider_explicit_without_key_raises(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    with pytest.raises(llm.LLMError):
        llm.resolve_provider()


def test_provider_unknown_raises(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("LLM_PROVIDER", "banana")
    with pytest.raises(llm.LLMError):
        llm.resolve_provider()


def test_chat_openai_compatible_parses_response(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("GROQ_API_KEY", "gk_test")

    class FakeResponse:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    out = llm.chat("hi", json_mode=True)
    assert json.loads(out) == {"ok": True}
    assert captured["url"].endswith("/chat/completions")
    assert captured["json"]["response_format"] == {"type": "json_object"}


def test_chat_retries_on_500_then_succeeds(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("GROQ_API_KEY", "gk_test")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    calls = {"n": 0}

    class Bad:
        status_code = 500
        headers = {}
        text = "server error"

    class Good:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "hello"}}]}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return Bad() if calls["n"] == 1 else Good()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.chat("hi") == "hello"
    assert calls["n"] == 2


def test_chat_gives_up_after_max_retries(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("GROQ_API_KEY", "gk_test")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class Bad:
        status_code = 503
        headers = {}
        text = "unavailable"

    monkeypatch.setattr(llm.httpx, "post", lambda url, **kw: Bad())
    with pytest.raises(llm.LLMError):
        llm.chat("hi")


def test_chat_401_fails_fast(reload_llm, monkeypatch):
    llm = reload_llm()
    monkeypatch.setenv("GROQ_API_KEY", "bad_key")
    calls = {"n": 0}

    class Unauthorized:
        status_code = 401
        headers = {}
        text = "unauthorized"

    def fake_post(url, **kwargs):
        calls["n"] += 1
        return Unauthorized()

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    with pytest.raises(llm.LLMError, match="401"):
        llm.chat("hi")
    assert calls["n"] == 1  # auth errors must not be retried
