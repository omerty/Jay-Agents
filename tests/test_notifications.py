def test_notifications_lifecycle(tmp_db):
    n1 = tmp_db.create_notification("Jane replied", agent="woodway", lead_id=1)
    tmp_db.create_notification("Daily run finished")

    items = tmp_db.get_notifications()
    assert len(items) == 2
    assert all(not n["read"] for n in items)

    unread = tmp_db.get_notifications(unread_only=True)
    assert len(unread) == 2

    tmp_db.mark_notifications_read([n1])
    unread = tmp_db.get_notifications(unread_only=True)
    assert len(unread) == 1
    assert unread[0]["message"] == "Daily run finished"

    tmp_db.mark_notifications_read()
    assert tmp_db.get_notifications(unread_only=True) == []


def test_run_history(tmp_db):
    assert tmp_db.last_run() is None
    tmp_db.record_run("daily", ok=True, summary="all good", started_at="2026-01-01T00:00:00")
    tmp_db.record_run("reply_scan", ok=False, summary="oops", started_at="2026-01-02T00:00:00")

    assert tmp_db.last_run("daily")["summary"] == "all good"
    assert tmp_db.last_run("reply_scan")["ok"] == 0
    assert tmp_db.last_run() is not None


def test_set_lead_fields(tmp_db):
    lead_id, _ = tmp_db.upsert_lead(
        {"company": "Acme", "email": "j@acme.com", "source": "apollo", "status": "drafted"},
        agent="woodway",
    )
    tmp_db.set_lead_fields(lead_id, gmail_draft_id="d1", gmail_thread_id="t1")
    lead = tmp_db.get_lead(lead_id)
    assert lead["gmail_draft_id"] == "d1"
    assert lead["gmail_thread_id"] == "t1"

    tmp_db.set_lead_fields(lead_id, status="emailed", emailed_at="2026-01-01T00:00:00")
    assert tmp_db.get_lead(lead_id)["status"] == "emailed"


def test_set_lead_fields_rejects_bad_input(tmp_db):
    lead_id, _ = tmp_db.upsert_lead({"company": "Acme", "source": "csv"}, agent="woodway")
    import pytest

    with pytest.raises(ValueError):
        tmp_db.set_lead_fields(lead_id, company="Hacked")
    with pytest.raises(ValueError):
        tmp_db.set_lead_fields(lead_id, status="bogus")
