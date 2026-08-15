"""Agent prompt templates — view, customize, and persist per agent."""

from pathlib import Path

import yaml

from .agent import AGENTS_DIR, load_agent
from .qualify import DEFAULT_QUALIFY_SYSTEM, _industries_text, _size_text
from .outreach import outreach_sender_name

DEFAULT_OUTREACH_SYSTEM = (
    "You write B2B sales emails. Output the complete email only. No preamble or commentary."
)

DEFAULT_ANALYST_SYSTEM = """You are a senior M&A research analyst for Keira Capital Partners.
Keira advises owners of private Eastern Ontario businesses ($10–100M) on succession / exit options.
Output strict JSON only. Never invent facts not supported by the research packet.
Family-owned alone is NOT exit intent. Prefer reject / research_further over weak outreach.
"""

DEFAULT_CRITIC_SYSTEM = """You are a skeptical M&A quality critic for Keira Capital.
Approve only if the person is a credible owner, company is in Eastern Ontario corridor,
private (not PE/sub/public), size plausible, succession evidence real, and outreach would not embarrass Keira.
Output strict JSON only.
"""

# Shared Anthropic system / instruction keys (all agents)
SHARED_PROMPT_KEYS = ("qualify_system", "qualify_extra", "outreach_system", "outreach_extra")
# Keira-only Claude analyst / critic system prompts
KEIRA_PROMPT_KEYS = ("analyst_system", "critic_system")
PROMPT_KEYS = SHARED_PROMPT_KEYS + KEIRA_PROMPT_KEYS

DEFAULTS = {
    "qualify_system": DEFAULT_QUALIFY_SYSTEM,
    "qualify_extra": "",
    "outreach_system": DEFAULT_OUTREACH_SYSTEM,
    "outreach_extra": "",
    "analyst_system": DEFAULT_ANALYST_SYSTEM,
    "critic_system": DEFAULT_CRITIC_SYSTEM,
}


def prompts_path(agent: str) -> Path:
    return AGENTS_DIR / agent / "prompts.yaml"


def _merged_prompts(cfg: dict) -> dict:
    return {**(cfg.get("prompts") or {})}


def get_prompt_settings(agent: str) -> dict:
    """Return editable values, defaults, and read-only template previews."""
    cfg = load_agent(agent)
    saved = _merged_prompts(cfg)
    keys = list(SHARED_PROMPT_KEYS)
    if agent == "keira":
        keys.extend(KEIRA_PROMPT_KEYS)

    effective = {key: (saved.get(key) or DEFAULTS[key]) for key in keys}
    using_defaults = {
        key: key not in saved or not str(saved.get(key) or "").strip() for key in keys
    }

    return {
        "agent": agent,
        "path": str(prompts_path(agent).relative_to(AGENTS_DIR.parent)),
        "keys": keys,
        "values": effective,
        "using_defaults": using_defaults,
        "defaults": {k: DEFAULTS[k] for k in keys},
        "templates": {
            "qualify_user": qualify_user_template(cfg),
            "outreach_user": outreach_user_template(cfg),
        },
        "notes": {
            "qualify_system": (
                "Anthropic system prompt for qualification, company extract, and digest. "
                "Also used when discovering companies from search results."
            ),
            "outreach_system": "Anthropic system prompt for draft email generation.",
            "analyst_system": "Anthropic system prompt for Keira succession analyst (Step 4).",
            "critic_system": "Anthropic system prompt for Keira critic gate (Step 5).",
        },
    }


def qualify_user_template(config: dict) -> str:
    icp = config.get("icp", {})
    geo = icp.get("geography")
    geo_note = f"\n- Geography: {geo}" if geo else ""
    return f"""You are a B2B sales analyst qualifying prospects.

PRODUCT: {config.get('product')} by {config.get('company')}
TAGLINE: {config.get('tagline')}

IDEAL CUSTOMER PROFILE:
- Industries: {_industries_text(icp)}
- Target titles: {', '.join(icp.get('titles', []))}
- Company size: {_size_text(icp)}{geo_note}
- Disqualifiers: {', '.join(config.get('disqualifiers', []))}

[KNOWN EMPLOYEE COUNT — filled when available]

PROSPECT: [contact + company — filled per lead]

WEB RESEARCH:
[research snippets — filled per lead]

CUSTOM INSTRUCTIONS (you MUST follow these — they override generic scoring when they conflict):
[qualify_extra — your custom instructions from Advanced settings are inserted here when set]

Score this prospect 0-100 against the ICP. Be realistic — use research if helpful.
Apply disqualifiers and blocklist strictly. Custom instructions above take priority.

Respond with ONLY valid JSON (no markdown):
{{
  "score": <integer 0-100>,
  "tier": "<hot|warm|cold>",
  "industries": ["matched industries or empty list"],
  "title": "<matched title or null>",
  "reasons": ["reason 1", "reason 2"],
  "recommendation": "<one sentence action recommendation>",
  "talking_points": ["2-3 specific angles for outreach based on research"]
}}"""


def outreach_user_template(config: dict) -> str:
    value_props = "\n".join(f"- {v}" for v in config.get("value_props", []))
    tone = config.get("outreach_tone", "professional")
    sender = outreach_sender_name()
    company = config.get("company", "")
    return f"""Write a short cold outreach email. Output ONLY the email — no intro, no commentary.

SELLER: {company} — {config.get('product')}
VALUE PROPS:
{value_props or '- (from agent config)'}

CONTACT:
[Name, title, email — filled per lead]

PROSPECT: [filled per lead]
QUALIFICATION: [score/tier — filled per lead]
TALKING POINTS:
[from qualification — filled per lead]

WEB RESEARCH:
[from qualification — filled per lead]

CUSTOM OUTREACH INSTRUCTIONS (you MUST follow these — they override generic rules when they conflict):
[outreach_extra — your custom instructions from Advanced settings are inserted here when set]

RULES:
- Under 150 words
- Address recipient by first name when known
- Tone: {tone}
- No hype; do not invent facts or names not in CONTACT/PROSPECT
- CTA: offer a 15-minute call
- Sign off: Best,\\n{sender}\\n{company}

Format:
Subject: <one line>

Hi [name],

<body paragraphs>

Best,
{sender}
{company}"""


def save_prompt_settings(agent: str, values: dict) -> dict:
    """Persist overrides to agents/<agent>/prompts.yaml. Empty = revert to default."""
    path = prompts_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)

    keys = list(SHARED_PROMPT_KEYS)
    if agent == "keira":
        keys.extend(KEIRA_PROMPT_KEYS)

    cleaned: dict[str, str] = {}
    for key in keys:
        raw = values.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text and text != DEFAULTS[key]:
            cleaned[key] = text

    if cleaned:
        with open(path, "w") as f:
            yaml.safe_dump(cleaned, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    elif path.exists():
        path.unlink()

    return get_prompt_settings(agent)
