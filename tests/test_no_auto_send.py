"""Auto-send hardening — pipelines must never import or call mail send APIs."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"

# Modules that create outreach / run on timers — must never reach send_*.
PIPELINE_MODULES = (
    "woodway_pipeline.py",
    "keira_pipeline.py",
    "sequences.py",
    "daily.py",
    "outreach.py",
    "pipeline.py",
    "agent.py",
)

SEND_NAMES = frozenset({"send_message", "send_draft"})


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
    return names


@pytest.mark.parametrize("module", PIPELINE_MODULES)
def test_pipeline_modules_do_not_import_send(module: str):
    path = ROOT / module
    assert path.exists(), f"missing {path}"
    imported = _imported_names(path)
    bad = imported & SEND_NAMES
    assert not bad, f"{module} imports send APIs {bad} — pipelines may only create_draft"


def test_only_web_imports_send_from_mail_apis():
    """send_message / send_draft may be defined in mail APIs and used by web.py only."""
    offenders = []
    for path in ROOT.glob("*.py"):
        if path.name in ("gmail_api.py", "microsoft_mail_api.py", "web.py"):
            continue
        imported = _imported_names(path)
        hit = imported & SEND_NAMES
        if hit:
            offenders.append(f"{path.name}: {sorted(hit)}")
    assert not offenders, "send_* leaked outside web/mail APIs:\n" + "\n".join(offenders)


def test_create_mailbox_drafts_never_calls_send(tmp_db, monkeypatch):
    monkeypatch.setenv("REQUIRE_MAILBOX_FOR_PIPELINE", "false")
    monkeypatch.setenv("EMAIL_VERIFY_BEFORE_DRAFT", "false")

    send_calls: list[str] = []
    draft_calls: list[str] = []

    def boom_send(*_a, **_k):
        send_calls.append("send")
        raise AssertionError("send must not be called from pipeline")

    def fake_draft(to, subject, body):
        draft_calls.append(to)
        return {
            "draft_id": "d1",
            "message_id": "m1",
            "thread_id": "t1",
            "sender": "jay@example.com",
            "gmail_url": "https://mail.google.com/draft/d1",
        }

    monkeypatch.setattr("src.gmail_api.send_message", boom_send)
    monkeypatch.setattr("src.gmail_api.send_draft", boom_send)
    monkeypatch.setattr("src.microsoft_mail_api.send_message", boom_send)
    monkeypatch.setattr("src.microsoft_mail_api.send_draft", boom_send)
    monkeypatch.setattr("src.gmail_api.create_draft", fake_draft)
    monkeypatch.setattr(
        "src.gmail_api.gmail_status",
        lambda: {"connected": True, "email": "jay@example.com"},
    )
    monkeypatch.setattr(
        "src.microsoft_mail_api.microsoft_status",
        lambda: {"connected": False},
    )

    from src.db import upsert_lead
    from src.woodway_pipeline import create_mailbox_drafts

    upsert_lead(
        {
            "company": "Acme Pharma",
            "contact_name": "Jane Doe",
            "contact_title": "VP Privacy",
            "email": "jane@acme.com",
            "score": 80,
            "status": "drafted",
            "outreach_subject": "Quick question",
            "outreach_body": "Hi Jane,\n\nWould love to chat.\n",
            "source": "test",
        },
        agent="woodway",
    )

    out = create_mailbox_drafts("woodway", prefer_microsoft=False)
    assert out["created"] >= 1
    assert draft_calls == ["jane@acme.com"]
    assert send_calls == []


def test_sequence_advance_never_calls_send(tmp_db, monkeypatch):
    monkeypatch.setenv("SEQUENCE_ENABLED", "true")
    monkeypatch.setenv("REQUIRE_MAILBOX_FOR_PIPELINE", "false")

    send_calls: list[str] = []

    def boom_send(*_a, **_k):
        send_calls.append("send")
        raise AssertionError("sequence must not auto-send")

    def fake_draft(to, subject, body):
        return {"draft_id": "sd1", "message_id": "sm1", "thread_id": "st1"}

    monkeypatch.setattr("src.gmail_api.send_message", boom_send)
    monkeypatch.setattr("src.gmail_api.send_draft", boom_send)
    monkeypatch.setattr("src.microsoft_mail_api.send_message", boom_send)
    monkeypatch.setattr("src.microsoft_mail_api.send_draft", boom_send)
    monkeypatch.setattr("src.gmail_api.create_draft", fake_draft)
    monkeypatch.setattr(
        "src.gmail_api.gmail_status",
        lambda: {"connected": True, "email": "jay@example.com"},
    )
    monkeypatch.setattr(
        "src.microsoft_mail_api.microsoft_status",
        lambda: {"connected": False},
    )
    monkeypatch.setattr(
        "src.sequences.get_due_sequence_steps",
        lambda **_k: [
            {
                "id": 1,
                "lead_id": 1,
                "step_number": 2,
                "channel": "email",
                "email": "owner@example.com",
                "subject": "Follow-up",
                "content": "Hi — circling back.",
                "contact_tier": "A",
            }
        ],
    )
    monkeypatch.setattr("src.sequences.mark_step_status", lambda *_a, **_k: None)
    monkeypatch.setattr("src.db.set_lead_fields", lambda *_a, **_k: None)

    from src.sequences import advance_due_sequences

    out = advance_due_sequences(agent="woodway", create_drafts=True)
    assert out["advanced"] == 1
    assert send_calls == []
