from src.microsoft_mail_api import conversation_has_reply, outlook_draft_url


def test_outlook_draft_url_prefers_web_link():
    assert outlook_draft_url("abc", "https://outlook.office.com/mail/x") == "https://outlook.office.com/mail/x"


def test_outlook_draft_url_fallback():
    url = outlook_draft_url("msg-1")
    assert "outlook.office.com" in url
    assert "msg-1" in url


def test_conversation_has_reply_detects_other_sender():
    messages = [
        {"id": "1", "from": {"emailAddress": {"address": "me@co.com"}}},
        {"id": "2", "from": {"emailAddress": {"address": "jane@acme.com"}}},
    ]
    assert conversation_has_reply(messages, "me@co.com")


def test_conversation_has_reply_ignores_own_messages():
    messages = [
        {"id": "1", "from": {"emailAddress": {"address": "me@co.com"}}},
        {"id": "2", "from": {"emailAddress": {"address": "me@co.com"}}},
    ]
    assert not conversation_has_reply(messages, "me@co.com")


def test_conversation_has_reply_skips_our_message_id():
    messages = [
        {"id": "sent1", "from": {"emailAddress": {"address": "weird@other.com"}}},
    ]
    assert not conversation_has_reply(messages, "me@co.com", our_message_id="sent1")


def test_disconnect_microsoft_removes_token_files(tmp_path, monkeypatch):
    from src import microsoft_mail_api as api

    token = tmp_path / "token.json"
    account = tmp_path / "account.json"
    token.write_text('{"access_token": "x"}')
    account.write_text('{"email": "user@example.com"}')

    monkeypatch.setattr(api, "TOKEN_PATH", token)
    monkeypatch.setattr(api, "ACCOUNT_PATH", account)
    api._oauth_pending["stale"] = {"redirect_uri": "http://x"}
    api._status_cache["value"] = {"connected": True, "email": "user@example.com"}

    status = api.disconnect_microsoft()

    assert not token.exists()
    assert not account.exists()
    assert api._oauth_pending == {}
    assert status.get("disconnected") is True
    assert status["connected"] is False
