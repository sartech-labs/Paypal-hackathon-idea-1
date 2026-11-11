/* SARA home page: sign-in dialog, demo sign-in, and the illustrated gate log. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const all = (selector) => Array.from(document.querySelectorAll(selector));

  // --- sign-in -------------------------------------------------------------------------
  let auth = { login_enabled: true, password_login: true, demo_signin: false, signed_in: false };

  function goToConsole() {
    window.location.assign("/console");
  }

  function applyAuth() {
    const signedIn = auth.signed_in && auth.login_enabled;
    for (const node of all("[data-open-signin]")) node.hidden = signedIn || !auth.login_enabled;
    for (const node of all("[data-demo-signin]")) node.hidden = signedIn || !auth.demo_signin;
    if ($("judges")) $("judges").hidden = signedIn || !auth.demo_signin;
    if ($("open-console")) $("open-console").hidden = !(signedIn || !auth.login_enabled);
    if ($("hero-console")) $("hero-console").hidden = !(signedIn || !auth.login_enabled);
    if ($("signin-form")) $("signin-form").hidden = !auth.password_login;
    if ($("signin-off")) $("signin-off").hidden = auth.password_login;
    if ($("signin-demo")) $("signin-demo").hidden = !auth.demo_signin;
  }

  function showError(message) {
    const node = $("signin-error");
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  function openSignin() {
    const dialog = $("signin");
    if (!dialog) return;
    showError(null);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    const email = $("signin-email");
    if (email && auth.password_login) email.focus();
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : "{}",
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* empty body */ }
    return { ok: response.ok, status: response.status, data };
  }

  async function signIn(event) {
    event.preventDefault();
    const email = $("signin-email").value.trim();
    const password = $("signin-password").value;
    if (!email || !password) {
      showError("Enter your email and password.");
      return;
    }
    const button = $("signin-submit");
    button.disabled = true;
    button.textContent = "Signing in...";
    try {
      const result = await postJson("/auth/login", { email, password });
      if (result.ok) {
        goToConsole();
        return;
      }
      showError(result.data.message || `Sign-in failed (HTTP ${result.status}).`);
    } catch (_) {
      showError("Could not reach the gateway. Check your connection and try again.");
    } finally {
      button.disabled = false;
      button.textContent = "Sign in";
    }
  }

  async function demoSignIn(event) {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await postJson("/auth/demo");
      if (result.ok) {
        goToConsole();
        return;
      }
      openSignin();
      showError(result.data.message || "The demo account is not available.");
    } catch (_) {
      openSignin();
      showError("Could not reach the gateway. Try again in a moment.");
    } finally {
      button.disabled = false;
    }
  }

  async function loadAuth() {
    try {
      const response = await fetch("/auth/me", { credentials: "same-origin" });
      if (response.ok) auth = await response.json();
    } catch (_) { /* keep defaults: the sign-in button still works */ }
    applyAuth();
  }

  // --- gate log illustration -----------------------------------------------------------
  const SCRIPTS = [
    {
      agent: "ResearchBot/2.1",
      tone: "info",
      note: "Signed agent · summarising the news",
      rows: [
        ["GET <b>/news/economy</b>", "402 Deposit $5.00", "warn"],
        ["PayPal <b>hold placed</b>", "AUTHORIZED", "info"],
        ["GET <b>/news/economy</b>", "200 Allowed", "good"],
        ["GET <b>/news/technology</b> · waits 2s", "200 Allowed", "good"],
        ["Session <b>complete</b> · 0 violations", "Hold voided", "good"],
      ],
      outcome: ["Deposit returned", "$5.00", "good", "No rules broken. The PayPal hold was voided, so the agent paid nothing."],
    },
    {
      agent: "unknown-scraper",
      tone: "bad",
      note: "Unsigned · told to copy everything",
      rows: [
        ["GET <b>/news</b>", "402 Deposit $5.00", "warn"],
        ["PayPal <b>hold placed</b>", "AUTHORIZED", "info"],
        ["GET <b>/admin</b>", "403 Disallowed", "bad"],
        ["GET <b>/%2e%2e/private</b>", "403 Traversal", "bad"],
        ["<b>14 requests</b> in 1s", "429 Rate limit", "bad"],
      ],
      outcome: ["Deposit kept", "$5.00", "bad", "3 violations, each cited from the audit log. The hold was captured to the site's PayPal account."],
    },
  ];

  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function renderRow(list, [request, chip, tone]) {
    const row = document.createElement("li");
    row.className = "gl-row";
    const req = document.createElement("span");
    req.className = "gl-req";
    req.innerHTML = request; // static strings from SCRIPTS above, never user input
    const badge = document.createElement("span");
    badge.className = "gl-chip";
    badge.dataset.tone = tone;
    badge.textContent = chip;
    row.append(req, badge);
    list.append(row);
  }

  function showOutcome([label, amount, tone, why]) {
    const box = $("gl-outcome");
    $("gl-outcome-label").textContent = label;
    $("gl-outcome-amount").textContent = amount;
    $("gl-outcome-why").textContent = why;
    box.dataset.tone = tone;
    box.hidden = false;
  }

  function startScript(script) {
    $("gl-agent-name").textContent = script.agent;
    $("gl-agent-name").dataset.tone = script.tone;
    $("gl-agent-note").textContent = script.note;
    $("gl-outcome").hidden = true;
    const list = $("gl-rows");
    list.textContent = "";
    return list;
  }

  async function playGateLog() {
    if (!$("gl-rows")) return;
    if (reduced) {
      const list = startScript(SCRIPTS[1]);
      SCRIPTS[1].rows.forEach((row) => renderRow(list, row));
      showOutcome(SCRIPTS[1].outcome);
      return;
    }
    for (let index = 0; ; index = (index + 1) % SCRIPTS.length) {
      const script = SCRIPTS[index];
      const list = startScript(script);
      await wait(500);
      for (const row of script.rows) {
        renderRow(list, row);
        await wait(900);
      }
      showOutcome(script.outcome);
      await wait(4200);
    }
  }

  function init() {
    for (const node of all("[data-open-signin]")) node.addEventListener("click", openSignin);
    for (const node of all("[data-demo-signin]")) node.addEventListener("click", demoSignIn);
    if ($("signin-form")) $("signin-form").addEventListener("submit", signIn);
    loadAuth();
    playGateLog();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
