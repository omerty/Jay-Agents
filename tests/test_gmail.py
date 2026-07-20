import base64
from email import message_from_bytes

from src.gmail_api import build_mime, strip_subject_line, thread_has_reply


def _decode(raw: str):
    return message_from_bytes(base64.urlsafe_b64decode(raw))


def test_build_mime_headers_and_body():
    raw = build_mime("jane@acme.com", "Hello", "Hi Jane,\n\nBody here.\n\nBest")
    msg = _decode(raw)
    assert msg["To"] == "jane@acme.com"
    assert msg["Subject"] == "Hello"
    assert "Body here." in msg.get_payload()


def test_build_mime_sets_sender():
    raw = build_mime("jane@acme.com", "Hello", "Body", sender="me@co.com")
    msg = _decode(raw)
    assert msg["From"] == "me@co.com"


def test_gmail_draft_url():
    from src.gmail_api import gmail_draft_url

    assert gmail_draft_url("r-123").endswith("compose=r-123")


def test_strip_subject_line():
    body = "Subject: Hello there\n\nHi Jane,\nBody"
    assert strip_subject_line(body) == "Hi Jane,\nBody"
    assert strip_subject_line("Hi Jane,\nBody") == "Hi Jane,\nBody"


def _thread(*senders, ids=None):
    return {
        "messages": [
            {
                "id": (ids or [f"m{i}" for i in range(len(senders))])[i],
                "payload": {"headers": [{"name": "From", "value": s}]},
            }
            for i, s in enumerate(senders)
        ]
    }


def test_thread_has_reply_detects_other_sender():
    t = _thread("Me <me@mydomain.com>", "Jane Doe <jane@acme.com>")
    assert thread_has_reply(t, "me@mydomain.com")


def test_thread_has_reply_ignores_own_messages():
    t = _thread("Me <me@mydomain.com>", "me@mydomain.com")
    assert not thread_has_reply(t, "me@mydomain.com")


def test_thread_has_reply_skips_our_message_id():
    t = _thread("weird@other.com", ids=["sent1"])
    assert not thread_has_reply(t, "me@mydomain.com", our_message_id="sent1")


def test_thread_has_reply_empty_thread():
    assert not thread_has_reply({"messages": []}, "me@mydomain.com")


def test_disconnect_gmail_removes_token_files(tmp_path, monkeypatch):
    from src import gmail_api

    token = tmp_path / "token.json"
    account = tmp_path / "account.json"
    token.write_text('{"token": "x"}')
    account.write_text('{"email": "user@example.com"}')

    monkeypatch.setattr(gmail_api, "TOKEN_PATH", token)
    monkeypatch.setattr(gmail_api, "ACCOUNT_PATH", account)
    gmail_api._oauth_pending["stale"] = object()
    gmail_api._status_cache["value"] = {"connected": True, "email": "user@example.com"}

    status = gmail_api.disconnect_gmail()

    assert not token.exists()
    assert not account.exists()
    assert gmail_api._oauth_pending == {}
    assert status.get("disconnected") is True
    assert status["connected"] is False
