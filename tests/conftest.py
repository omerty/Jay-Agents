import importlib

import pytest


@pytest.fixture(autouse=True)
def woodway_test_defaults(monkeypatch):
    """Keep unit tests on single-pass qualify and without extra LLM critique calls."""
    monkeypatch.setenv("QUALIFY_TWO_PASS", "false")
    monkeypatch.setenv("OUTREACH_CRITIQUE", "false")
    monkeypatch.setenv("SEQUENCE_ENABLED", "false")
    monkeypatch.setenv("SIGNALS_ENABLED", "false")
    monkeypatch.setenv("OUTREACH_REQUIRE_CITATIONS", "false")
    monkeypatch.setenv("ACCOUNT_BRIEFS_ENABLED", "false")
    monkeypatch.setenv("ACCOUNT_BRIEF_FILINGS", "false")
    monkeypatch.setenv("EMAIL_VERIFY_BEFORE_DRAFT", "false")


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point src.db at a throwaway SQLite file for the duration of a test."""
    from src import db

    path = tmp_path / "test-leads.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db._initialized_paths.discard(str(path))
    yield db
    db._initialized_paths.discard(str(path))


@pytest.fixture()
def reload_llm(monkeypatch):
    """Reload src.llm with a clean env so provider auto-detection is testable."""
    for var in (
        "LLM_PROVIDER", "LLM_FALLBACK",
        "GROQ_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ACTAVA_API_KEY",
        "OPENAI_BASE_URL", "GROQ_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL",
        "OLLAMA_HOST", "OLLAMA_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)

    def _reload():
        from src import llm
        return importlib.reload(llm)

    return _reload
