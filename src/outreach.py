"""Outreach email generation — LLM (real) or templates (mock)."""

import logging
import os
import re
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

SENDER_PLACEHOLDER = "[Your name]"

AGENTS_DIR = Path(__file__).parent.parent / "agents"

PERSONA_ANGLES = {
    "chief privacy officer": "Defensibility — can you evidence the re-identification risk assessment to a regulator?",
    "cpo": "Defensibility — can you evidence the re-identification risk assessment to a regulator?",
    "vp data governance": "Scale — manual expert determination doesn't survive 400 datasets.",
    "director of privacy": "Defensibility and audit consistency across business units.",
    "chief data officer": "Revenue unblock — data-sharing deals stall in privacy review; quantified risk shortens the gate.",
    "head of ai governance": "Training-data provenance and model-inversion risk on de-identified corpora.",
    "head of data sharing": "Speed to partner — quantified risk lets you say yes faster.",
    "director of data sharing": "Speed to partner — quantified risk lets you say yes faster.",
    "legal": "Auditability and consistency of anonymization method across BUs.",
    "compliance": "Auditability and consistency of anonymization method across BUs.",
}


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
    trigger_signal: dict | None = None,
    account_brief: dict | None = None,
) -> dict:
    if use_llm:
        return _generate_with_llm(
            config, prospect, qualification, research_context, contact,
            trigger_signal=trigger_signal,
            account_brief=account_brief,
        )

    name_hint, company_hint = _extract_hints(prospect, contact)
    template = MOCK_TEMPLATES.get(config["name"], MOCK_TEMPLATES["woodway"])
    sign_off = outreach_sign_off(config["company"])
    body = template.format(name_hint=name_hint, company_hint=company_hint, sign_off=sign_off)
    subject = body.split("\n")[0].replace("Subject: ", "")
    return {
        "subject": subject,
        "body": body,
        "mode": "mock",
        "to_email": contact.get("email") if contact else None,
        "citation_ok": True,
        "citations": [{"sentence": "EviData quantifies re-identification risk in anonymized datasets.", "source": "proof:evidata_core"}],
    }


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
    trigger_signal: dict | None = None,
    account_brief: dict | None = None,
) -> dict:
    from .llm import chat, resolve_provider
    from .llm_optimize import task_defaults, truncate_context
    from .account_brief import brief_citation_ids
    from .evidence_bound import (
        allowed_proof_ids,
        extract_citations_via_llm,
        validate_citations,
    )

    prompts = config.get("prompts") or {}
    defaults = task_defaults("outreach")
    value_props = "\n".join(f"- {v}" for v in config.get("value_props", []))
    talking_points = "\n".join(f"- {t}" for t in qualification.get("talking_points", []))
    name_hint, company_hint = _extract_hints(prospect, contact)
    sender = outreach_sender_name()
    agent_name = config.get("name") or "woodway"

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

    research_trimmed = truncate_context(research_context, defaults["context_chars"])
    persona = _persona_angle(contact)
    proof_block = _proof_library_block(agent_name, contact)

    trigger_block = ""
    if trigger_signal:
        trigger_block = (
            f"\nMUST open with this trigger (paraphrase, cite fact):\n"
            f"- {trigger_signal.get('label') or trigger_signal.get('signal_type')}: "
            f"{(trigger_signal.get('snippet') or '')[:200]}\n"
        )

    system = prompts.get("outreach_system") or DEFAULT_OUTREACH_SYSTEM
    system = f"""{system}

SELLER: {config['company']} — {config['product']}
VALUE PROPS:
{value_props}
Tone: {config.get('outreach_tone', 'professional')}
HARD CONSTRAINT: Do NOT pitch "you should de-identify." Pitch: "your de-identification needs a defensible, repeatable review."
HARD CONSTRAINT: Every product/factual claim must be supportable from APPROVED CLAIMS or ACCOUNT BRIEF — invent nothing.
{f"PERSONA ANGLE: {persona}" if persona else ""}
APPROVED CLAIMS ONLY (do not invent customers or stats):
{proof_block}
{f"OUTREACH NOTES:{chr(10)}{prompts.get('outreach_extra')}" if prompts.get('outreach_extra') else ""}"""

    prompt = f"""Write a short cold outreach email. Output ONLY the email — no intro, no commentary.

{contact_block}PROSPECT: {prospect}
QUALIFICATION: {qualification['score']}/100 ({qualification['tier']})
TALKING POINTS:
{talking_points or '- General privacy/governance angle'}
{trigger_block}
ACCOUNT BRIEF + RESEARCH (cite facts from here — do not invent):
{research_trimmed or 'None'}

RULES:
- ≤120 words, ≤5-word lowercase subject line
- First sentence states a specific trigger/fact from the brief/research — no flattery
- Exactly one ask — a question, not a calendar link
- No links or attachments
- No ROI percentages, named customers, or regulatory approval claims
- Address recipient as "{name_hint}"
- No banned phrases: hope this finds you well, I came across, revolutionize, cutting-edge, seamlessly
- Sign off: Best,\\n{sender}\\n{config['company']}

Format:
Subject: <one line>

Hi {name_hint},

<body>

Best,
{sender}
{config['company']}"""

    body = chat(
        prompt,
        system=system,
        max_tokens=defaults["max_tokens"],
        temperature=defaults["temperature"],
        task="outreach",
    )
    body = finalize_outreach_body(_woodway_anti_slop(_clean_email(body)))
    body = _append_opt_out(body)

    critique = critique_draft(body, config=config)
    if critique.get("score", 5) < 3 and critique.get("rewrite"):
        body = critique["rewrite"]
        body = finalize_outreach_body(_woodway_anti_slop(_clean_email(body)))
        body = _append_opt_out(body)

    # Evidence binding — annotate + reject uncited claims
    from .evidence_bound import citation_enforcement_enabled

    citations: list = []
    validation = {"ok": True, "issues": [], "citations": []}
    if citation_enforcement_enabled():
        allowed = {f"proof:{pid}" for pid in allowed_proof_ids(agent_name)} | allowed_proof_ids(agent_name)
        allowed |= brief_citation_ids(account_brief)
        if trigger_signal and trigger_signal.get("id") is not None:
            allowed.add(f"signal:{trigger_signal['id']}")
            allowed.add("signal")

        citations = extract_citations_via_llm(body, allowed=allowed, use_llm=True)
        validation = validate_citations(
            body, citations, agent=agent_name, allowed_extra=allowed,
        )
        if not validation["ok"]:
            logger.info("Citation gate failed for %s: %s", company_hint, validation["issues"][:3])
            # One repair attempt: strip and rewrite under stricter constraint
            repair_prompt = f"""Rewrite this email so EVERY factual/product sentence is supportable from the brief or approved claims.
Remove any uncited assertion. Keep ≤120 words.

ISSUES: {validation['issues'][:5]}
BRIEF/RESEARCH:
{research_trimmed or 'none'}

DRAFT:
{body[:1500]}

Output complete email only (Subject: ...)."""
            try:
                repaired = chat(
                    repair_prompt,
                    system=system,
                    max_tokens=defaults["max_tokens"],
                    temperature=0.3,
                    task="outreach",
                )
                body = finalize_outreach_body(_woodway_anti_slop(_clean_email(repaired)))
                body = _append_opt_out(body)
                citations = extract_citations_via_llm(body, allowed=allowed, use_llm=True)
                validation = validate_citations(
                    body, citations, agent=agent_name, allowed_extra=allowed,
                )
            except Exception as e:
                logger.warning("Citation repair failed: %s", e)

    lines = body.strip().split("\n")
    subject = lines[0].replace("Subject:", "").strip() if lines else "Follow-up"
    result = {
        "subject": subject,
        "body": body.strip(),
        "mode": resolve_provider()["provider"],
        "to_email": contact.get("email") if contact else None,
        "to_name": contact.get("contact_name") if contact else None,
        "citations": validation.get("citations") or citations,
        "citation_ok": bool(validation.get("ok")),
        "citation_issues": validation.get("issues") or [],
    }
    if agent_name == "keira":
        conf = check_keira_confidentiality(result["subject"], result["body"])
        if not conf["ok"]:
            result["citation_ok"] = False
            result["citation_issues"] = list(result.get("citation_issues") or []) + conf["issues"]
            result["confidentiality_ok"] = False
            logger.info("Keira confidentiality gate failed: %s", conf["issues"][:3])
        else:
            result["confidentiality_ok"] = True
    return result


def check_keira_confidentiality(subject: str, body: str) -> dict:
    """Hard content filter — block drafts that leak sources or claim the owner is selling."""
    text = f"{subject or ''}\n{body or ''}"
    issues: list[str] = []
    banned = (
        (r"\bseamless\.?ai\b", "mentions Seamless"),
        (r"\bapollo\.?io\b", "mentions Apollo"),
        (r"\bhunter\.io\b", "mentions Hunter"),
        (r"\bbizbuysell\b", "mentions BizBuySell"),
        (r"\bsunbelt\b", "mentions Sunbelt"),
        (r"\bdealstream\b", "mentions DealStream"),
        (r"\baxial\b", "mentions Axial"),
        (r"\bwe (saw|noticed|found) you('re| are) (selling|for sale)\b", "claims owner is selling"),
        (r"\byou('re| are) (looking to sell|seeking (a )?buyer|for sale)\b", "claims owner seeking buyer"),
        (r"\blisted (your |the )?business\b", "implies listing knowledge"),
    )
    for pattern, label in banned:
        if re.search(pattern, text, re.I):
            issues.append(label)
    return {"ok": len(issues) == 0, "issues": issues}


def _clean_email(text: str) -> str:
    """Strip model preamble before the Subject line."""
    lines = text.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("subject:"):
            return "\n".join(lines[i:])
    return text.strip()


WOODWAY_BANNED_PHRASES = (
    "hope this finds you well",
    "reach out",
    "touch base",
    "synergy",
    "leverage",
    "best-in-class",
    "game-changer",
    "excited to",
    "in today's fast-paced",
)


def _woodway_anti_slop(text: str) -> str:
    out = text
    for phrase in WOODWAY_BANNED_PHRASES:
        out = re.sub(re.escape(phrase), "", out, flags=re.I)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def generate_linkedin_note(
    config: dict,
    prospect: str,
    qualification: dict,
    *,
    contact: dict | None = None,
    use_llm: bool = True,
    research_context: str | None = None,
) -> str:
    """≤300 char LinkedIn connection note for tier-C leads."""
    name_hint, company_hint = _extract_hints(prospect, contact)
    if not use_llm:
        return (
            f"Hi {name_hint} — I work with {config['company']} on anonymized data risk. "
            f"Saw your role at {company_hint}; open to connecting?"
        )[:300]

    from .llm import chat
    from .llm_optimize import task_defaults, truncate_context

    defaults = task_defaults("outreach")
    system = (
        f"You write ultra-short LinkedIn connection notes for {config['company']}. "
        "Output ONLY the note text — no quotes, no preamble. Max 280 characters."
    )
    prompt = f"""Write a LinkedIn connection request note (≤280 chars).

Contact: {prospect}
Company: {company_hint}
Score: {qualification.get('score')}/100
Context: {truncate_context(research_context or '', 800)}

Rules:
- First sentence references something specific from context if available
- Mention re-identification risk or data governance only if natural
- No sales pitch, no links, no "hope this finds you well"
- End with a simple connect ask"""

    note = chat(prompt, system=system, max_tokens=120, temperature=0.4, task="outreach")
    note = _woodway_anti_slop(note.strip().strip('"'))
    return note[:300]


def load_proof_library(agent: str = "woodway") -> dict:
    path = AGENTS_DIR / agent / "proof_library.yaml"
    if not path.exists():
        return {"claims": [], "forbidden": []}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _proof_library_block(agent: str, contact: dict | None) -> str:
    lib = load_proof_library(agent)
    title = ((contact or {}).get("contact_title") or "").lower()
    claims = lib.get("claims") or []
    lines = []
    for c in claims[:6]:
        tags = c.get("tags") or []
        if not tags or any(t in title for t in tags) or "core" in tags or "product" in tags:
            lines.append(f"- {c.get('text', '')}")
    return "\n".join(lines) or "- Use only general product description from value props."


def _persona_angle(contact: dict | None) -> str | None:
    if not contact:
        return None
    title = (contact.get("contact_title") or "").lower()
    for key, angle in PERSONA_ANGLES.items():
        if key in title:
            return angle
    return None


def _append_opt_out(body: str) -> str:
    from .compliance import opt_out_footer
    footer = opt_out_footer()
    if footer and footer.strip() not in body:
        return body.rstrip() + footer
    return body


def critique_draft(body: str, *, config: dict | None = None) -> dict:
    """Haiku self-critique — score 1-5, optional rewrite."""
    if os.getenv("OUTREACH_CRITIQUE", "true").lower() in ("0", "false", "no"):
        return {"score": 5}
    from .llm import chat_json

    try:
        result = chat_json(
            f"""Score this B2B email draft 1-5 on: trigger specificity, no cliché, persona fit, single CTA, factual grounding.
If score < 3, provide a rewrite.

DRAFT:
{body[:1500]}

JSON: {{"score": 1-5, "issues": ["..."], "rewrite": "full improved email or null"}}""",
            system="You critique sales emails. JSON only.",
            max_tokens=512,
            temperature=0.1,
            task="critique",
        )
        return result
    except Exception:
        return {"score": 5}


def generate_sequence_touch(
    config: dict,
    prospect: str,
    qualification: dict,
    *,
    touch: int,
    contact: dict | None = None,
    use_llm: bool = True,
    research_context: str | None = None,
) -> dict:
    """Generate touch 3 (bump) or touch 4 (breakup)."""
    if not use_llm:
        if touch == 4:
            return {"subject": "close the loop?", "body": f"Subject: close the loop?\n\nHi — should I close this out, or is there someone better to speak with about anonymized data risk at your org?\n\nBest,\n{outreach_sender_name()}"}
        return {"subject": "Re: following up", "body": "Subject: Re: following up\n\nHi — circling back with a quick bump.\n\nBest"}

    from .llm import chat
    from .llm_optimize import task_defaults, truncate_context

    defaults = task_defaults("outreach")
    name_hint, _ = _extract_hints(prospect, contact)
    kind = "breakup/referral ask" if touch == 4 else "reply-in-thread bump with new proof point"
    prompt = f"""Write touch {touch} ({kind}) for {prospect}. ≤80 words. One question CTA. No links.

Context: {truncate_context(research_context, 600) or 'none'}
Score: {qualification.get('score')}

Format:
Subject: ...
Hi {name_hint}, ..."""

    body = chat(prompt, system="Output complete email only.", max_tokens=300, temperature=0.4, task="outreach")
    body = _woodway_anti_slop(_clean_email(body))
    lines = body.strip().split("\n")
    subject = lines[0].replace("Subject:", "").strip() if lines else "Follow-up"
    return {"subject": subject, "body": body}
