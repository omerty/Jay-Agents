"""Filters for role-inbox / junk contacts that must never enter the qualify queue."""

from __future__ import annotations

import re

# Role-based / non-person "contacts" from privacy pages, footers, etc.
JUNK_CONTACT_NAME_RE = re.compile(
    r"""(?ix)
    ^\s*(
        privacy\s+office |
        privacy\s+team |
        compliance\s+team |
        data\s+protection\s+office |
        for\s+assistance\s+at |
        for\s+your\s+country\s+or\s+region |
        can\s+be\s+emailed\s+at |
        by\s+email |
        per\s+aziende |
        contact\s+us |
        customer\s+service |
        support\s+team |
        info\s+desk |
        webmaster |
        no[\s-]?reply |
        do[\s-]?not[\s-]?reply
    )\s*$
    """,
)

JUNK_EMAIL_LOCAL_RE = re.compile(
    r"(?i)^(privacy|dpo|gdpr|compliance|info|support|noreply|no-reply|donotreply|webmaster)([.+_]|$)"
)


def is_junk_contact_name(name: str | None) -> bool:
    if not name or not str(name).strip():
        return False
    return bool(JUNK_CONTACT_NAME_RE.match(str(name).strip()))


def is_junk_contact_email(email: str | None) -> bool:
    if not email or "@" not in str(email):
        return False
    local = str(email).split("@", 1)[0].strip()
    return bool(JUNK_EMAIL_LOCAL_RE.match(local))


def is_junk_contact(lead: dict | None) -> bool:
    if not lead:
        return False
    if is_junk_contact_name(lead.get("contact_name")):
        return True
    # Role inbox alone is OK if we have a real person name; junk if name also missing/junk
    name = (lead.get("contact_name") or "").strip()
    if (not name or is_junk_contact_name(name)) and is_junk_contact_email(lead.get("email")):
        return True
    return False
