"""Pre-flight checks before spending Claude / Seamless credits."""

from __future__ import annotations

import os


class PipelinePreflightError(Exception):
    """Raised when a pipeline should abort before paid work."""


def mailbox_ready() -> dict:
    """Return which outbound mailboxes can create drafts."""
    from .gmail_api import gmail_status
    from .microsoft_mail_api import microsoft_status

    gmail = gmail_status()
    ms = microsoft_status()
    ready = bool(gmail.get("connected") or ms.get("connected"))
    return {
        "ready": ready,
        "gmail": gmail,
        "microsoft": ms,
        "detail": (
            None
            if ready
            else "No mailbox connected — connect Gmail or Microsoft Email before running the pipeline"
        ),
    }


def require_mailbox_for_pipeline() -> bool:
    return os.getenv("REQUIRE_MAILBOX_FOR_PIPELINE", "true").lower() not in (
        "0", "false", "no", "off",
    )


def assert_mailbox_ready(*, create_drafts: bool = True) -> dict:
    """
    Abort early when drafts are requested but no mailbox is connected.
    Set REQUIRE_MAILBOX_FOR_PIPELINE=false to bypass (research-only runs).
    """
    status = mailbox_ready()
    if not create_drafts:
        return status
    if not require_mailbox_for_pipeline():
        return status
    if not status["ready"]:
        raise PipelinePreflightError(status["detail"])
    return status
