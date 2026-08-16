/* JayAgents dashboard */

const AGENT_COLORS = {
  woodway: "#3ecf8e",
  fonex: "#ff9f43",
  keira: "#8b5cf6",
};

const STATUSES = ["discovered", "imported", "qualified", "drafted", "emailed", "replied", "awaiting_contact", "skipped"];

const STATUS_COLORS = {
  discovered: "#5c6785",
  imported: "#4f8cff",
  qualified: "#ffb02e",
  drafted: "#3ecf8e",
  emailed: "#8b5cf6",
  replied: "#2dd4bf",
  skipped: "#3a4257",
  awaiting_contact: "#6b7289",
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
let microsoftConnected = false;
let hunterAvailable = false;
let modalLeadId = null;
let promptDefaults = null;
let reviewMode = false;
let reviewQueue = [];
let reviewIndex = 0;

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "include", ...opts });
  if (r.status === 401) {
    window.location.href = "/login.html";
    throw new Error("Sign in required");
  }
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
  handleMicrosoftOAuthReturn();
  handleSeamlessOAuthReturn();
  await loadCurrentUser();
  await Promise.all([loadHealth(), loadAgents(), loadNotifications(), loadAutomation()]);
  if (agents.length) selectAgent(agents[0].name);
  setInterval(() => loadHealth(), 120000);
  setInterval(loadNotifications, 60000);
  setInterval(loadAutomation, 300000);
}

async function loadCurrentUser() {
  const menu = $("user-menu");
  const logoutBtn = $("btn-logout");
  if (!menu || !logoutBtn) return;
  try {
    const user = await api("/api/auth/me");
    $("user-email").textContent = user.email;
    menu.classList.remove("hidden");
    logoutBtn.onclick = async () => {
      try {
        await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
      } catch {}
      window.location.href = "/login.html";
    };
  } catch {
    menu.classList.add("hidden");
  }
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
  // Only surface warnings/errors — healthy status lives in the sidebar.
  const items = [];

  if (!h.llm?.ok) {
    items.push({ ok: false, warn: false, label: "AI unavailable", detail: h.llm?.detail });
  }

  const contactsOk = !!h.contacts?.configured;
  const seamlessOk = !!h.seamless?.configured;
  if (seamlessOk) {
    const rem = h.seamless.credits_remaining_budget;
    if (rem != null && rem < 500) {
      items.push({
        ok: true,
        warn: true,
        label: `Seamless low (${rem} left)`,
        detail: `Budget ${h.seamless.credits_used ?? 0}/${h.seamless.monthly_budget ?? 10000} used this month`,
      });
    }
  } else if (h.seamless?.can_connect) {
    items.push({
      ok: false,
      warn: true,
      label: "Connect Seamless",
      detail: h.seamless.detail || "Sign in to use your Seamless plan credits",
    });
  } else if (!h.actava?.configured && !contactsOk) {
    items.push({
      ok: false,
      warn: true,
      label: "Contacts not configured",
      detail: "Set Apollo/PDL or connect Seamless",
    });
  }

  const g = h.gmail || {};
  const m = h.microsoft || {};
  if (!(g.connected || m.connected)) {
    if (g.authenticated || g.has_token) {
      items.push({ ok: false, warn: true, label: "Gmail setup incomplete", detail: g.detail });
    } else if (m.authenticated || m.has_token) {
      items.push({ ok: false, warn: true, label: "Outlook setup incomplete", detail: m.detail });
    } else if (!g.needs_operator_setup || !m.needs_operator_setup) {
      items.push({
        ok: false,
        warn: true,
        label: "Connect email",
        detail: "Gmail or Outlook required for drafts",
      });
    }
  }

  if (h.config_issues?.length) {
    items.push({ ok: false, warn: false, label: "Configuration issue", detail: h.config_issues[0] });
  }

  if (!items.length) {
    banner.innerHTML = "";
    banner.classList.add("hidden");
    return;
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

    const mEl = $("health-ms");
    const m = h.microsoft || {};
    microsoftConnected = !!m.connected;
    if (mEl) {
      mEl.querySelector(".dot").className = "dot " + (m.connected ? "ok" : "bad");
      mEl.querySelector(".health-label").textContent = m.connected && m.email ? `Outlook (${m.email})` : "Outlook";
      mEl.title = m.connected ? `Connected as ${m.email}` : m.detail || "Microsoft Email not connected";
    }
    await updateMicrosoftUI(m);

    const seamlessBtn = $("btn-seamless-sidebar");
    if (seamlessBtn) {
      const showConnect = !!s.can_connect && !s.connected;
      seamlessBtn.classList.toggle("hidden", !showConnect);
    }

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
    if (headerBtn && !headerBtn.classList.contains("disabled") && headerBtn.getAttribute("href")) {
      window.location.href = "/api/gmail/oauth/start";
    } else {
      switchView("settings");
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
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

/* ---------------- Microsoft / Outlook connect ---------------- */

const MS_CONNECT_BTNS = [
  "btn-ms-connect",
  "btn-ms-header",
  "btn-ms-sidebar",
];

const MS_DISCONNECT_BTNS = [
  "btn-ms-disconnect-header",
  "btn-ms-disconnect-sidebar",
  "btn-ms-disconnect-pending",
  "btn-ms-disconnect-card",
];

function setMsDisconnectButtonsVisible(visible) {
  for (const id of MS_DISCONNECT_BTNS) {
    const btn = $(id);
    if (btn) btn.classList.toggle("hidden", !visible);
  }
}

function setMsConnectButtons(enabled) {
  for (const id of MS_CONNECT_BTNS) {
    const btn = $(id);
    if (!btn) continue;
    if (btn.tagName === "A") {
      if (enabled) {
        btn.removeAttribute("aria-disabled");
        btn.href = "/api/microsoft/oauth/start";
        btn.classList.remove("disabled");
      } else {
        btn.setAttribute("aria-disabled", "true");
        btn.removeAttribute("href");
        btn.classList.add("disabled");
      }
    } else {
      btn.disabled = !enabled;
    }
  }
}

function toggleMsConnectButtons(visible) {
  for (const id of MS_CONNECT_BTNS) {
    const btn = $(id);
    if (btn) btn.classList.toggle("hidden", !visible);
  }
}

function setMsConnectedEmail(email) {
  for (const id of ["ms-card-email", "ms-header-email", "ms-sidebar-email"]) {
    const el = $(id);
    if (el) el.textContent = email || "";
  }
}

function hideAllMsPanels() {
  for (const id of ["ms-connect-panel", "ms-connected-panel", "ms-pending-panel"]) {
    const el = $(id);
    if (el) el.classList.add("hidden");
  }
}

function setMsCardVisible(visible) {
  const card = $("ms-card");
  if (card) card.classList.toggle("hidden", !visible);
}

function setMsHeaderSidebarStatus(m, mode) {
  const sidebar = $("ms-sidebar-status");
  const header = $("ms-header-status");
  if (!sidebar || !header) return;
  const sidebarLabel = sidebar.querySelector(".gmail-status-label");
  const headerBadge = header.querySelector(".gmail-status-badge");
  $("btn-ms-sidebar")?.classList.add("hidden");
  $("btn-ms-header")?.classList.add("hidden");
  sidebar.classList.remove("hidden");
  header.classList.remove("hidden");
  sidebar.classList.toggle("pending", mode === "pending");
  header.classList.toggle("pending", mode === "pending");
  if (sidebarLabel) sidebarLabel.textContent = mode === "pending" ? "Signed in" : "Outlook";
  if (headerBadge) {
    headerBadge.textContent = mode === "pending" ? "Signed in" : "Outlook";
    headerBadge.classList.toggle("pending", mode === "pending");
  }
  setMsConnectedEmail(m.email);
  const addr = m.email || "";
  $("ms-sidebar-email").textContent = addr;
  $("ms-header-email").textContent = addr;
}

function showMsConnectedUI(m) {
  hideAllMsPanels();
  setMsCardVisible(false);
  toggleMsConnectButtons(false);
  setMsDisconnectButtonsVisible(true);
  setMsHeaderSidebarStatus(m, "connected");
}

function showMsPendingUI(m) {
  const pending = $("ms-pending-panel");
  hideAllMsPanels();
  if (pending) pending.classList.remove("hidden");
  const card = $("ms-card");
  if (card) {
    card.classList.remove("connected");
    card.classList.add("pending");
  }
  setMsCardVisible(true);
  toggleMsConnectButtons(false);
  setMsDisconnectButtonsVisible(true);
  setMsHeaderSidebarStatus(m, "pending");
  const badge = $("ms-pending-badge");
  const email = $("ms-pending-email");
  const detail = $("ms-pending-detail");
  if (badge) badge.textContent = "Signed in";
  if (email) email.textContent = m.email || "";
  if (detail) detail.textContent = m.detail || "Finishing Microsoft Email setup…";
}

function showMsDisconnectedUI() {
  const connect = $("ms-connect-panel");
  hideAllMsPanels();
  if (connect) connect.classList.remove("hidden");
  const card = $("ms-card");
  if (card) {
    card.classList.remove("connected", "pending");
  }
  setMsCardVisible(true);
  toggleMsConnectButtons(true);
  setMsDisconnectButtonsVisible(false);
  $("btn-ms-sidebar")?.classList.remove("hidden");
  $("ms-sidebar-status")?.classList.add("hidden");
  $("btn-ms-header")?.classList.remove("hidden");
  $("ms-header-status")?.classList.add("hidden");
}

async function updateMicrosoftUI(m) {
  const card = $("ms-card");
  const meta = $("ms-connect-meta");
  const mEl = $("health-ms");
  if (!card) return;

  if (m.connected) {
    showMsConnectedUI(m);
    if (mEl) {
      mEl.querySelector(".dot").className = "dot ok";
      mEl.querySelector(".health-label").textContent = m.email ? `Outlook (${m.email})` : "Outlook";
      mEl.title = `Connected as ${m.email}`;
    }
    return;
  }

  if (m.authenticated || m.has_token) {
    showMsPendingUI(m);
    if (mEl) {
      mEl.querySelector(".dot").className = "dot bad";
      mEl.querySelector(".health-label").textContent = m.email ? `Outlook (${m.email})` : "Outlook (signed in)";
      mEl.title = m.detail || "Microsoft signed in but not fully connected";
    }
    return;
  }

  showMsDisconnectedUI();
  if (mEl) {
    mEl.querySelector(".dot").className = "dot bad";
    mEl.classList.add("clickable");
    mEl.onclick = () => {
      switchView("settings");
      card?.scrollIntoView({ behavior: "smooth", block: "center" });
    };
  }

  if (m.can_connect === false && !m.needs_operator_setup) {
    const headerBtn = $("btn-ms-header");
    if (headerBtn && headerBtn.tagName === "A") {
      headerBtn.href = "/api/microsoft/oauth/start";
    }
  }

  if (m.needs_operator_setup) {
    if ($("ms-connect-desc")) {
      $("ms-connect-desc").textContent =
        "Microsoft Email isn't configured on this server yet. Your administrator needs to set it up once.";
    }
    if (meta) {
      meta.innerHTML =
        "End users only need to click <b>Connect Microsoft Email</b> once the server is configured. " +
        "Ask your admin to add MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET.";
    }
    setMsConnectButtons(false);
    return;
  }

  if ($("ms-connect-desc")) {
    $("ms-connect-desc").textContent =
      "Connect Microsoft 365 / Outlook to create drafts, send outreach, and scan for replies.";
  }
  try {
    const setup = await api("/api/microsoft/setup");
    if (meta && setup.redirect_uri) {
      meta.innerHTML = `Redirect URI for Azure: <code>${esc(setup.redirect_uri)}</code>`;
    }
  } catch {
    if (meta) meta.textContent = "";
  }
  setMsConnectButtons(true);
}

function handleMicrosoftOAuthReturn() {
  const params = new URLSearchParams(location.search);
  const result = params.get("microsoft");
  if (!result) return;
  const msgs = {
    connected: "Microsoft sign-in complete — checking Outlook status…",
    denied: "Microsoft Email connection was cancelled.",
    error: "Microsoft Email connection failed — try again.",
  };
  showToast(msgs[result] || "Microsoft Email connection finished.", result === "connected");
  history.replaceState({}, "", location.pathname);
  loadHealth(true);
}

function handleSeamlessOAuthReturn() {
  const params = new URLSearchParams(location.search);
  const result = params.get("seamless");
  if (!result) return;
  const msgs = {
    connected: "Seamless connected — Woodway will use your account credits.",
    denied: "Seamless connection was cancelled.",
    error: "Seamless connection failed — check OAuth settings in .env.",
  };
  showToast(msgs[result] || "Seamless connection finished.", result === "connected");
  history.replaceState({}, "", location.pathname);
  loadHealth(true);
}

$("btn-ms-refresh")?.addEventListener("click", () => loadHealth(true));

async function disconnectMicrosoft() {
  const ok = confirm(
    "Disconnect Microsoft Email from JayAgents?\n\nYou'll need to sign in again to create Outlook drafts, send outreach, or scan for replies."
  );
  if (!ok) return;

  for (const id of MS_DISCONNECT_BTNS) {
    const btn = $(id);
    if (btn) btn.disabled = true;
  }

  try {
    await api("/api/microsoft/disconnect", { method: "POST" });
    microsoftConnected = false;
    showToast("Microsoft Email disconnected — you can connect a different account anytime.");
    await loadHealth(true);
  } catch (e) {
    showToast(e.message, false);
  } finally {
    for (const id of MS_DISCONNECT_BTNS) {
      const btn = $(id);
      if (btn) btn.disabled = false;
    }
  }
}

for (const id of MS_DISCONNECT_BTNS) {
  $(id)?.addEventListener("click", disconnectMicrosoft);
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
let activeJobId = null;

const RUN_LABELS = {
  requalify_all: "Re-qualify all",
  discover: "Discover prospects",
  process_imported: "Process imported",
  contact_search: "Contact search",
  woodway_pipeline: "Woodway pipeline",
  keira_pipeline: "Keira pipeline",
  recontact: "Re-Contact",
};

function updateContactSearchButton() {
  const btn = $("btn-contacts");
  if (!btn) return;
  if (currentAgent === "woodway") {
    btn.textContent = "Woodway pipeline";
    btn.title = "Claude companies → Seamless/web contacts → qualify → Outlook drafts";
  } else if (currentAgent === "keira" && (seamlessConfigured || actavaConfigured)) {
    const rem = seamlessBudget?.credits_remaining_budget;
    btn.textContent = rem != null ? `Keira pipeline (${rem} left)` : "Keira pipeline";
    btn.title = "Company-first gates → Seamless for all non-rejected → confidential drafts";
  } else {
    btn.textContent = "Contact search";
    btn.title = "";
  }
  const re = $("btn-recontact");
  if (re) {
    const show = currentAgent === "woodway" || currentAgent === "keira";
    re.classList.toggle("hidden", !show);
    re.title = "Contact search for awaiting_contact + leads still missing email";
  }
}

function setActionButtonsDisabled(disabled) {
  for (const id of ["btn-requalify", "btn-discover", "btn-process", "btn-contacts", "btn-recontact"]) {
    const el = $(id);
    if (el) el.disabled = disabled;
  }
}

function renderJobPanel(job, label) {
  const panel = $("job-panel");
  const title = $("job-title");
  const status = $("job-status");
  const logEl = $("job-log");
  const cancelBtn = $("btn-cancel-job");
  if (!panel || !title || !status || !logEl) return;

  panel.classList.remove("hidden");
  title.textContent = label || "Agent task";
  const statusText = {
    running: "Running…",
    done: "Complete",
    error: "Failed",
    cancelled: "Cancelled",
  };
  status.textContent = statusText[job.status] || job.status || "Running…";
  status.className = "job-status " + (job.status || "running");
  logEl.textContent = (job.log || []).map((e) => e.msg).join("\n") || "Starting…";
  logEl.scrollTop = logEl.scrollHeight;
  if (cancelBtn) {
    cancelBtn.classList.toggle("hidden", job.status !== "running");
    cancelBtn.disabled = !!job.cancel_requested;
    cancelBtn.textContent = job.cancel_requested ? "Cancelling…" : "Cancel";
  }
}

function extractSeamlessBudgetAlerts(job) {
  const alerts = [];
  const seen = new Set();
  const push = (msg) => {
    const m = String(msg || "").trim();
    if (!m || seen.has(m)) return;
    seen.add(m);
    alerts.push(m);
  };
  for (const entry of job?.log || []) {
    const msg = entry?.msg || "";
    if (msg.includes("[SEAMLESS_BUDGET]") || /Seamless research (budget empty|blocked)/i.test(msg)) {
      push(msg.replace(/^\[SEAMLESS_BUDGET\]\s*/i, "").trim());
    }
  }
  const result = job?.result;
  if (result && typeof result === "object") {
    if (result.budget_alert?.message) push(result.budget_alert.message.replace(/^\[SEAMLESS_BUDGET\]\s*/i, "").trim());
    if (result.budget_alert?.reason) push(result.budget_alert.reason);
    for (const a of result.alerts || []) {
      if (a?.message) push(String(a.message).replace(/^\[SEAMLESS_BUDGET\]\s*/i, "").trim());
      else if (a?.reason) push(a.reason);
    }
    const enrichNote = result.steps?.enrich?.people_search?.note || result.steps?.enrich?.note;
    if (enrichNote && /budget|exhausted|blocked/i.test(enrichNote)) push(enrichNote);
  }
  return alerts;
}

async function pollJob(jobId, label) {
  if (activeJobPoll) clearInterval(activeJobPoll);
  activeJobId = jobId;
  let budgetToastShown = false;
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const job = await api(`/api/jobs/${jobId}`);
        renderJobPanel(job, label);

        // Mid-run: toast as soon as Seamless budget exhaustion appears in logs
        if (!budgetToastShown) {
          const live = extractSeamlessBudgetAlerts(job);
          if (live.length) {
            budgetToastShown = true;
            showToast(`Seamless budget: ${live[0]}`, false);
            loadNotifications();
            loadHealth();
          }
        }

        if (job.status !== "running") {
          clearInterval(activeJobPoll);
          activeJobPoll = null;
          activeJobId = null;
          setActionButtonsDisabled(false);
          const budgetAlerts = extractSeamlessBudgetAlerts(job);
          if (job.status === "done") {
            if (budgetAlerts.length) {
              if (!budgetToastShown) {
                showToast(`Seamless budget ran out mid-run — ${budgetAlerts[0]}`, false);
              }
              await Promise.all([loadNotifications(), loadLeads(), refreshStats(), loadAgents().then(() => renderNav()), loadHealth()]);
            } else {
              showToast(job.log?.at(-1)?.msg || "Task complete", true);
              await Promise.all([loadLeads(), refreshStats(), loadAgents().then(() => renderNav())]);
            }
          } else if (job.status === "cancelled") {
            showToast("Task cancelled", false);
          } else {
            showToast(job.error || "Task failed", false);
          }
          resolve(job);
        }
      } catch (e) {
        clearInterval(activeJobPoll);
        activeJobPoll = null;
        activeJobId = null;
        setActionButtonsDisabled(false);
        showToast(e.message, false);
        resolve(null);
      }
    };
    tick();
    activeJobPoll = setInterval(tick, 3000);
  });
}

async function cancelActiveJob() {
  if (!activeJobId) return;
  const btn = $("btn-cancel-job");
  if (btn) btn.disabled = true;
  try {
    await api(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
    showToast("Cancel requested…", false);
  } catch (e) {
    if (btn) btn.disabled = false;
    showToast(e.message, false);
  }
}

$("btn-cancel-job")?.addEventListener("click", cancelActiveJob);

async function runAgentAction(mode, { limit } = {}) {
  if (!currentAgent) return;
  const label = RUN_LABELS[mode] || mode;
  const limits = {
    requalify_all: limit ?? 500,
    discover: limit ?? 5,
    process_imported: limit ?? 25,
    contact_search: limit ?? 25,
    woodway_pipeline: limit ?? 50,
    keira_pipeline: limit ?? 10,
    recontact: limit ?? 50,
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
$("btn-recontact")?.addEventListener("click", () => runAgentAction("recontact"));
$("btn-contacts")?.addEventListener("click", () => {
  if (currentAgent === "woodway") {
    runAgentAction("woodway_pipeline");
  } else if (currentAgent === "keira" && (seamlessConfigured || actavaConfigured)) {
    runAgentAction("keira_pipeline");
  } else {
    runAgentAction("contact_search");
  }
});

/* ---------------- automation ---------------- */

async function loadAutomation() {
  try {
    const a = await api("/api/automation");
    const meta = [];
    if (a.last_daily_run) {
      const ok = a.last_daily_run.ok ? "✓" : "✗ failed";
      meta.push(`<span class="chip"><b>Last nightly run:</b> ${fmtDateTime(a.last_daily_run.finished_at)} ${ok}</span>`);
    } else {
      meta.push(`<span class="chip"><b>Last nightly run:</b> not yet run</span>`);
    }
    if (a.schedule) {
      meta.push(`<span class="chip"><b>Schedule:</b> ${esc(a.schedule)}</span>`);
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
    let replies = 0;
    let checked = 0;
    if (gmailConnected) {
      const r = await api("/api/gmail/scan-replies", { method: "POST" });
      replies += r.replies || 0;
      checked += r.checked || 0;
    }
    if (microsoftConnected) {
      const r = await api("/api/microsoft/scan-replies", { method: "POST" });
      replies += r.replies || 0;
      checked += r.checked || 0;
    }
    if (!gmailConnected && !microsoftConnected) {
      throw new Error("Connect Gmail or Microsoft Email first");
    }
    btn.textContent = replies ? `${replies} new replies!` : "No new replies";
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
  await Promise.all([refreshStats(), loadLeads(), loadPrompts(), loadToday()]);
}

async function loadToday() {
  const el = $("today-stats");
  if (!el || !currentAgent) return;
  try {
    const data = await api(`/api/agents/${currentAgent}/today`);
    const t = data.today || {};
    const c = data.costs || {};
    el.innerHTML = `
      <div class="stat"><div class="stat-value">${t.drafts_to_review || 0}</div><div class="stat-label">Drafts</div></div>
      <div class="stat"><div class="stat-value">${t.replies_need_review || 0}</div><div class="stat-label">Replies</div></div>
      <div class="stat"><div class="stat-value">${t.new_signals || 0}</div><div class="stat-label">Signals</div></div>
      <div class="stat"><div class="stat-value">$${(c.total_cost_usd || 0).toFixed(2)}</div><div class="stat-label">Cost</div></div>`;
  } catch {
    el.innerHTML = "";
  }
}

async function loadSignals() {
  const list = $("signals-list");
  if (!list || !currentAgent) return;
  try {
    const data = await api(`/api/agents/${currentAgent}/signals`);
    list.innerHTML = (data.signals || []).map((s) => `
      <div class="signal-row">
        <div><b>${esc(s.label || s.signal_type)}</b> — ${esc(s.company || s.company_domain || "")}</div>
        <div class="signal-snippet">${esc((s.snippet || "").slice(0, 160))}</div>
        ${s.source_url ? `<a href="${esc(s.source_url)}" target="_blank" rel="noopener">Source ↗</a>` : ""}
      </div>`).join("") || "<p class='automation-desc'>No new signals this week.</p>";
  } catch (e) {
    list.innerHTML = `<p class='automation-desc'>${esc(e.message)}</p>`;
  }
}

function switchView(view) {
  document.querySelectorAll(".view-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("leads-panel")?.classList.toggle("hidden", view !== "leads");
  $("review-panel")?.classList.toggle("hidden", view !== "review");
  $("run-panel")?.classList.toggle("hidden", view !== "run");
  $("settings-panel")?.classList.toggle("hidden", view !== "settings");
  $("overview-card")?.classList.toggle("hidden", view !== "leads");
  if (view === "review") loadQaQueue();
  if (view === "settings") loadSignals();
}

async function enterReviewMode() {
  if (!currentAgent) return;
  reviewQueue = await api(`/api/agents/${currentAgent}/review-queue`);
  reviewIndex = 0;
  reviewMode = true;
  $("btn-review-mode")?.classList.add("active");
  $("m-review-actions")?.classList.remove("hidden");
  if (reviewQueue.length) openModal(reviewQueue[0].id);
  else showToast("Review queue is empty");
}

async function reviewAction(action) {
  if (!modalLeadId) return;
  await api(`/api/leads/${modalLeadId}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action }),
  });
  await loadLeads();
  await loadToday();
  if (reviewMode && reviewQueue.length) {
    reviewIndex = Math.min(reviewIndex + 1, reviewQueue.length - 1);
    if (reviewIndex < reviewQueue.length) openModal(reviewQueue[reviewIndex].id);
  }
}

function renderHeader() {
  const a = agentData(currentAgent);
  $("agent-title").textContent = a.company;
  $("agent-tagline").textContent = `${a.product} — ${a.tagline}`;

  const strip = $("icp-strip");
  const chips = [];
  if (a.industries.length)
    chips.push(`<span class="chip"><b>ICP</b> ${esc(a.industries.slice(0, 3).join(", "))}</span>`);
  if (a.titles.length)
    chips.push(`<span class="chip">${esc(a.titles.slice(0, 3).join(", "))}</span>`);
  if (a.geography.length)
    chips.push(`<span class="chip">${esc(String(a.geography.slice(0, 2).join(", ")).slice(0, 48))}</span>`);
  strip.innerHTML = chips.join("");
}

async function refreshStats() {
  agents = await api("/api/agents");
  renderNav();
  const s = agentData(currentAgent).stats;
  const drafted = s.by_status.drafted || 0;
  const qualified = s.qualified_ready ?? ((s.by_status.qualified || 0) + drafted);
  const awaiting = s.awaiting_contact ?? (s.by_status.awaiting_contact || 0);
  $("stats-row").innerHTML = `
    <div class="stat"><div class="stat-value">${s.total}</div><div class="stat-label">Total leads</div></div>
    <div class="stat"><div class="stat-value">${s.with_email}</div><div class="stat-label">With email</div></div>
    <div class="stat"><div class="stat-value">${s.linkedin_only || 0}</div><div class="stat-label">LinkedIn only</div></div>
    <div class="stat"><div class="stat-value">${drafted}</div><div class="stat-label">Outreach drafted</div></div>
    <div class="stat"><div class="stat-value">${qualified}</div><div class="stat-label">Qualified + drafted</div></div>
    <div class="stat"><div class="stat-value">${awaiting}</div><div class="stat-label">Awaiting contact</div></div>`;
  renderFunnel(s);
  loadQaQueue();
}

async function loadQaQueue() {
  const list = $("qa-list");
  if (!list) return;
  try {
    const data = await api(`/api/agents/${currentAgent}/qa-queue?limit=10`);
    const leads = data.leads || [];
    if (!leads.length) {
      list.innerHTML = `<div class="funnel-empty">No drafts awaiting QA</div>`;
      return;
    }
    list.innerHTML = leads.map((l) => `
      <div class="qa-row" data-id="${l.id}">
        <div class="qa-main">
          <strong>${esc(l.company || "")}</strong>
          <span class="muted">${esc(l.contact_name || "—")} · ${esc(l.email || "no email")} · score ${l.score ?? "—"}</span>
          <div class="qa-subject">${esc(l.outreach_subject || "(no subject)")}</div>
        </div>
        <div class="qa-actions">
          <button class="btn small primary" data-qa="accept" type="button">Accept</button>
          <button class="btn small" data-qa="reject" type="button">Reject</button>
          <button class="btn small ghost" data-open="${l.id}" type="button">Open</button>
        </div>
      </div>
    `).join("");
    list.querySelectorAll("[data-qa]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const row = btn.closest(".qa-row");
        const id = row?.dataset.id;
        if (!id) return;
        const action = btn.getAttribute("data-qa");
        let notes = null;
        if (action === "reject") notes = prompt("Reject reason (optional):") || "";
        await api(`/api/leads/${id}/qa`, {
          method: "POST",
          body: JSON.stringify({ action, notes }),
        });
        showToast(action === "accept" ? "QA accepted" : "QA rejected", true);
        loadQaQueue();
        loadLeads();
      });
    });
    list.querySelectorAll("[data-open]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.getAttribute("data-open"));
        if (id) openModal(id);
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="funnel-empty">QA queue unavailable</div>`;
  }
}

$("btn-qa-refresh")?.addEventListener("click", () => loadQaQueue());

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
  if (l.contact_tier) meta.push(`<span class="chip">${esc(l.contact_tier_label || l.contact_tier)}</span>`);
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

  const evidence = l.evidence || [];
  $("m-evidence-wrap")?.classList.toggle("hidden", !evidence.length);
  if ($("m-evidence")) {
    $("m-evidence").innerHTML = evidence.map((e) =>
      `<li><b>${esc(e.field)}:</b> ${esc(e.value || "")} — <a href="${esc(e.source_url || "#")}" target="_blank" rel="noopener">source</a><br><span class="signal-snippet">${esc((e.snippet || "").slice(0, 120))}</span></li>`
    ).join("");
  }

  const steps = l.sequence_steps || [];
  $("m-sequence-wrap")?.classList.toggle("hidden", !steps.length);
  if ($("m-sequence")) {
    $("m-sequence").innerHTML = steps.map((s) =>
      `<div class="signal-row"><b>Touch ${s.step_number}</b> (${esc(s.channel)}) — ${esc(s.scheduled_for || "")}<pre class="outreach-pre">${esc((s.content || "").slice(0, 400))}</pre></div>`
    ).join("");
  }

  $("m-review-actions")?.classList.toggle("hidden", !reviewMode);
  $("m-actava-dive")?.classList.toggle("hidden", !(l.score >= 70));

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

  const hasLinkedinNote = !!(l.linkedin_note && String(l.linkedin_note).trim());
  $("m-linkedin-note-wrap")?.classList.toggle("hidden", !hasLinkedinNote);
  if ($("m-linkedin-note")) $("m-linkedin-note").textContent = l.linkedin_note || "";
  $("m-linkedin-copy")?.classList.toggle("hidden", !hasLinkedinNote);

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

function outlookDraftUrl(draftId, webLink) {
  if (webLink) return webLink;
  return `https://outlook.office.com/mail/deeplink/read/${encodeURIComponent(draftId)}`;
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

async function importLeadToOutlook(lead) {
  const result = await api(`/api/leads/${lead.id}/microsoft-draft`, { method: "POST" });
  showToast(`Draft saved to Outlook as ${result.sender || "you"} — review and send from Outlook`, true);
  if (result.outlook_url) {
    window.open(result.outlook_url, "_blank", "noopener");
  }
  await openModal(lead.id);
  loadLeads();
  return result;
}

function renderOutreachGmailActions(l) {
  const bar = $("m-gmail-bar");
  const importBtn = $("m-gmail-import");
  const msImportBtn = $("m-ms-import");
  const openBtn = $("m-gmail-open");
  const msOpenBtn = $("m-ms-open");
  const linkedinBtn = $("m-linkedin-open");
  const hint = $("m-gmail-hint");
  const hasOutreach = !!(l.outreach_body || l.outreach_subject);
  const channel = l.contact_channel || (l.email ? "email" : l.linkedin_url ? "linkedin" : "incomplete");

  const hideMailBtns = () => {
    importBtn.classList.add("hidden");
    msImportBtn?.classList.add("hidden");
    openBtn.classList.add("hidden");
    msOpenBtn?.classList.add("hidden");
  };

  if (!hasOutreach) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");

  if (channel === "linkedin" && l.linkedin_url) {
    hideMailBtns();
    linkedinBtn.classList.remove("hidden");
    linkedinBtn.href = normalizeUrl(l.linkedin_url);
    const noteHint = l.linkedin_note ? " Copy the connection note above and paste it in LinkedIn." : "";
    const hunterHint = l.can_hunter_research
      ? " Email not found — try Hunter above or use LinkedIn for outreach."
      : "";
    hint.textContent = (l.contact_message || "Email not found — LinkedIn is the best channel for outreach.") + noteHint + hunterHint;
    return;
  }

  linkedinBtn.classList.add("hidden");

  if (!l.email || !String(l.email).includes("@")) {
    hideMailBtns();
    importBtn.classList.remove("hidden");
    importBtn.disabled = true;
    importBtn.textContent = "Import to Gmail";
    hint.textContent = l.contact_message || "This lead needs a contact email before importing to email.";
    importBtn.onclick = null;
    if (msImportBtn) msImportBtn.onclick = null;
    return;
  }

  if (l.gmail_draft_id) {
    hideMailBtns();
    const isMs = (l.mail_provider || "gmail") === "microsoft";
    if (isMs && msOpenBtn) {
      msOpenBtn.classList.remove("hidden");
      msOpenBtn.href = outlookDraftUrl(l.gmail_draft_id, l.outlook_url);
      hint.textContent = "Draft is in Outlook — open it there to review and send.";
    } else {
      openBtn.classList.remove("hidden");
      openBtn.href = gmailDraftUrl(l.gmail_draft_id);
      hint.textContent = "Draft is in Gmail — open it there to review and send.";
    }
    return;
  }

  if (!gmailConnected && !microsoftConnected) {
    hideMailBtns();
    importBtn.classList.remove("hidden");
    importBtn.disabled = true;
    importBtn.textContent = "Import to Gmail";
    hint.textContent = "Connect Gmail or Microsoft Email first — creates a draft only, nothing is sent.";
    importBtn.onclick = null;
    if (msImportBtn) msImportBtn.onclick = null;
    return;
  }

  hideMailBtns();
  const bits = [];
  if (gmailConnected) {
    importBtn.classList.remove("hidden");
    importBtn.disabled = false;
    importBtn.textContent = "Import to Gmail";
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
    bits.push("Gmail");
  }
  if (microsoftConnected && msImportBtn) {
    msImportBtn.classList.remove("hidden");
    msImportBtn.disabled = false;
    msImportBtn.textContent = "Import to Outlook";
    msImportBtn.onclick = async () => {
      msImportBtn.disabled = true;
      msImportBtn.textContent = "Importing…";
      try {
        await importLeadToOutlook(l);
      } catch (e) {
        showToast(e.message, false);
        msImportBtn.disabled = false;
        msImportBtn.textContent = "Import to Outlook";
      }
    };
    bits.push("Outlook");
  }
  hint.textContent = `Creates a draft in your ${bits.join(" or ")}. You send it from your mailbox when ready.`;
}

function normalizeUrl(u) {
  return /^https?:\/\//i.test(u) ? u : `https://${u}`;
}

$("modal-close").onclick = () => $("modal").classList.add("hidden");
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("modal").classList.add("hidden");
  if (!$("modal") || $("modal").classList.contains("hidden") || !reviewMode) return;
  if (e.key === "j" || e.key === "J") {
    reviewIndex = Math.min(reviewIndex + 1, reviewQueue.length - 1);
    if (reviewQueue[reviewIndex]) openModal(reviewQueue[reviewIndex].id);
  }
  if (e.key === "k" || e.key === "K") {
    reviewIndex = Math.max(reviewIndex - 1, 0);
    if (reviewQueue[reviewIndex]) openModal(reviewQueue[reviewIndex].id);
  }
  if (e.key === "a" || e.key === "A") reviewAction("approve");
  if (e.key === "x" || e.key === "X") reviewAction("reject");
  if (e.key === "r" || e.key === "R") reviewAction("regenerate");
});
$("modal").onclick = (e) => { if (e.target === $("modal")) $("modal").classList.add("hidden"); };
$("m-copy").onclick = () => {
  navigator.clipboard.writeText($("m-outreach").textContent);
  $("m-copy").textContent = "Copied!";
  setTimeout(() => ($("m-copy").textContent = "Copy"), 1500);
};
$("m-linkedin-copy")?.addEventListener("click", () => {
  navigator.clipboard.writeText($("m-linkedin-note").textContent);
  $("m-linkedin-copy").textContent = "Copied!";
  setTimeout(() => ($("m-linkedin-copy").textContent = "Copy note"), 1500);
});

document.querySelectorAll(".view-tab").forEach((tab) => {
  tab.onclick = () => switchView(tab.dataset.view);
});
$("btn-review-mode")?.addEventListener("click", enterReviewMode);
$("m-approve")?.addEventListener("click", () => reviewAction("approve"));
$("m-reject")?.addEventListener("click", () => reviewAction("reject"));
$("m-regen")?.addEventListener("click", () => reviewAction("regenerate"));
$("m-actava-dive")?.addEventListener("click", async () => {
  if (!modalLeadId) return;
  try {
    await api(`/api/leads/${modalLeadId}/actava-deep-dive`, { method: "POST" });
    showToast("Actava deep dive complete");
    openModal(modalLeadId);
  } catch (e) {
    showToast(e.message, false);
  }
});
$("btn-load-suppression")?.addEventListener("click", async () => {
  if (!currentAgent) return;
  const data = await api(`/api/agents/${currentAgent}/suppression`);
  $("suppression-list").textContent = JSON.stringify(data.entries || [], null, 2);
});

$("status-filter").onchange = loadLeads;
$("search-filter").addEventListener("input", renderLeads);

/* ---------------- prompts ---------------- */

function syncKeiraPromptTabs() {
  const isKeira = currentAgent === "keira";
  document.querySelectorAll(".keira-only").forEach((el) => {
    el.classList.toggle("hidden", !isKeira);
  });
  if (!isKeira) {
    const activeKeira = document.querySelector(".prompts-tab.active[data-tab='analyst'], .prompts-tab.active[data-tab='critic']");
    if (activeKeira) {
      document.querySelectorAll(".prompts-tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".prompts-panel").forEach((p) => p.classList.remove("active"));
      document.querySelector('.prompts-tab[data-tab="qualify"]')?.classList.add("active");
      $("prompts-panel-qualify")?.classList.add("active");
    }
  }
}

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
    if ($("prompt-analyst-system")) {
      $("prompt-analyst-system").value = p.values.analyst_system || "";
    }
    if ($("prompt-critic-system")) {
      $("prompt-critic-system").value = p.values.critic_system || "";
    }
    $("prompt-qualify-template").textContent = p.templates.qualify_user || "";
    $("prompt-outreach-template").textContent = p.templates.outreach_user || "";
    syncKeiraPromptTabs();
    updatePromptsSummary(p);
    hidePromptStatus();
  } catch (e) {
    showPromptStatus(e.message, true);
  }
}

function promptPayload() {
  const payload = {
    qualify_system: $("prompt-qualify-system").value,
    qualify_extra: $("prompt-qualify-extra").value,
    outreach_system: $("prompt-outreach-system").value,
    outreach_extra: $("prompt-outreach-extra").value,
  };
  if (currentAgent === "keira") {
    payload.analyst_system = $("prompt-analyst-system")?.value || "";
    payload.critic_system = $("prompt-critic-system")?.value || "";
  }
  return payload;
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
    ? "Custom Anthropic prompts saved — next run will use them."
    : "Using default Anthropic prompts. Open Edit prompts to customize.";
}

$("prompts-toggle")?.addEventListener("click", () => {
  const body = $("prompts-body");
  const btn = $("prompts-toggle");
  const expanded = body.classList.toggle("hidden") === false;
  btn.setAttribute("aria-expanded", String(expanded));
  btn.textContent = expanded ? "Hide prompts" : "Edit prompts";
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
        analyst_system: "",
        critic_system: "",
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
