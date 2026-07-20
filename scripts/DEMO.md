# JayAgents — Demo script for Jay

**Before the meeting:**
```bash
cd JayAgents
source .venv/bin/activate
python scripts/prepare_demo.py
python -m src.web          # → http://127.0.0.1:8400
```
Hard refresh the browser: `Cmd+Shift+R`

**System check:** green dots for LLM, Contacts (PDL), Gmail connected.

---

## 10-minute flow (recommended)

### 1. Overview (30 sec)
- Three agents in the sidebar — one per client engagement
- Agents find prospects → score against ICP → draft outreach → hand off to Gmail
- **Nothing sends automatically**

### 2. Woodway — full email path (2 min)
**Agent:** Woodway Assurance

| Demo piece | Lead to open |
|------------|--------------|
| Hot score + drafted outreach | **RBC — Sarah Chen** |
| Second email example | **Sun Life — James Okonkwo** |

**Show:**
- Pipeline funnel + stats (3 with email, drafted count)
- Click **Sarah Chen** → score 100, qualification reasons, outreach draft signed "Jay Swayze"
- **Outreach channel** column shows her email
- Click **Import to Gmail** → draft appears in connected Gmail (review only — don't send to real addresses in demo)

**Say:** *"EviData targets large pharma, banking, insurance, healthcare — VP/Director privacy and data governance roles."*

---

### 3. Keira — LinkedIn + Hunter path (2 min)
**Agent:** Keira Capital

| Demo piece | Lead to open |
|------------|--------------|
| LinkedIn-only contact | **intact public entities — Glenn Minnis** |
| Ideal owner profile | **Ottawa Precision Manufacturing — Robert Leblanc** |

**Show:**
- Stats: **LinkedIn only** count
- Open **Glenn Minnis** → banner: *"Email not found — LinkedIn is the best channel"*
- **Open LinkedIn** button (not Gmail)
- **Use Hunter for research (not guaranteed)** — optional live click (uses 1 Hunter credit)
- Open **Robert Leblanc** → owner, Ottawa, mid-size manufacturer — ideal $10–100M proxy

**Say:** *"Keira is sector-agnostic — we hunt owners in Ottawa/Eastern Ontario. PDL often gives LinkedIn but not email; Hunter is on-demand per contact."*

---

### 4. FONEX — ICP + blocklist (2 min)
**Agent:** FONEX

| Demo piece | Lead to open |
|------------|--------------|
| Good buyer | **Cologix — Marc Tremblay** |
| Auto-skipped telco | **Telus** (search "telus" in filter) |

**Show:**
- **Marc Tremblay** — VP Network Ops at Canadian DC provider, drafted outreach mentions Nokia/Ciena/Smartoptics
- **Import to Gmail** works (has email)
- Filter/search **Telus** → explain blocklist: Rogers, Bell, TELUS are service providers, not buyers
- Click **Re-qualify all** briefly (or mention it) — re-scores with latest rules

**Say:** *"FONEX targets government, crown corps, data centres, large enterprise in Canada — plus US Tier 2/3 DC. Telcos are hard-excluded."*

---

### 5. Prompts + Re-qualify (2 min)
**Any agent → Advanced settings → Agent prompts**

**Show:**
- `qualify_extra` — Jay's ICP instructions (editable)
- Edit one line → **Save prompts** → **Re-qualify all** → scores refresh

**Say:** *"These prompts are not cosmetic — they're injected into every qualify and outreach call."*

---

### 6. Gmail + reply scanning (1 min)
**Automation section**

**Show:**
- Gmail connected as [your email]
- **Scan replies now** — checks threads for leads marked **Emailed**
- Explain: after Jay sends from Gmail, he marks lead **Emailed** → system watches for replies → notification when they respond

**Don't demo fake replies** — explain the workflow only unless you have a real emailed thread.

---

## Demo pieces cheat sheet

| Feature | Best lead | Agent |
|---------|-----------|-------|
| Full Gmail import | Sarah Chen @ RBC | Woodway |
| Hot insurance fit | James Okonkwo @ Sun Life | Woodway |
| LinkedIn-only + Hunter | Glenn Minnis @ Intact | Keira |
| Ideal owner profile | Robert Leblanc @ Ottawa Precision Mfg | Keira |
| DC / optical networking | Marc Tremblay @ Cologix | FONEX |
| Blocklist (telco skip) | Telus | FONEX |
| Live contact search | Agent actions → Contact search | Any |
| Live discover | Agent actions → Discover | Keira or FONEX |
| Prompt tuning | Advanced settings | Any |

---

## Readiness checklist

| Item | Status |
|------|--------|
| 111 tests passing | ✓ |
| LLM (Groq) configured | ✓ |
| PDL contacts configured | ✓ |
| Hunter on-demand | ✓ |
| Gmail OAuth connected | ✓ (verify before demo) |
| Hero leads seeded | Run `prepare_demo.py` |
| Sender name = Jay Swayze | `.env` OUTREACH_SENDER_NAME |

## Known limitations (be upfront)

1. **Don't send** to real `@rbc.com` / `@sunlife.com` addresses in demo — Import to Gmail only
2. **Reply scan** only works after lead is marked **Emailed** (not just drafted)
3. **Keira** leads often have LinkedIn only — that's expected, not a bug
4. **FONEX** PDL search is Canada-focused; US DC leads come from web Discover
5. **Contact search / Re-qualify** take 1–3 min live — start early or pre-run

## If something breaks live

- **LLM red:** check GROQ_API_KEY in `.env`, restart server
- **Gmail red:** reconnect via Connect Gmail; ensure Gmail API enabled in Google Cloud
- **Empty leads:** run `python scripts/prepare_demo.py`
- **Stale UI:** `Cmd+Shift+R`
