/* JayAgents dashboard */

const AGENT_COLORS = {
  woodway: "#3ecf8e",
  fonex: "#ff9f43",
  keira: "#8b5cf6",
};

const STATUSES = ["discovered", "imported", "qualified", "drafted", "emailed", "replied", "skipped"];

const STATUS_COLORS = {
  discovered: "#5c6785",
  imported: "#4f8cff",
  qualified: "#ffb02e",
  drafted: "#3ecf8e",
  emailed: "#8b5cf6",
  replied: "#2dd4bf",
  skipped: "#3a4257",
};

const SOURCE_LABELS = {
  discover: "Web discovery",
  pdl_api: "Contact search",
  apollo: "Contact search",
  seamless: "Seamless",
  actava: "Actava",
  csv_import: "CSV import",
  manual: "Manual entry",
};

let seamlessConfigured = false;
let seamlessBudget = null;
let actavaConfigured = false;
let actavaMode = null;

let agents = [];
let currentAgent = null;
let allLeads = [];
let gmailConnected = false;
let hunterAvailable = false;
let modalLeadId = null;
let promptDefaults = null;

const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

/* ---------------- init ---------------- */

async function init() {
  handleGmailOAuthReturn();
  await Promise.all([loadHealth(), loadAgents(), loadNotifications(), loadAutomation()]);
  if (agents.length) selectAgent(agents[0].name);
  setInterval(() => loadHealth(), 120000);
  setInterval(loadNotifications, 60000);
  setInterval(loadAutomation, 300000);
}

function showToast(msg, ok = true) {
  const el = document.createElement("div");
  el.className = "toast " + (ok ? "ok" : "bad");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function renderStatusBanner(h) {
  const banner = $("status-banner");
  if (!banner) return;
  const items = [];

  const llmOk = !!h.llm?.ok;
  items.push({
    ok: llmOk,
    warn: false,
    label: llmOk ? `AI ready (${h.llm.provider})` : `AI unavailable`,
    detail: llmOk ? h.llm.model : h.llm?.detail,
  });

  const contactsOk = !!h.contacts?.configured;
  const seamlessOk = !!h.seamless?.configured;
  if (seamlessOk) {
    const rem = h.seamless.credits_remaining_budget;
    items.push({
      ok: true,
      warn: rem != null && rem < 500,
      label: `Seamless (${rem ?? "?"} credits left)`,
      detail: `Budget ${h.seamless.credits_used ?? 0}/${h.seamless.monthly_budget ?? 10000} used this month`,
    });
  } else if (h.actava?.configured) {
    items.push({
      ok: !!h.actava.cura?.ok,
      warn: !h.actava.cura?.ok,
      label: `Actava (${h.actava.mode || "ready"})`,
      detail: h.actava.agent_id ? `Agent ${h.actava.agent_id.slice(0, 8)}…` : "Web + Cura extraction",
    });
  } else {
    items.push({
      ok: contactsOk,
      warn: false,
      label: contactsOk ? `Contacts (${h.contacts.provider})` : "Contacts not configured",
      detail: contactsOk ? "API key configured" : "Missing API key",
    });
  }

  const g = h.gmail || {};
  if (g.connected) {
    items.push({ ok: true, warn: false, label: `Gmail connected`, detail: g.email });
  } else if (g.authenticated || g.has_token) {
    items.push({ ok: false, warn: true, label: "Gmail setup incomplete", detail: g.detail });
  } else {
    items.push({ ok: false, warn: !g.needs_operator_setup, label: "Gmail not connected", detail: g.detail });
  }

  if (h.config_ok) {
    items.push({ ok: true, warn: false, label: "All systems ready", detail: "Ready for nightly automation" });
  } else if (h.config_issues?.length) {
    items.push({ ok: false, warn: false, label: "Configuration issue", detail: h.config_issues[0] });
  } else if (h.config_warnings?.length) {
    items.push({ ok: false, warn: true, label: "Optional setup", detail: h.config_warnings[0] });
  }

  banner.innerHTML = items.map((item) => {
    const cls = item.ok ? "ok" : item.warn ? "warn" : "bad";
    return `<span class="status-banner-item ${cls}" title="${esc(item.detail || "")}">
      <span class="dot ${item.ok ? "ok" : "bad"}"></span>${esc(item.label)}
    </span>`;
  }).join("");
  banner.classList.remove("hidden");
}

function sourceLabel(source) {
  return SOURCE_LABELS[source] || String(source || "unknown").replace(/_/g, " ");
}

async function loadHealth(forceRefresh = false) {
  try {
    const q = forceRefresh ? "?refresh=1" : "";
    const h = await api(`/api/health${q}`);

    const llmEl = $("health-llm");
    llmEl.querySelector(".dot").className = "dot " + (h.llm?.ok ? "ok" : "bad");
    llmEl.querySelector(".health-label").textContent = h.llm?.provider ? `LLM (${h.llm.provider})` : "LLM";
    llmEl.title = h.llm?.ok ? `${h.llm.provider} — ${h.llm.model}` : h.llm?.detail || "LLM unavailable";

    const cEl = $("health-contacts");
    const c = h.contacts || {};
    const s = h.seamless || {};
    const a = h.actava || {};
    seamlessConfigured = !!s.configured;
    seamlessBudget = s.configured ? s : null;
    actavaConfigured = !!a.configured;
    actavaMode = a.mode || null;
    hunterAvailable = !!c.email_finder;
    const contactsReady = s.configured || a.configured || c.configured;
    cEl.querySelector(".dot").className = "dot " + (contactsReady ? "ok" : "bad");
    if (s.configured) {
      const rem = s.credits_remaining_budget;
      cEl.querySelector(".health-label").textContent = `Seamless (${rem ?? "?"} left)`;
      cEl.title = `Seamless API — ${s.credits_used ?? 0}/${s.monthly_budget ?? 10000} budget used; ${s.per_run_limit ?? 8} max per run`;
    } else if (a.configured) {
      cEl.querySelector(".health-label").textContent = `Actava (${a.mode || "ready"})`;
      cEl.title = a.agent_id
        ? `Actava agent ${a.agent_id} — external agent run`
        : "Actava — web discover + Cura extraction (no Seamless needed)";
    } else {
      cEl.querySelector(".health-label").textContent = c.provider ? `Contacts (${c.provider})` : "Contacts";
      cEl.title = c.configured
        ? `${c.provider} configured${c.email_finder ? " + Hunter email finder" : ""}`
        : `${(c.provider || "contacts").toUpperCase()} API key missing`;
    }
    updateContactSearchButton();

    const gEl = $("health-gmail");
    const g = h.gmail || {};
    gmailConnected = !!g.connected;
    gEl.querySelector(".dot").className = "dot " + (g.connected ? "ok" : "bad");
    gEl.querySelector(".health-label").textContent = g.connected && g.email ? `Gmail (${g.email})` : "Gmail";
    gEl.title = g.connected ? `Connected as ${g.email}` : g.detail || "Gmail not connected";
    await updateGmailUI(g);
    renderStatusBanner(h);

    if (h.config_issues?.length) {
      llmEl.title = (llmEl.title || "") + "\nConfig: " + h.config_issues.join("; ");
    }
    if (h.config_warnings?.length && !h.config_issues?.length) {
      cEl.title = (cEl.title || "") + "\n" + h.config_warnings.join("; ");
    }
  } catch (e) {
    console.error("Health check failed:", e);
  }
}

const GMAIL_CONNECT_BTNS = [
  "btn-gmail-connect",
  "btn-gmail-header",
  "btn-gmail-sidebar",
  "btn-gmail-modal",
];

const GMAIL_DISCONNECT_BTNS = [
  "btn-gmail-disconnect-header",
  "btn-gmail-disconnect-sidebar",
  "btn-gmail-disconnect-pending",
  "btn-gmail-disconnect-card",
];

function setGmailDisconnectButtonsVisible(visible) {
  for (const id of GMAIL_DISCONNECT_BTNS) {
    const btn = $(id);
    if (!btn) continue;
    btn.classList.toggle("hidden", !visible);
    btn.disabled = false;
  }
}

function setGmailConnectButtons(enabled) {
  for (const id of GMAIL_CONNECT_BTNS) {
    const btn = $(id);
    if (!btn) continue;
    if (enabled) {
      btn.classList.remove("disabled");
      btn.removeAttribute("aria-disabled");
      btn.href = "/api/gmail/oauth/start";
      btn.onclick = null;
    } else {
      btn.classList.add("disabled");
      btn.setAttribute("aria-disabled", "true");
      btn.href = "#";
      btn.onclick = (e) => e.preventDefault();
    }
  }
}

function toggleGmailConnectButtons(visible) {
  for (const id of GMAIL_CONNECT_BTNS) {
    const btn = $(id);
    if (!btn) continue;
    btn.classList.toggle("hidden", !visible);
  }
}

function setGmailConnectedEmail(email) {
  const addr = email || "—";
  for (const id of ["gmail-card-email", "gmail-header-email", "gmail-sidebar-email"]) {
    const el = $(id);
    if (el) el.textContent = addr;
  }
}

function hideAllGmailPanels() {
  for (const id of ["gmail-connect-panel", "gmail-connected-panel", "gmail-pending-panel"]) {
    const el = $(id);
    if (el) el.classList.add("hidden");
  }
}

function setGmailCardVisible(visible) {
  const card = $("gmail-card");
  if (card) card.classList.toggle("hidden", !visible);
}

function setGmailHeaderSidebarStatus(g, mode) {
  const sidebar = $("gmail-sidebar-status");
  const header = $("gmail-header-status");
  if (!sidebar || !header) return;

  const sidebarLabel = sidebar.querySelector(".gmail-status-label");
  const headerBadge = header.querySelector(".gmail-status-badge");

  sidebar.classList.toggle("pending", mode === "pending");
  header.classList.toggle("pending", mode === "pending");
  $("btn-gmail-sidebar").classList.add("hidden");
  $("btn-gmail-header").classList.add("hidden");
  sidebar.classList.remove("hidden");
  header.classList.remove("hidden");

  if (mode === "connected") {
    sidebarLabel.textContent = "Connected";
    if (headerBadge) headerBadge.textContent = "Connected";
    setGmailConnectedEmail(g.email);
  } else if (mode === "pending") {
    sidebarLabel.textContent = g.needs_api_enable ? "Setup required" : "Signed in";
    if (headerBadge) headerBadge.textContent = g.needs_api_enable ? "Setup required" : "Signed in";
    const addr = g.email || "Google account linked";
    $("gmail-sidebar-email").textContent = addr;
    $("gmail-header-email").textContent = addr;
  }
}

function showGmailConnectedUI(g) {
  hideAllGmailPanels();
  setGmailCardVisible(false);
  toggleGmailConnectButtons(false);
  setGmailDisconnectButtonsVisible(true);
  setGmailHeaderSidebarStatus(g, "connected");
}

function showGmailPendingUI(g) {
  const pending = $("gmail-pending-panel");
  hideAllGmailPanels();
  if (pending) pending.classList.remove("hidden");
  const card = $("gmail-card");
  if (card) {
    card.classList.remove("connected");
    card.classList.add("pending");
  }
  setGmailCardVisible(true);
  toggleGmailConnectButtons(false);
  setGmailDisconnectButtonsVisible(true);
  setGmailHeaderSidebarStatus(g, "pending");
  const badge = $("gmail-pending-badge");
  const email = $("gmail-pending-email");
  const detail = $("gmail-pending-detail");
  if (badge) badge.textContent = g.needs_api_enable ? "Setup required" : "Signed in";
  if (email) email.textContent = g.email || "Google account linked";
  if (detail) detail.textContent = g.detail || "Finishing Gmail setup…";
}

function showGmailDisconnectedUI() {
  const connect = $("gmail-connect-panel");
  hideAllGmailPanels();
  if (connect) connect.classList.remove("hidden");
  const card = $("gmail-card");
  if (card) {
    card.classList.remove("connected");
    card.classList.remove("pending");
  }
  setGmailCardVisible(true);
  toggleGmailConnectButtons(true);
  setGmailDisconnectButtonsVisible(false);
  $("btn-gmail-sidebar").classList.remove("hidden");
  $("gmail-sidebar-status").classList.add("hidden");
  $("btn-gmail-header").classList.remove("hidden");
  $("gmail-header-status").classList.add("hidden");
}

async function updateGmailUI(g) {
  const card = $("gmail-card");
  const meta = $("gmail-connect-meta");
  const gEl = $("health-gmail");
  const authenticated = !!(g.authenticated || g.has_token);

  if (g.connected) {
    showGmailConnectedUI(g);
    if (gEl) {
      gEl.classList.remove("clickable");
      gEl.onclick = null;
      gEl.querySelector(".dot").className = "dot ok";
      gEl.querySelector(".health-label").textContent = g.email ? `Gmail (${g.email})` : "Gmail";
      gEl.title = `Connected as ${g.email || "unknown"}`;
    }
    return;
  }

  if (authenticated) {
    showGmailPendingUI(g);
    gEl.classList.remove("clickable");
    gEl.onclick = null;
    gEl.querySelector(".dot").className = "dot bad";
    gEl.querySelector(".health-label").textContent = g.email ? `Gmail (${g.email})` : "Gmail (signed in)";
    gEl.title = g.detail || "Gmail signed in but not fully connected";
    return;
  }

  showGmailDisconnectedUI();
  gEl.querySelector(".dot").className = "dot bad";
  gEl.classList.add("clickable");
  gEl.onclick = () => {
    const headerBtn = $("btn-gmail-header");
    if (headerBtn && !headerBtn.classList.contains("disabled")) {
      window.location.href = "/api/gmail/oauth/start";
    } else {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  if (g.needs_operator_setup || g.needs_credentials) {
    $("gmail-connect-desc").textContent =
      "Gmail isn't configured on this server yet. Your administrator needs to set it up once.";
    meta.innerHTML =
      "End users only need to click <b>Connect Gmail</b> once the server is configured. " +
      "Operators: set <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> in server env.";
    setGmailConnectButtons(false);
    return;
  }

  $("gmail-connect-desc").textContent =
    "Sign in with your Google account to create drafts, send outreach, and get reply notifications.";
  try {
    const setup = await api("/api/gmail/setup");
    meta.innerHTML =
      `Add this redirect URI in Google Cloud Console if prompted:<br>` +
      `<code>${esc(setup.redirect_uri)}</code>`;
  } catch {
    meta.textContent = "";
  }
  setGmailConnectButtons(true);
}

function showGmailToast(msg, ok = true) {
  showToast(msg, ok);
}

function handleGmailOAuthReturn() {
  const params = new URLSearchParams(location.search);
  const result = params.get("gmail");
  if (!result) return;
  const msgs = {
    connected: "Google sign-in complete — checking Gmail status…",
    denied: "Gmail connection was cancelled.",
    error: "Gmail connection failed — try again.",
  };
  showGmailToast(msgs[result] || "Gmail connection finished.", result === "connected");
  history.replaceState({}, "", location.pathname);
  loadHealth(true);
}

$("btn-gmail-refresh")?.addEventListener("click", () => loadHealth(true));

async function disconnectGmail() {
  const ok = confirm(
    "Disconnect Gmail from JayAgents?\n\nYou'll need to sign in again to create drafts, send outreach, or scan for replies."
  );
  if (!ok) return;

  for (const id of GMAIL_DISCONNECT_BTNS) {
    const btn = $(id);
    if (btn) btn.disabled = true;
  }

  try {
    await api("/api/gmail/disconnect", { method: "POST" });
    gmailConnected = false;
    showGmailToast("Gmail disconnected — you can connect a different account anytime.");
    await loadHealth(true);
  } catch (e) {
    showGmailToast(e.message, false);
  } finally {
    for (const id of GMAIL_DISCONNECT_BTNS) {
      const btn = $(id);
      if (btn) btn.disabled = false;
    }
  }
}

for (const id of GMAIL_DISCONNECT_BTNS) {
  $(id)?.addEventListener("click", disconnectGmail);
}

async function loadAgents() {
  agents = await api("/api/agents");
  renderNav();
}

/* ---------------- notifications ---------------- */

async function loadNotifications() {
  try {
    const data = await api("/api/notifications");
    const count = data.unread || 0;
    const badge = $("bell-count");
    badge.textContent = count;
    badge.classList.toggle("hidden", count === 0);

    const list = $("notif-list");
    if (!data.notifications.length) {
      list.innerHTML = `<div class="empty">No notifications yet.</div>`;
      return;
    }
    list.innerHTML = data.notifications
      .map((n) => `
        <div class="notif-item ${n.read ? "" : "unread"}" ${n.lead_id ? `data-lead="${n.lead_id}"` : ""}>
          <span class="notif-dot"></span>
          <div>
            <div class="notif-msg">${esc(n.message)}</div>
            <div class="notif-time">${fmtDateTime(n.created_at)}${n.agent ? ` · ${esc(n.agent)}` : ""}</div>
          </div>
        </div>`)
      .join("");
    list.querySelectorAll("[data-lead]").forEach((el) => {
      el.style.cursor = "pointer";
      el.onclick = () => openModal(+el.dataset.lead);
    });
  } catch (e) {
    console.error("Failed to load notifications:", e);
  }
}

$("bell-btn").onclick = () => $("notif-panel").classList.toggle("hidden");
$("notif-mark-read").onclick = async () => {
  await api("/api/notifications/mark-read", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  loadNotifications();
};

/* ---------------- agent actions ---------------- */

let activeJobPoll = null;

const RUN_LABELS = {
  requalify_all: "Re-qualify all",
  discover: "Discover prospects",
  process_imported: "Process imported",
  contact_search: "Contact search",
};

function updateContactSearchButton() {
  const btn = $("btn-contacts");
  if (!btn) return;
  if (currentAgent === "keira" && seamlessConfigured) {
    const rem = seamlessBudget?.credits_remaining_budget;
    btn.textContent = rem != null ? `Seamless search (${rem} left)` : "Seamless search";
    btn.title = `Search free, research up to ${seamlessBudget?.per_run_limit ?? 8} owners per run`;
  } else if (currentAgent === "keira" && actavaConfigured) {
    btn.textContent = actavaMode === "agent" ? "Actava agent search" : "Actava prospect search";
    btn.title = actavaMode === "agent"
      ? "Run your Actava external agent for owner prospects"
      : "Web search + Actava Cura extraction (no Seamless)";
  } else {
    btn.textContent = "Contact search";
    btn.title = "";
  }
}

function setActionButtonsDisabled(disabled) {
  for (const id of ["btn-requalify", "btn-discover", "btn-process", "btn-contacts"]) {
    const el = $(id);
    if (el) el.disabled = disabled;
  }
}

function renderJobPanel(job, label) {
  const panel = $("job-panel");
  const title = $("job-title");
  const status = $("job-status");
  const logEl = $("job-log");
  if (!panel || !title || !status || !logEl) return;

  panel.classList.remove("hidden");
  title.textContent = label || "Agent task";
  status.textContent = job.status === "running" ? "Running…" : job.status === "done" ? "Complete" : "Failed";
  status.className = "job-status " + (job.status || "running");
  logEl.textContent = (job.log || []).map((e) => e.msg).join("\n") || "Starting…";
  logEl.scrollTop = logEl.scrollHeight;
}

async function pollJob(jobId, label) {
  if (activeJobPoll) clearInterval(activeJobPoll);
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const job = await api(`/api/jobs/${jobId}`);
        renderJobPanel(job, label);
        if (job.status !== "running") {
          clearInterval(activeJobPoll);
          activeJobPoll = null;
          setActionButtonsDisabled(false);
          if (job.status === "done") {
            showToast(job.log?.at(-1)?.msg || "Task complete", true);
            await Promise.all([loadLeads(), refreshStats(), loadAgents().then(() => renderNav())]);
          } else {
            showToast(job.error || "Task failed", false);
          }
          resolve(job);
        }
      } catch (e) {
        clearInterval(activeJobPoll);
        activeJobPoll = null;
        setActionButtonsDisabled(false);
        showToast(e.message, false);
        resolve(null);
      }
    };
    tick();
    activeJobPoll = setInterval(tick, 3000);
  });
}

async function runAgentAction(mode, { limit } = {}) {
  if (!currentAgent) return;
  const label = RUN_LABELS[mode] || mode;
  const limits = {
    requalify_all: limit ?? 500,
    discover: limit ?? 5,
    process_imported: limit ?? 25,
    contact_search: limit ?? 25,
  };
  setActionButtonsDisabled(true);
  renderJobPanel({ status: "running", log: [{ msg: `Starting ${label.toLowerCase()}…` }] }, label);
  try {
    const started = await api(`/api/agents/${currentAgent}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, limit: limits[mode] ?? 25, mock: false }),
    });
    await pollJob(started.job_id, label);
  } catch (e) {
    setActionButtonsDisabled(false);
    $("job-panel")?.classList.add("hidden");
    showToast(e.message, false);
  }
}

$("btn-requalify")?.addEventListener("click", () => runAgentAction("requalify_all"));
$("btn-discover")?.addEventListener("click", () => runAgentAction("discover"));
$("btn-process")?.addEventListener("click", () => runAgentAction("process_imported"));
$("btn-contacts")?.addEventListener("click", () => runAgentAction("contact_search"));

/* ---------------- automation ---------------- */

async function loadAutomation() {
  try {
    const a = await api("/api/automation");
    const meta = [];
    if (a.last_daily_run) {
      const ok = a.last_daily_run.ok ? "✓" : "✗ failed";
      meta.push(`<span class="chip"><b>Last nightly run:</b> ${fmtDateTime(a.last_daily_run.finished_at)} ${ok}</span>`);
    } else {
      meta.push(`<span class="chip"><b>Last nightly run:</b> not yet scheduled</span>`);
    }
    if (a.last_reply_scan) {
      meta.push(`<span class="chip"><b>Last reply scan:</b> ${fmtDateTime(a.last_reply_scan.finished_at)} (${esc(a.last_reply_scan.summary || "")})</span>`);
    }
    $("automation-meta").innerHTML = meta.join(" ");
  } catch (e) {
    console.error("Failed to load automation:", e);
  }
}

$("btn-scan-replies").onclick = async () => {
  const btn = $("btn-scan-replies");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  try {
    const r = await api("/api/gmail/scan-replies", { method: "POST" });
    btn.textContent = r.replies ? `${r.replies} new replies!` : "No new replies";
    await Promise.all([loadNotifications(), loadLeads(), refreshStats(), loadAutomation()]);
  } catch (e) {
    btn.textContent = "Scan replies now";
    showToast(e.message, false);
  } finally {
    btn.disabled = false;
    setTimeout(() => (btn.textContent = "Scan replies now"), 4000);
  }
};

/* ---------------- agents nav ---------------- */

function renderNav() {
  const nav = $("agent-nav");
  nav.innerHTML = "";
  for (const a of agents) {
    const el = document.createElement("button");
    el.type = "button";
    el.setAttribute("aria-label", `Agent ${a.company}`);
    el.className = "agent-item" + (currentAgent === a.name ? " active" : "");
    el.style.setProperty("--agent-color", AGENT_COLORS[a.name]);
    el.innerHTML = `
      <div class="agent-avatar">${a.name.slice(0, 2).toUpperCase()}</div>
      <div style="min-width:0">
        <div class="agent-item-name">${esc(a.company)}</div>
        <div class="agent-item-product">${esc(a.product)}</div>
      </div>
      <div class="agent-item-leads">${a.stats.total}</div>`;
    el.onclick = () => selectAgent(a.name);
    nav.appendChild(el);
  }
}

function agentData(name) {
  return agents.find((a) => a.name === name);
}

async function selectAgent(name) {
  currentAgent = name;
  document.documentElement.style.setProperty("--agent-color", AGENT_COLORS[name]);
  renderNav();
  renderHeader();
  updateContactSearchButton();
  $("export-btn").href = `/api/agents/${name}/export.csv`;
  await Promise.all([refreshStats(), loadLeads(), loadPrompts()]);
}

function renderHeader() {
  const a = agentData(currentAgent);
  $("agent-title").textContent = a.company;
  $("agent-tagline").textContent = `${a.product} — ${a.tagline}`;

  const strip = $("icp-strip");
  const chips = [];
  if (a.industries.length)
    chips.push(`<span class="chip"><b>Industries:</b> ${esc(a.industries.slice(0, 5).join(", "))}</span>`);
  if (a.titles.length)
    chips.push(`<span class="chip"><b>Titles:</b> ${esc(a.titles.slice(0, 4).join(", "))}</span>`);
  if (a.geography.length)
    chips.push(`<span class="chip"><b>Geo:</b> ${esc(String(a.geography.slice(0, 2).join(", ")).slice(0, 60))}</span>`);
  strip.innerHTML = chips.join("");
}

async function refreshStats() {
  agents = await api("/api/agents");
  renderNav();
  const s = agentData(currentAgent).stats;
  const drafted = s.by_status.drafted || 0;
  $("stats-row").innerHTML = `
    <div class="stat"><div class="stat-value">${s.total}</div><div class="stat-label">Total leads</div></div>
    <div class="stat"><div class="stat-value">${s.with_email}</div><div class="stat-label">With email</div></div>
    <div class="stat"><div class="stat-value">${s.linkedin_only || 0}</div><div class="stat-label">LinkedIn only</div></div>
    <div class="stat"><div class="stat-value">${drafted}</div><div class="stat-label">Outreach drafted</div></div>
    <div class="stat"><div class="stat-value">${s.by_status.qualified || 0}</div><div class="stat-label">Qualified</div></div>`;
  renderFunnel(s);
}

function renderFunnel(s) {
  const bar = $("funnel-bar");
  const legend = $("funnel-legend");
  const total = s.total || 0;
  bar.innerHTML = "";
  legend.innerHTML = "";
  if (!total) {
    bar.innerHTML = `<div class="funnel-empty">No leads yet</div>`;
    return;
  }
  for (const st of STATUSES) {
    const n = s.by_status[st] || 0;
    if (!n) continue;
    const seg = document.createElement("div");
    seg.className = "funnel-seg";
    seg.style.width = `${(n / total) * 100}%`;
    seg.style.background = STATUS_COLORS[st];
    seg.title = `${st}: ${n}`;
    bar.appendChild(seg);
    legend.innerHTML += `<span class="legend-item"><span class="legend-dot" style="background:${STATUS_COLORS[st]}"></span>${st} <b>${n}</b></span>`;
  }
}

/* ---------------- leads ---------------- */

async function loadLeads() {
  const status = $("status-filter").value;
  const url = `/api/agents/${currentAgent}/leads` + (status ? `?status=${status}` : "");
  allLeads = await api(url);
  renderLeads();
}

function renderContactChannelCell(l) {
  if (l.email) return esc(l.email);
  const ch = l.contact_channel || "incomplete";
  if (ch === "linkedin") {
    return `<span class="contact-badge linkedin" title="${esc(l.contact_message || "")}">Email not found · LinkedIn</span>`;
  }
  if (ch === "company_only") {
    return `<span class="contact-badge company">No contact yet</span>`;
  }
  if (ch === "incomplete") {
    return `<span class="contact-badge incomplete">No outreach channel</span>`;
  }
  return '<span class="muted">—</span>';
}

function renderContactChannelBanner(l) {
  const el = $("m-contact-channel");
  if (!el) return;
  const ch = l.contact_channel;
  const msg = l.contact_message;
  if (!ch || ch === "email") {
    el.classList.add("hidden");
    el.innerHTML = "";
    renderHunterResearchButton(l);
    return;
  }
  el.className = `contact-channel-banner ${ch}`;
  const labels = {
    linkedin: "Email not found",
    incomplete: "Contact info incomplete",
    company_only: "No named contact",
  };
  el.innerHTML = `<strong>${esc(labels[ch] || l.contact_label || "Contact status")}</strong>${esc(msg || "")}`;
  el.classList.remove("hidden");
  renderHunterResearchButton(l);
}

function renderHunterResearchButton(l) {
  const wrap = $("m-contact-actions");
  const btn = $("m-hunter-research");
  if (!wrap || !btn) return;

  const show = !!(l.can_hunter_research || (hunterAvailable && !l.email && l.contact_name));
  wrap.classList.toggle("hidden", !show);
  if (!show) return;

  btn.disabled = false;
  btn.textContent = "Use Hunter for research (not guaranteed)";
  btn.onclick = async () => {
    btn.disabled = true;
    btn.textContent = "Searching with Hunter…";
    try {
      const result = await api(`/api/leads/${l.id}/hunter-research`, { method: "POST" });
      showToast(result.message || (result.found ? "Email found" : "No email found"), !!result.found);
      await openModal(l.id);
      loadLeads().then(refreshStats);
    } catch (e) {
      showToast(e.message, false);
      btn.disabled = false;
      btn.textContent = "Use Hunter for research (not guaranteed)";
    }
  };
}

function renderLeads() {
  const q = $("search-filter").value.trim().toLowerCase();
  const leads = q
    ? allLeads.filter((l) =>
        [l.company, l.contact_name, l.contact_title, l.email, l.industry]
          .filter(Boolean).some((v) => String(v).toLowerCase().includes(q)))
    : allLeads;

  const body = $("leads-body");
  body.innerHTML = "";
  $("leads-count").textContent = leads.length;

  const hasAnyLeads = allLeads.length > 0;
  const filtered = q || $("status-filter").value;
  $("leads-empty").classList.toggle("hidden", hasAnyLeads);
  $("leads-filter-empty").classList.toggle("hidden", !hasAnyLeads || leads.length > 0);

  for (const l of leads) {
    const tr = document.createElement("tr");
    const score = l.score ?? "—";
    const tier = l.tier || "";
    const color = tier === "hot" ? "var(--hot)" : tier === "warm" ? "var(--warm)" : "var(--cold)";
    tr.innerHTML = `
      <td class="company">${esc(l.company)}</td>
      <td class="${l.contact_name ? "" : "muted"}">${esc(l.contact_name || l.contact_title || "—")}</td>
      <td>${renderContactChannelCell(l)}</td>
      <td><div class="score-cell">
        <span class="score-num">${score}</span>
        <div class="score-bar"><div class="score-fill" style="width:${l.score || 0}%;background:${color}"></div></div>
      </div></td>
      <td>${tier ? `<span class="badge ${tier}">${tier.toUpperCase()}</span>` : '<span class="muted">—</span>'}</td>
      <td><span class="badge status ${esc(l.status)}">${esc(l.status)}</span></td>
      <td>${l.linkedin_url ? `<a class="li-link" href="${esc(normalizeUrl(l.linkedin_url))}" target="_blank" rel="noopener" title="Open LinkedIn profile">in</a>` : ""}</td>
      <td class="muted date">${fmtDate(l.updated_at)}</td>`;
    tr.onclick = () => openModal(l.id);
    tr.querySelector(".li-link")?.addEventListener("click", (e) => e.stopPropagation());
    body.appendChild(tr);
  }
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/* ---------------- modal ---------------- */

async function openModal(id) {
  const l = await api(`/api/leads/${id}`);
  modalLeadId = id;
  $("m-company").textContent = l.company;
  $("m-prospect").textContent = l.prospect || [l.contact_name, l.contact_title].filter(Boolean).join(", ") || "—";

  const meta = [];
  if (l.score != null) meta.push(`<span class="chip accent"><b>${l.score}/100</b> ${(l.tier || "").toUpperCase()}</span>`);
  if (l.email) meta.push(`<span class="chip">${esc(l.email)}</span>`);
  else if (l.contact_channel === "linkedin")
    meta.push(`<span class="chip">Email not found · LinkedIn</span>`);
  if (l.industry) meta.push(`<span class="chip">${esc(l.industry)}</span>`);
  if (l.employee_count) meta.push(`<span class="chip">${Number(l.employee_count).toLocaleString()} employees</span>`);
  if (l.linkedin_url) meta.push(`<span class="chip"><a href="${esc(normalizeUrl(l.linkedin_url))}" target="_blank" rel="noopener" style="color:var(--accent)">LinkedIn ↗</a></span>`);
  meta.push(`<span class="chip">${esc(sourceLabel(l.source))}</span>`);
  $("m-meta").innerHTML = meta.join("");
  renderContactChannelBanner(l);

  $("m-signal-wrap").classList.toggle("hidden", !l.signal);
  $("m-signal").textContent = l.signal || "";

  let qual = null;
  try { qual = l.qualification_json ? JSON.parse(l.qualification_json) : null; } catch {}
  const reasons = qual?.reasons || [];
  const talking = qual?.talking_points || [];
  $("m-qual-wrap").classList.toggle("hidden", !reasons.length && !qual?.recommendation);
  $("m-reasons").innerHTML = reasons.map((r) => `<li>${esc(r)}</li>`).join("");
  const rec = $("m-recommendation");
  rec.classList.toggle("hidden", !qual?.recommendation);
  rec.textContent = qual?.recommendation || "";
  $("m-talking-wrap").classList.toggle("hidden", !talking.length);
  $("m-talking").innerHTML = talking.map((t) => `<li>${esc(t)}</li>`).join("");

  const hasOutreach = !!(l.outreach_body || l.outreach_subject);
  $("m-outreach-wrap").classList.toggle("hidden", !hasOutreach);
  $("m-outreach").textContent = hasOutreach
    ? (l.outreach_subject && !String(l.outreach_body || "").startsWith("Subject")
        ? `Subject: ${l.outreach_subject}\n\n` : "") + (l.outreach_body || "")
    : "";

  renderOutreachGmailActions(l);

  const btns = $("m-status-buttons");
  btns.innerHTML = "";
  for (const s of STATUSES) {
    const b = document.createElement("button");
    b.className = "btn small" + (l.status === s ? " active" : "");
    b.textContent = s;
    b.onclick = async () => {
      await api(`/api/leads/${l.id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: s }),
      });
      openModal(l.id);
      loadLeads().then(refreshStats);
    };
    btns.appendChild(b);
  }

  $("modal").classList.remove("hidden");
}

function gmailDraftUrl(draftId) {
  return `https://mail.google.com/mail/u/0/#drafts?compose=${encodeURIComponent(draftId)}`;
}

async function importLeadToGmail(lead) {
  const result = await api(`/api/leads/${lead.id}/gmail-draft`, { method: "POST" });
  showToast(`Draft saved to Gmail as ${result.sender || "you"} — review and send from Gmail`, true);
  if (result.gmail_url) {
    window.open(result.gmail_url, "_blank", "noopener");
  }
  await openModal(lead.id);
  loadLeads();
  return result;
}

function renderOutreachGmailActions(l) {
  const bar = $("m-gmail-bar");
  const importBtn = $("m-gmail-import");
  const openBtn = $("m-gmail-open");
  const linkedinBtn = $("m-linkedin-open");
  const hint = $("m-gmail-hint");
  const hasOutreach = !!(l.outreach_body || l.outreach_subject);
  const channel = l.contact_channel || (l.email ? "email" : l.linkedin_url ? "linkedin" : "incomplete");

  if (!hasOutreach) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");

  if (channel === "linkedin" && l.linkedin_url) {
    importBtn.classList.add("hidden");
    openBtn.classList.add("hidden");
    linkedinBtn.classList.remove("hidden");
    linkedinBtn.href = normalizeUrl(l.linkedin_url);
    const hunterHint = l.can_hunter_research
      ? " Email not found — try Hunter above or use LinkedIn for outreach."
      : "";
    hint.textContent = (l.contact_message || "Email not found — LinkedIn is the best channel for outreach.") + hunterHint;
    return;
  }

  linkedinBtn.classList.add("hidden");

  if (!l.email || !String(l.email).includes("@")) {
    importBtn.classList.remove("hidden");
    openBtn.classList.add("hidden");
    importBtn.disabled = true;
    importBtn.textContent = "Import to Gmail";
    hint.textContent = l.contact_message || "This lead needs a contact email before importing to Gmail.";
    importBtn.onclick = null;
    return;
  }

  if (!gmailConnected) {
    importBtn.classList.remove("hidden");
    openBtn.classList.add("hidden");
    importBtn.disabled = true;
    importBtn.textContent = "Import to Gmail";
    hint.textContent = "Connect Gmail first — creates a draft only, nothing is sent.";
    importBtn.onclick = null;
    return;
  }

  if (l.gmail_draft_id) {
    importBtn.classList.add("hidden");
    openBtn.classList.remove("hidden");
    openBtn.href = gmailDraftUrl(l.gmail_draft_id);
    hint.textContent = "Draft is in Gmail — open it there to review and send.";
    return;
  }

  importBtn.classList.remove("hidden");
  openBtn.classList.add("hidden");
  importBtn.disabled = false;
  importBtn.textContent = "Import to Gmail";
  hint.textContent = "Creates a draft in your Gmail. You send it from Gmail when ready.";

  importBtn.onclick = async () => {
    importBtn.disabled = true;
    importBtn.textContent = "Importing…";
    try {
      await importLeadToGmail(l);
    } catch (e) {
      showToast(e.message, false);
      importBtn.disabled = false;
      importBtn.textContent = "Import to Gmail";
    }
  };
}

function normalizeUrl(u) {
  return /^https?:\/\//i.test(u) ? u : `https://${u}`;
}

$("modal-close").onclick = () => $("modal").classList.add("hidden");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("modal").classList.add("hidden");
});
$("modal").onclick = (e) => { if (e.target === $("modal")) $("modal").classList.add("hidden"); };
$("m-copy").onclick = () => {
  navigator.clipboard.writeText($("m-outreach").textContent);
  $("m-copy").textContent = "Copied!";
  setTimeout(() => ($("m-copy").textContent = "Copy"), 1500);
};

$("status-filter").onchange = loadLeads;
$("search-filter").addEventListener("input", renderLeads);

/* ---------------- prompts ---------------- */

async function loadPrompts() {
  if (!currentAgent) return;
  try {
    const p = await api(`/api/agents/${currentAgent}/prompts`);
    promptDefaults = p.defaults;
    $("prompts-path").textContent = p.path;
    $("prompt-qualify-system").value = p.values.qualify_system || "";
    $("prompt-qualify-extra").value = p.values.qualify_extra || "";
    $("prompt-outreach-system").value = p.values.outreach_system || "";
    $("prompt-outreach-extra").value = p.values.outreach_extra || "";
    $("prompt-qualify-template").textContent = p.templates.qualify_user || "";
    $("prompt-outreach-template").textContent = p.templates.outreach_user || "";
    updatePromptsSummary(p);
    hidePromptStatus();
  } catch (e) {
    showPromptStatus(e.message, true);
  }
}

function promptPayload() {
  return {
    qualify_system: $("prompt-qualify-system").value,
    qualify_extra: $("prompt-qualify-extra").value,
    outreach_system: $("prompt-outreach-system").value,
    outreach_extra: $("prompt-outreach-extra").value,
  };
}

function showPromptStatus(msg, isError = false) {
  const el = $("prompts-status");
  el.textContent = msg;
  el.classList.remove("hidden", "error");
  if (isError) el.classList.add("error");
}

function hidePromptStatus() {
  $("prompts-status").classList.add("hidden");
}

function updatePromptsSummary(p) {
  const el = $("prompts-summary");
  if (!el) return;
  const custom = Object.values(p.using_defaults || {}).some((v) => !v);
  el.textContent = custom
    ? "Custom AI instructions saved for this agent."
    : "Using default AI instructions for qualification and outreach.";
}

$("prompts-toggle")?.addEventListener("click", () => {
  const body = $("prompts-body");
  const btn = $("prompts-toggle");
  const expanded = body.classList.toggle("hidden") === false;
  btn.setAttribute("aria-expanded", String(expanded));
  btn.textContent = expanded ? "Hide advanced settings" : "Advanced settings";
});

document.querySelectorAll(".prompts-tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".prompts-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".prompts-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`prompts-panel-${tab.dataset.tab}`).classList.add("active");
  };
});

$("prompts-save").onclick = async () => {
  const btn = $("prompts-save");
  btn.disabled = true;
  try {
    await api(`/api/agents/${currentAgent}/prompts`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(promptPayload()),
    });
    await loadPrompts();
    showPromptStatus("Prompts saved — new runs will use these settings.");
  } catch (e) {
    showPromptStatus(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

$("prompts-reset").onclick = async () => {
  if (!confirm("Reset prompts to defaults for this agent?")) return;
  const btn = $("prompts-reset");
  btn.disabled = true;
  try {
    await api(`/api/agents/${currentAgent}/prompts`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        qualify_system: "",
        qualify_extra: "",
        outreach_system: "",
        outreach_extra: "",
      }),
    });
    await loadPrompts();
    showPromptStatus("Prompts reset to defaults.");
  } catch (e) {
    showPromptStatus(e.message, true);
  } finally {
    btn.disabled = false;
  }
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

init();
