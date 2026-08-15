# JayAgents

Sales prospecting agents for Jay's three client engagements — **Woodway Assurance**, **FONEX**, and **Keira Capital**. Each agent discovers companies, pulls named contacts with emails, qualifies them against an ICP with an LLM, and drafts personalized outreach.

## Stack & Costs

| Service | Key? | Cost | Purpose |
|---------|------|------|---------|
| **Groq** (recommended LLM) | Yes (free) | Free tier — no credit card, 1k req/day | Qualify + draft outreach (llama-3.3-70b) |
| **OpenAI** (optional LLM) | Yes | ~$0.15/M input tokens (gpt-4o-mini); a full run costs well under $0.01 | Same, higher quality/reliability |
| **Ollama** (optional LLM) | No | Free (local) | Same, offline fallback |
| **Apollo.io** (recommended contacts) | Yes (free) | Person **search is free** — no credits, 230M+ contacts | Named contacts: name, title, company, LinkedIn |
| **Hunter.io** (optional) | Yes (free) | 25 free email finds/month | Attach work emails to Apollo contacts |
| **People Data Labs** (alternative) | Yes | 100 free credits/month, 1 credit per contact | Contacts incl. work email in one call |
| **DuckDuckGo** | No | Free | Web research + company discovery |
| **Clearbit autocomplete** | No | Free | Company domain lookup |
| **SQLite** | No | Free | Persistence + dedup |

### LLM provider

Set one of these in `.env` (auto-detected in this order):

1. `GROQ_API_KEY` — free key at [console.groq.com/keys](https://console.groq.com/keys), no credit card. **Recommended default.**
2. `OPENAI_API_KEY` — uses `gpt-4o-mini`. Any OpenAI-compatible endpoint works via `OPENAI_BASE_URL` (OpenRouter, Together, …).
3. Neither — falls back to local Ollama (`brew install ollama && ollama pull llama3.2`).

Force a specific provider with `LLM_PROVIDER=groq|openai|ollama`.

### Contact search provider

Auto-detected: `APOLLO_API_KEY` set → Apollo, else PDL. Force with `CONTACTS_PROVIDER=apollo|pdl`.

**Apollo (recommended):** person search is completely free — no credits consumed, unlimited ICP searches. Create a **master** API key at app.apollo.io → Settings → API Keys. Search results don't include emails; emails come from a waterfall:

1. **Hunter.io email finder** (free 25/month) — set `HUNTER_API_KEY`
2. **Apollo enrichment** (1 Apollo credit per contact) — opt-in via `APOLLO_REVEAL_EMAILS=true`

**PDL (alternative):** every contact returned includes a work email, but costs 1 credit each (100 free/month). Sign up at [peopledatalabs.com](https://www.peopledatalabs.com).

Per-agent search filters live in `agents/<name>/config.yaml` under `apollo:` and `pdl:`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add GROQ_API_KEY (or OPENAI_API_KEY) + PDL_API_KEY
```

## Web Dashboard

```bash
source .venv/bin/activate
python -m src.web        # → http://localhost:8400
```

One monitoring screen for all three agents: live stats, pipeline funnel, lead table with search, qualification reasoning, Gmail draft/send controls, reply notifications, and CSV export. The agents themselves run on a schedule (below) — the dashboard is for reviewing and approving.

Configurable via env: `WEB_HOST`, `WEB_PORT`, `LOG_LEVEL`, `LEADS_DB_PATH`, `REPLY_SCAN_MINUTES`.

## Daily Automation (cron)

Agents run automatically once a day — contact search → qualification → outreach drafting → Gmail drafts → reply scan:

```bash
./scripts/setup_cron.sh        # installs cron job at 12:00 AM
./scripts/setup_cron.sh 7      # …or at 7:00 AM
python -m src.daily            # run once manually / test
tail -f logs/daily.log         # watch the runs
```

Tune with `DAILY_CONTACT_LIMIT`, `DAILY_PROCESS_LIMIT`, `DAILY_DRAFT_MIN_SCORE` in `.env`.

## Woodway pipeline

Woodway’s primary flow is company-first, then people, then Outlook drafts:

1. **Web + Claude** (default) — DuckDuckGo search + Anthropic extracts target companies (~1–2 min)
   - Or set `WOODWAY_COMPANY_DISCOVERY=actava` to use a published Actava agent instead (slow)
2. **Digest** — LLM keeps the best ICP fits
3. **Seamless** — find privacy/governance contacts at those companies (falls back to Apollo/PDL if Seamless isn’t configured yet)
4. **Qualify + outreach** — generate email copy
5. **Microsoft 365 drafts** — create Outlook drafts (nothing auto-sends)

In the dashboard (Woodway agent), click **Woodway pipeline**. Or run:

```bash
# via API
curl -X POST http://localhost:8400/api/agents/woodway/run \
  -H 'Content-Type: application/json' \
  -d '{"mode":"woodway_pipeline","limit":10}'
```

Requires `ANTHROPIC_API_KEY` (or Groq). Optional: `ACTAVA_API_KEY` + `ACTAVA_AGENT_ID` if using Actava discovery, `SEAMLESS_API_KEY`, and a connected Microsoft mailbox.

## Gmail Integration

The agent **drafts emails into your Gmail but never sends anything without your explicit confirmation.** Once you send, it watches the thread and notifies you in the dashboard when the prospect replies.

### For end users (no codebase access)

1. Open the dashboard
2. Click **Connect Gmail**
3. Sign in with Google and click **Allow**

That's it. You never touch `credentials.json` or any files.

### For the operator (you, once per deployment)

Someone with server access registers the app with Google **once**. End users only authorize their own Gmail.

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project → enable **Gmail API**
2. OAuth consent screen → External → add test users (or publish the app)
3. Credentials → **OAuth client ID** → **Web application**
4. Authorized redirect URI: `http://YOUR_HOST:8400/api/gmail/oauth/callback`
5. Copy **Client ID** and **Client secret** into server environment variables:

```bash
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
```

For local dev you can use `credentials.json` instead — but hosted deployments should use env vars.

Flow: daily run creates Gmail drafts for qualified leads → user reviews in dashboard → **Send email…** with confirmation → reply scanner notifies on responses.

## Microsoft 365 / Outlook Integration

Same safety model as Gmail: **drafts only until you confirm send.** You can connect Gmail, Outlook, or both.

### For end users

1. Open the dashboard
2. Click **Connect Microsoft Email**
3. Sign in with Microsoft 365 / Outlook and accept permissions

### For the operator (once per deployment)

1. [Azure portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**
2. Supported account types: multitenant + personal Microsoft accounts (or your org only)
3. Redirect URI type **Web**: `http://YOUR_HOST:8400/api/microsoft/oauth/callback`
4. Certificates & secrets → create a **Client secret**
5. API permissions → Microsoft Graph **delegated**: `User.Read`, `Mail.ReadWrite`, `Mail.Send` (grant admin consent if your tenant requires it)
6. Set env vars:

```bash
MICROSOFT_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
MICROSOFT_CLIENT_SECRET=your-client-secret
# optional — default "common" allows work + personal Microsoft accounts
#MICROSOFT_TENANT_ID=common
```

Daily automation prefers Gmail when both mailboxes are connected; otherwise it uses Outlook.

## Manual CLI (optional)

```bash
# Contact search (Apollo/PDL auto-detected)
python -m src.pdl_cli search --limit 25

# Qualify + draft outreach
python -m src.demo woodway --process-imported

# Export for HubSpot / spreadsheet
python -m src.demo woodway --export leads.csv

# CSV fallback import
python -m src.pdl_cli import samples/contacts-sample.csv

# Web discover (companies only, no emails)
python -m src.demo woodway
```

## CLI Reference

```bash
# PDL contact search
python -m src.pdl_cli search --limit 25
python -m src.pdl_cli import contacts.csv
python -m src.pdl_cli status
python -m src.pdl_cli export out.csv

# Agent (woodway | fonex | keira)
python -m src.demo woodway --process-imported
python -m src.demo woodway --export leads.csv
python -m src.demo woodway                         # web discover
python -m src.demo woodway -p "VP Data Governance at RBC"   # qualify one prospect
python -m src.pdl_cli search --agent fonex --limit 25
python -m src.demo fonex --process-imported
python -m src.demo keira --mock                    # no-LLM demo mode
```

Search filters live in each agent's `agents/<name>/config.yaml` under `pdl:`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

## Agents

| Agent | Status | Plays |
|-------|--------|-------|
| `woodway` | **Live** | EviData → VP/Director Data Governance & Privacy at large pharma/banking/insurance/healthcare/tech |
| `fonex` | **Live** | Optical networking → Infrastructure/Network Ops leaders at Canadian enterprise, government, and data centre providers |
| `keira` | **Live** | M&A advisory → Owners/founders of $10–100M businesses in Ottawa/Eastern Ontario (exit-intent signals) |
# Jay-Agents
