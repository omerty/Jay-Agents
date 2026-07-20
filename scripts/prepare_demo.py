"""Demo prep — run before showing Jay: python scripts/prepare_demo.py"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.db import build_prospect_string, init_db, upsert_lead  # noqa: E402

DB_PATH = ROOT / "leads.db"
SENDER = (os.getenv("OUTREACH_SENDER_NAME") or "Jay Swayze").strip()
PLACEHOLDER = "[Your name]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fix_signoffs(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT id, outreach_body FROM leads WHERE outreach_body GLOB '*[Your [Nn]ame]*'"
    ).fetchall()
    now = _now()
    for lead_id, body in rows:
        fixed = body.replace(PLACEHOLDER, SENDER)
        fixed = fixed.replace("[Your name]", SENDER).replace("[your name]", SENDER)
        conn.execute(
            "UPDATE leads SET outreach_body = ?, updated_at = ? WHERE id = ?",
            (fixed, now, lead_id),
        )
    return len(rows)


def _clear_notifications(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE notifications SET read = 1")
    conn.execute("DELETE FROM notifications WHERE message LIKE '%403 Forbidden%'")
    conn.execute("DELETE FROM notifications WHERE message LIKE '%replied to your outreach%'")


def _seed_hero_leads() -> list[str]:
    """Upsert polished showcase leads — returns summary lines."""
    init_db()
    notes: list[str] = []

    heroes = [
        {
            "agent": "woodway",
            "company": "RBC",
            "contact_name": "Sarah Chen",
            "contact_title": "VP Data Governance",
            "email": "sarah.chen@rbc.com",
            "linkedin_url": "https://linkedin.com/in/sarahchen-demo",
            "industry": "banking",
            "employee_count": 95000,
            "signal": "[DEMO] Large Canadian bank — strong EviData fit",
            "score": 100,
            "tier": "hot",
            "outreach_subject": "Quantifying re-identification risk at RBC",
            "outreach_body": f"""Subject: Quantifying re-identification risk at RBC

Hi Sarah,

Woodway Assurance's EviData platform helps data governance teams quantify the actual risk of re-identification in anonymized datasets — giving leaders defensible metrics for privacy and data-sharing programs.

Given RBC's scale and regulatory footprint, I thought a brief conversation might be worthwhile.

Would 15 minutes this week work?

Best,
{SENDER}
Woodway Assurance""",
            "qualification_json": json.dumps({
                "reasons": [
                    "Industry match: banking",
                    "Title match: VP Data Governance",
                    "Company size boost: 95,000 employees (+15)",
                ],
                "talking_points": [
                    "Regulatory pressure on anonymized customer data sharing",
                    "Defensible metrics for AI governance leadership",
                ],
                "recommendation": "Strong fit for EviData. Prioritize outreach.",
                "mode": "demo",
            }),
            "source": "demo",
            "status": "drafted",
        },
        {
            "agent": "woodway",
            "company": "Sun Life",
            "contact_name": "James Okonkwo",
            "contact_title": "Director of Privacy",
            "email": "james.okonkwo@sunlife.com",
            "linkedin_url": "https://linkedin.com/in/jokonkwo-demo",
            "industry": "insurance",
            "employee_count": 40000,
            "signal": "[DEMO] Major insurer — privacy & governance buyer",
            "score": 100,
            "tier": "hot",
            "outreach_subject": "Re-identification risk in anonymized insurance data",
            "outreach_body": f"""Subject: Re-identification risk in anonymized insurance data

Hi James,

Teams like yours at Sun Life are under growing pressure to prove anonymized data is safe to share internally and with partners.

EviData gives privacy and governance leaders a quantitative read on re-identification risk — useful for compliance conversations and AI initiatives.

Open to a short call?

Best,
{SENDER}
Woodway Assurance""",
            "qualification_json": json.dumps({
                "reasons": ["Industry match: insurance", "Title match: Director of Privacy"],
                "talking_points": ["Insurance data sharing with reinsurers and analytics teams"],
                "recommendation": "Strong fit for EviData. Prioritize outreach.",
                "mode": "demo",
            }),
            "source": "demo",
            "status": "drafted",
        },
        {
            "agent": "fonex",
            "company": "Cologix",
            "contact_name": "Marc Tremblay",
            "contact_title": "VP Network Operations",
            "email": "marc.tremblay@cologix.com",
            "linkedin_url": "https://linkedin.com/in/marctremblay-demo",
            "industry": "data centre providers",
            "employee_count": 800,
            "signal": "[DEMO] Canadian colocation provider — optical network upgrade candidate",
            "score": 92,
            "tier": "hot",
            "outreach_subject": "Optical networking for Cologix infrastructure",
            "outreach_body": f"""Subject: Optical networking for Cologix infrastructure

Hi Marc,

FONEX helps data centre and enterprise teams in Canada deploy optical networking from Nokia, Ciena, and Smartoptics — especially during expansion or backbone upgrades.

If Cologix is planning capacity or network refreshes, I'd welcome a brief technical conversation.

Best,
{SENDER}
FONEX""",
            "qualification_json": json.dumps({
                "reasons": [
                    "Industry match: data centre providers",
                    "Title match: VP Network Operations",
                    "Canadian colocation operator",
                ],
                "talking_points": [
                    "DWDM for inter-facility connectivity",
                    "Nokia/Ciena/Smartoptics reseller support in Canada",
                ],
                "recommendation": "Strong fit for FONEX. Prioritize outreach.",
                "mode": "demo",
            }),
            "source": "demo",
            "status": "drafted",
        },
        {
            "agent": "fonex",
            "company": "Telus",
            "contact_name": "Network Operations",
            "contact_title": "VP Network Operations",
            "email": None,
            "industry": "large enterprise",
            "signal": "[DEMO] Blocklist example — telecom service provider (not a FONEX buyer)",
            "score": 70,
            "tier": "warm",
            "outreach_subject": None,
            "outreach_body": None,
            "qualification_json": json.dumps({
                "reasons": ["Would match network ops title, but blocklist applies"],
                "recommendation": "Skip — service provider",
                "mode": "demo",
            }),
            "source": "demo",
            "status": "discovered",
        },
        {
            "agent": "keira",
            "company": "intact public entities",
            "contact_name": "glenn minnis",
            "contact_title": "president",
            "email": None,
            "linkedin_url": "linkedin.com/in/glennminnis",
            "industry": "insurance",
            "employee_count": 125,
            "signal": "[DEMO] Ottawa-area contact — LinkedIn only, no email on file",
            "score": 80,
            "tier": "warm",
            "outreach_subject": "Exploring Strategic Options for Intact Public Entities",
            "outreach_body": f"""Subject: Exploring Strategic Options for Intact Public Entities

Hi Glenn,

Keira Capital Partners works discreetly with business owners in the Ottawa and Eastern Ontario region who are considering strategic options or succession.

If you've ever thought about what a future transition might look like for Intact Public Entities, I'd welcome a confidential conversation — no obligation.

Best,
{SENDER}
Keira Capital Partners Inc.""",
            "qualification_json": json.dumps({
                "reasons": [
                    "Geography match: Eastern Ontario",
                    "Title match: President",
                    "Email not found — LinkedIn is best channel",
                ],
                "talking_points": [
                    "Confidential owner-to-advisor conversation",
                    "Local Ottawa/Eastern Ontario M&A expertise",
                ],
                "recommendation": "Reach out via LinkedIn; try Hunter if email needed.",
                "mode": "demo",
            }),
            "source": "demo",
            "status": "drafted",
        },
        {
            "agent": "keira",
            "company": "Ottawa Precision Manufacturing",
            "contact_name": "Robert Leblanc",
            "contact_title": "Owner & Founder",
            "email": None,
            "linkedin_url": "https://linkedin.com/in/robertleblanc-demo",
            "industry": "manufacturing",
            "employee_count": 180,
            "signal": "[DEMO] Owner-operated manufacturer — ideal Keira sweet spot",
            "score": 88,
            "tier": "hot",
            "outreach_subject": "Confidential — strategic options for Ottawa Precision Manufacturing",
            "outreach_body": f"""Subject: Confidential — strategic options for Ottawa Precision Manufacturing

Hi Robert,

I work with owner-operators in Ottawa who are exploring a potential sale or succession plan on a confidential basis.

If that's ever been on your mind for Ottawa Precision Manufacturing, I'd be glad to share how we typically approach these conversations.

Best,
{SENDER}
Keira Capital Partners Inc.""",
            "qualification_json": json.dumps({
                "reasons": [
                    "Title match: Owner",
                    "Geography match: Ottawa",
                    "Company size adjustment: 180 employees (+15)",
                ],
                "talking_points": ["Family business succession", "Mid-market exit ($10–100M range)"],
                "recommendation": "Strong Keira fit. Prioritize outreach.",
                "mode": "demo",
            }),
            "source": "demo",
            "status": "drafted",
        },
    ]

    for h in heroes:
        h["prospect"] = build_prospect_string(h)
        lead_id, created = upsert_lead(h, agent=h["agent"])
        action = "created" if created else "updated"
        notes.append(f"  {h['agent']}: {h['company']} / {h.get('contact_name', '—')} ({action}, id≈{lead_id})")

    return notes


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        n = _fix_signoffs(conn)
        print(f"Fixed sign-off on {n} outreach drafts (sender: {SENDER})")
        _clear_notifications(conn)
        print("Cleared stale notifications")
        conn.commit()
    finally:
        conn.close()

    print("\nSeeding demo hero leads…")
    for line in _seed_hero_leads():
        print(line)

    print("\n✓ Demo ready. Open http://127.0.0.1:8400 and follow scripts/DEMO.md")


if __name__ == "__main__":
    main()
