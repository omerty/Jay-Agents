"""Outreach email generation — LLM (real) or templates (mock)."""

import os
import re

SENDER_PLACEHOLDER = "[Your name]"


def outreach_sender_name() -> str:
    return (os.getenv("OUTREACH_SENDER_NAME") or "JayAgents").strip()


def finalize_outreach_body(body: str) -> str:
    """Replace placeholder sign-off with configured sender name."""
    name = outreach_sender_name()
    body = body.replace(SENDER_PLACEHOLDER, name)
    body = re.sub(r"\[Your [Nn]ame\]", name, body)
    return body


def outreach_sign_off(company: str) -> str:
    return f"Best,\n{outreach_sender_name()}\n{company}"


MOCK_TEMPLATES = {
    "woodway": """Subject: Quantifying re-identification risk in your anonymized data

Hi {name_hint},

I noticed {company_hint} is actively working on data governance and privacy — areas where anonymized data sharing is increasingly under scrutiny.

Woodway Assurance's EviData platform helps teams like yours validate the actual risk of re-identification in anonymized datasets — giving Data and AI Governance leaders defensible metrics for compliance and leadership conversations.

Would a 15-minute call make sense to explore whether this is relevant for your current initiatives?

{sign_off}""",
    "fonex": """Subject: Optical networking for {company_hint}

Hi {name_hint},

FONEX helps large enterprises, government organizations, and data centre operators in Canada deploy optical networking from Nokia, Ciena, and Smartoptics.

Given {company_hint}'s infrastructure footprint, I wanted to see if an upgrade or expansion conversation would be timely.

Open to a brief call?

{sign_off}""",
    "keira": """Subject: Confidential — exploring options for {company_hint}

Hi {name_hint},

Keira Capital Partners works with business owners in the Ottawa/Eastern Ontario region who are considering a confidential sale or succession plan.

If you've ever thought about what an exit might look like for {company_hint}, I'd welcome a discreet conversation — no obligation.

{sign_off}""",
}


def generate_outreach(
    config: dict,
    prospect: str,
    qualification: dict,
    *,
    use_llm: bool = True,
    research_context: str | None = None,
    contact: dict | None = None,
) -> dict:
    if use_llm:
        return _generate_with_llm(config, prospect, qualification, research_context, contact)

    name_hint, company_hint = _extract_hints(prospect, contact)
    template = MOCK_TEMPLATES.get(config["name"], MOCK_TEMPLATES["woodway"])
    sign_off = outreach_sign_off(config["company"])
    body = template.format(name_hint=name_hint, company_hint=company_hint, sign_off=sign_off)
    subject = body.split("\n")[0].replace("Subject: ", "")
    return {"subject": subject, "body": body, "mode": "mock", "to_email": contact.get("email") if contact else None}


def _extract_hints(prospect: str, contact: dict | None = None) -> tuple[str, str]:
    if contact and contact.get("contact_name"):
        name = contact["contact_name"]
        first = name.split()[0] if name else "there"
        company = contact.get("company") or "your organization"
        return first, company

    text = prospect.strip()
    company_hint = "your organization"
    name_hint = "there"

    if " at " in text.lower():
        idx = text.lower().rfind(" at ")
        name_hint = text[:idx].strip()
        company_hint = text[idx + 4:].strip()
        # "Sarah Chen, VP Data Governance" → use first name
        if "," in name_hint:
            name_hint = name_hint.split(",")[0].strip().split()[0]
        elif " " in name_hint and not any(t in name_hint.lower() for t in ("vp", "director", "head", "chief")):
            name_hint = name_hint.split()[0]
    else:
        company_hint = text

    return name_hint, company_hint


DEFAULT_OUTREACH_SYSTEM = (
    "You write B2B sales emails. Output the complete email only. No preamble or commentary."
)


def _generate_with_llm(
    config: dict,
    prospect: str,
    qualification: dict,
    research_context: str | None,
    contact: dict | None = None,
) -> dict:
    from .llm import chat, resolve_provider

    prompts = config.get("prompts") or {}
    value_props = "\n".join(f"- {v}" for v in config.get("value_props", []))
    talking_points = "\n".join(f"- {t}" for t in qualification.get("talking_points", []))
    name_hint, company_hint = _extract_hints(prospect, contact)
    sender = outreach_sender_name()

    contact_block = ""
    if contact:
        parts = []
        if contact.get("contact_name"):
            parts.append(f"Name: {contact['contact_name']}")
        if contact.get("contact_title"):
            parts.append(f"Title: {contact['contact_title']}")
        if contact.get("email"):
            parts.append(f"Email: {contact['email']} (do NOT include in email body)")
        if parts:
            contact_block = "CONTACT:\n" + "\n".join(parts) + "\n"

    prompt = f"""Write a short cold outreach email. Output ONLY the email — no intro, no commentary.

SELLER: {config['company']} — {config['product']}
VALUE PROPS:
{value_props}

{contact_block}PROSPECT: {prospect}
QUALIFICATION: {qualification['score']}/100 ({qualification['tier']})
TALKING POINTS:
{talking_points or '- General privacy/governance angle'}

WEB RESEARCH:
{research_context or 'None'}

CUSTOM OUTREACH INSTRUCTIONS (you MUST follow these — they override generic rules when they conflict):
{prompts.get('outreach_extra') or '(none)'}

RULES:
- Under 150 words
- Address recipient as "{name_hint}" (first name if known)
- Tone: {config.get('outreach_tone', 'professional')}
- No hype; do not invent facts or names not in CONTACT/PROSPECT
- CTA: offer a 15-minute call
- Sign off: Best,\\n{sender}\\n{config['company']}

Format:
Subject: <one line>

Hi {name_hint},

<body paragraphs>

Best,
{sender}
{config['company']}"""

    body = chat(
        prompt,
        system=prompts.get("outreach_system") or DEFAULT_OUTREACH_SYSTEM,
    )
    body = finalize_outreach_body(_clean_email(body))

    lines = body.strip().split("\n")
    subject = lines[0].replace("Subject:", "").strip() if lines else "Follow-up"
    return {
        "subject": subject,
        "body": body.strip(),
        "mode": resolve_provider()["provider"],
        "to_email": contact.get("email") if contact else None,
        "to_name": contact.get("contact_name") if contact else None,
    }


def _clean_email(text: str) -> str:
    """Strip model preamble before the Subject line."""
    lines = text.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("subject:"):
            return "\n".join(lines[i:])
    return text.strip()
