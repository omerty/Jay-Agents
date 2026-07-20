import importlib

import pytest


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
    for var in ("LLM_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "GROQ_MODEL", "OPENAI_MODEL", "OLLAMA_HOST", "OLLAMA_MODEL"):
        monkeypatch.delenv(var, raising=False)

    def _reload():
        from src import llm
        return importlib.reload(llm)

    return _reload
