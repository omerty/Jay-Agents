const $ = (id) => document.getElementById(id);

async function authApi(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

function showError(msg) {
  const el = $("auth-error");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function clearError() {
  $("auth-error").classList.add("hidden");
}

function setTab(tab) {
  document.querySelectorAll(".auth-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  $("login-form").classList.toggle("hidden", tab !== "login");
  $("signup-form").classList.toggle("hidden", tab !== "signup");
  clearError();
}

document.querySelectorAll(".auth-tab").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError();
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await authApi("/api/auth/login", {
      email: $("login-email").value.trim(),
      password: $("login-password").value,
    });
    window.location.href = "/";
  } catch (err) {
    showError(err.message || "Sign in failed");
  } finally {
    btn.disabled = false;
  }
});

$("signup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError();
  const p1 = $("signup-password").value;
  const p2 = $("signup-password2").value;
  if (p1 !== p2) {
    showError("Passwords do not match");
    return;
  }
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await authApi("/api/auth/signup", {
      email: $("signup-email").value.trim(),
      otp: $("signup-otp").value.trim(),
      password: p1,
    });
    window.location.href = "/";
  } catch (err) {
    showError(err.message || "Signup failed");
  } finally {
    btn.disabled = false;
  }
});

(async function init() {
  try {
    const r = await fetch("/api/auth/me", { credentials: "include" });
    if (r.ok) window.location.href = "/";
  } catch {}
})();
