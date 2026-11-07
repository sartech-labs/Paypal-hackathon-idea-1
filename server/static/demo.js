/* SARA live demo: start a crawler run, follow it live, and verify with PayPal.
   Values from crawlers, pages, PayPal, and AI models are inserted with textContent. */
(() => {
  "use strict";

  const POLL_MS = 700;
  const IDLE_POLL_MS = 4000;

  const state = {
    token: null,
    config: null,
    runId: null,
    nextSeq: 0,
    run: null,
    timer: null,
    pendingRow: null,
    steps: {},
    paypal: null,
    popup: null,
    lastExplanationFor: null,
    counts: { allowed: 0, blocked: 0, violations: 0 },
    note: null,
    tier: null,
    historyGrid: null,
    historyTimer: null,
    historyLoading: false,
    historyAgain: false,
    // Playback: run events are revealed one at a time so the run reads as it happened.
    queue: [],
    draining: false,
    drainTimer: null,
    latest: null,
    instant: false,
    clock: null,
    eventAt: null,
    alerts: 0,
  };
  const HISTORY_IDLE_MS = 15000;

  const $ = (id) => document.getElementById(id);

  // --- helpers -------------------------------------------------------------------------
  const store = {
    get(area, key) { try { return window[area].getItem(key); } catch (_) { return null; } },
    set(area, key, value) {
      try { if (value === null) window[area].removeItem(key); else window[area].setItem(key, value); } catch (_) { /* ignore */ }
    },
  };

  function el(tag, props, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "text") node.textContent = String(value);
      else if (key === "className") node.className = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children || []) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  function chip(tone, icon, label) {
    return el("span", { className: "chip" }, [
      el("span", { className: "chip-icon", dataset: { tone }, "aria-hidden": "true", text: icon }),
      el("span", { text: label }),
    ]);
  }

  function badge(tone, text) {
    return el("span", { className: "badge" }, [el("span", { className: "badge-dot", dataset: { tone } }), el("span", { text })]);
  }

  function providerName(code) {
    if (code === "claude" || code === "anthropic") return "Claude";
    if (code === "openai") return "OpenAI";
    return "AI";
  }

  function humanize(code) {
    if (!code) return "";
    const known = {
      DISALLOWED_PATH: "Disallowed path",
      INVALID_PATH: "Path traversal / invalid path",
      RATE_LIMIT_EXCEEDED: "Rate limit exceeded",
      MAX_REQUESTS_EXCEEDED: "Request budget exceeded",
      SESSION_ALREADY_COMPLETED: "Reused a completed session",
      TIER_NOT_COVERED: "Deposit does not cover this pricing tier",
      PAYMENT_REQUIRED: "Payment required",
      ELIGIBLE_FOR_REFUND: "Eligible for refund",
      FORFEITED_BY_POLICY: "Forfeited by policy",
    };
    if (known[code]) return known[code];
    const lower = String(code).toLowerCase().replace(/_/g, " ");
    return lower.charAt(0).toUpperCase() + lower.slice(1);
  }

  function formatTime(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  const DEPOSIT_STATUS = {
    REFUNDED: ["good", "✓", "Refunded"],
    RELEASED: ["good", "✓", "Hold released"],
    FORFEITED: ["critical", "✕", "Forfeited"],
    REFUND_PENDING: ["warning", "!", "Refund pending"],
    FAILED: ["serious", "!", "Payment failed"],
    CAPTURED: ["neutral", "●", "Held (captured)"],
    AUTHORIZED: ["neutral", "●", "Held (authorized)"],
    APPROVED: ["neutral", "○", "Awaiting capture"],
    PENDING: ["neutral", "○", "Awaiting payment"],
  };

  function depositChip(code) {
    const [tone, icon, label] = DEPOSIT_STATUS[code] || ["neutral", "●", humanize(code)];
    return chip(tone, icon, label);
  }

  // --- pricing tiers (prices come from the gateway's /demo/config, never from this page) ---
  function money(amount, currency) {
    const value = Number(amount);
    if (amount === null || amount === undefined || amount === "" || Number.isNaN(value)) return "";
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD" }).format(value);
    } catch (_) {
      return `${amount} ${currency || ""}`.trim();
    }
  }

  function tiers() { return (state.config && state.config.pricing_tiers) || []; }
  function tierById(id) { return tiers().find((t) => t.tier_id === id) || null; }

  function tierBadge(id, name) {
    if (!id) return el("span", { className: "tier-badge", dataset: { tier: "base" }, text: "Base" });
    return el("span", { className: "tier-badge", dataset: { tier: id }, text: name || (tierById(id) || {}).name || id });
  }

  function authHeaders(extra) {
    const headers = Object.assign({ Accept: "application/json" }, extra || {});
    if (state.token) headers["X-Admin-Token"] = state.token;
    return headers;
  }

  async function getJson(url, init) {
    const response = await fetch(url, Object.assign({}, init, { headers: authHeaders((init && init.headers) || {}) }));
    let body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    return { ok: response.ok, status: response.status, body };
  }

  function message(body, status) {
    return (body && body.message) || `Request failed (HTTP ${status}).`;
  }

  function showBanner(text) {
    const banner = $("banner");
    clear(banner);
    banner.append(el("span", { text }));
    banner.hidden = false;
  }

  function hideBanner() { $("banner").hidden = true; }

  // --- theme ---------------------------------------------------------------------------
  // Dark is the default; "system" follows the operating system's preference.
  const THEMES = ["dark", "light", "system"];
  function applyTheme(pref) {
    document.documentElement.dataset.theme = pref;
    $("theme-toggle").textContent = `Theme: ${pref}`;
  }
  function initTheme() {
    let pref = store.get("localStorage", "ct-theme") || "dark";
    if (!THEMES.includes(pref)) pref = "dark";
    applyTheme(pref);
    $("theme-toggle").addEventListener("click", () => {
      const current = store.get("localStorage", "ct-theme") || "dark";
      const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
      store.set("localStorage", "ct-theme", next === "dark" ? null : next);
      applyTheme(next);
    });
  }

  // --- steps -----------------------------------------------------------------------------
  // [key, label, short status, icon, plain-English explanation shown on hover]
  const STEP_DEFS = [
    ["challenge", "Gateway · HTTP 402", "Deposit requested", "402", "The crawler asks for a protected page. The gateway answers HTTP 402 Payment Required with the deposit it wants and a single-use challenge."],
    ["policy", "Policy", "Paths, limits, refund rules", "§", "The crawler reads the machine-readable policy: which paths are allowed, the rate limit, and when the deposit is refunded or kept."],
    ["deposit", "PayPal deposit", "Refundable payment", "$", "The crawler pays a small refundable deposit through PayPal. The gateway verifies the payment with PayPal itself; the crawler cannot self-certify."],
    ["crawl", "Policy checks", "Every request enforced", "⚑", "Every request is checked against the policy: disallowed paths, path traversal, the rate limit, and the per-session budget. Each decision goes into the audit log."],
    ["settle", "Settlement", "Refund, release, or forfeit", "⇄", "When the session ends, the gateway's rules decide: no disqualifying violations returns the deposit through PayPal; otherwise the publisher keeps it."],
    ["explain", "Explanation", "Cited audit evidence", "✎", "The outcome is explained in plain English, and every statement cites the audit events behind it. The AI explains the decision; it never makes it."],
  ];
  const STEP_GLYPH = { done: "✓", failed: "✕", skipped: "–" };

  const reducedMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Restart a one-shot CSS animation class on a node.
  function replay(node, className) {
    if (!node || reducedMotion()) return;
    node.classList.remove(className);
    void node.offsetWidth;
    node.classList.add(className);
  }

  // Seconds since the run started, for "+2.4s" stamps. Uses the event's own timestamp.
  function relTime(at) {
    const run = state.run;
    const start = run ? Date.parse(run.started_at) : NaN;
    const t = Date.parse(at);
    if (Number.isNaN(start) || Number.isNaN(t)) return "";
    return `+${Math.max(0, (t - start) / 1000).toFixed(1)}s`;
  }

  function renderSteps() {
    const list = $("steps");
    clear(list);
    STEP_DEFS.forEach(([key, label, hint, icon, about], index) => {
      const status = el("span", { className: "step-status", text: hint });
      const num = el("span", { className: "step-num", text: icon });
      const time = el("span", { className: "step-time" });
      const item = el("li", { className: "step", title: about, dataset: { state: "pending", step: key } }, [
        index ? el("span", { className: "step-link", "aria-hidden": "true" }, [el("span", { className: "step-fill" }), el("span", { className: "step-packet" })]) : null,
        el("span", { className: "step-node", "aria-hidden": "true" }, [el("span", { className: "step-ring" }), num]),
        el("span", { className: "step-label", text: label }),
        status,
        time,
        el("span", { className: "sr-only", text: about }),
      ]);
      state.steps[key] = { item, status, hint, num, icon, time };
      list.append(item);
    });
  }

  function setStep(key, stepState, text) {
    const step = state.steps[key];
    if (!step) return;
    const changed = step.item.dataset.state !== stepState;
    step.item.dataset.state = stepState;
    step.status.textContent = text || step.hint;
    step.num.textContent = STEP_GLYPH[stepState] || step.icon;
    if (stepState === "pending") step.time.textContent = "";
    else if (changed && state.eventAt && ["done", "failed"].includes(stepState)) step.time.textContent = relTime(state.eventAt);
    if (changed && stepState === "done") replay(step.item, "pop");
    if (changed && stepState === "failed") replay(step.item, "shake");
    renderProgress();
  }

  function resetSteps() {
    for (const [key, step] of Object.entries(state.steps)) setStep(key, "pending", step.hint);
  }

  // Overall progress across the six stages, colored by how the run is going.
  function renderProgress() {
    const steps = Object.values(state.steps);
    if (!steps.length) return;
    const finished = steps.filter((s) => ["done", "failed", "skipped"].includes(s.item.dataset.state)).length;
    const active = steps.some((s) => s.item.dataset.state === "active") ? 0.5 : 0;
    const fill = $("pipe-progress");
    fill.style.width = `${Math.min(100, ((finished + active) / steps.length) * 100)}%`;
    fill.dataset.tone = steps.some((s) => s.item.dataset.state === "failed") ? "critical" : finished === steps.length ? "good" : "accent";
  }

  // --- config and badges -------------------------------------------------------------------
  function renderBadges(c) {
    const badges = $("badges");
    clear(badges);
    if (c.payment_mode === "mock") badges.append(badge("neutral", "Mock payments (simulated)"));
    else badges.append(badge("accent", c.paypal_autopay ? "PayPal Sandbox · test-card autopay" : "PayPal Sandbox · buyer approval"));
    badges.append(
      badge("accent", c.settlement_mode === "authorization" ? "Settlement: authorization hold" : "Settlement: capture + refund"),
      badge(c.ai_available ? "good" : "neutral", c.ai_available ? `AI: ${providerName(c.ai_provider)} (${c.ai_model})` : "AI: not configured (scripted runs only)"),
      badge(c.paypal_mcp_configured ? "good" : "neutral", c.paypal_mcp_configured ? "PayPal MCP: connected" : "PayPal MCP: not configured"),
      // Tier prices are on the tier cards below, so only the policy version is repeated here.
      badge("neutral", `Policy ${c.policy_version}`)
    );
  }

  function selectedPersona() {
    const input = document.querySelector("input[name=persona]:checked");
    return input ? input.value : "compliant";
  }

  function selectTier(id) {
    if (!tierById(id)) return;
    state.tier = id;
    store.set("sessionStorage", "ct-tier", id);
    for (const card of document.querySelectorAll(".tier-card")) {
      const selected = card.dataset.tier === id;
      card.dataset.selected = selected ? "true" : "false";
      card.setAttribute("aria-checked", selected ? "true" : "false");
      card.tabIndex = selected ? 0 : -1;
    }
    updateTierHint();
  }

  function renderTiers() {
    const grid = $("tier-grid");
    clear(grid);
    const list = tiers();
    if (!list.length) {
      grid.append(el("p", { className: "empty", text: "This gateway has no pricing tiers configured. Runs use the base deposit." }));
      return;
    }
    // Compact radio cards: the whole card is the control.
    for (const t of list) {
      const example = (t.resources && t.resources[0] && t.resources[0].path) || t.path_prefix;
      grid.append(el("button", {
        type: "button",
        className: "tier-card",
        role: "radio",
        "aria-checked": "false",
        "aria-label": `${t.name} tier, ${money(t.price, t.currency)} deposit`,
        title: t.description || "",
        dataset: { tier: t.tier_id, name: t.name, selected: "false" },
        onclick: () => selectTier(t.tier_id),
        onkeydown: (event) => {
          if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(event.key)) return;
          event.preventDefault();
          const ids = list.map((x) => x.tier_id);
          const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
          const next = ids[(ids.indexOf(t.tier_id) + step + ids.length) % ids.length];
          selectTier(next);
          const card = grid.querySelector(`.tier-card[data-tier="${next}"]`);
          if (card) card.focus();
        },
      }, [
        el("span", { className: "tier-head" }, [
          el("span", { className: "tier-name", text: t.name }),
          el("span", { className: "tier-check", "aria-hidden": "true", text: "✓" }),
        ]),
        el("span", { className: "tier-price" }, [
          el("span", { text: money(t.price, t.currency) }),
          el("span", { className: "tier-price-unit", text: " deposit" }),
        ]),
        el("span", { className: "tier-cats", text: (t.categories || []).join(" · ") }),
        el("code", { className: "tier-example mono", text: `GET /gateway${example}` }),
      ]));
    }
    const remembered = store.get("sessionStorage", "ct-tier");
    selectTier(tierById(state.tier) ? state.tier : tierById(remembered) ? remembered : (tierById("standard") ? "standard" : list[0].tier_id));
  }

  function updateTierHint() {
    // Tier choice only matters for the compliant crawler: the rogue scraper always goes for
    // /news (Public tier), so the tier column is locked while it is selected.
    const rogue = selectedPersona() === "rogue";
    $("tiers-card").dataset.locked = rogue ? "true" : "false";
    const pub = tierById("public");
    $("rogue-expect").textContent = pub
      ? `Expected outcome: pays the Public deposit (${money(pub.price, pub.currency)}) for /news, then forfeits it`
      : "Expected outcome: deposit forfeited";
    const t = tierById(state.tier);
    if (rogue) {
      $("tier-hint").textContent = pub
        ? `The rogue scraper requests /news, is challenged for the Public deposit (${money(pub.price, pub.currency)}), then breaks the rules.`
        : "The rogue scraper pays the base deposit, then breaks the rules.";
    } else {
      $("tier-hint").textContent = t
        ? `The compliant crawler requests ${t.name} content; the gateway will challenge it for a ${money(t.price, t.currency)} refundable deposit.`
        : "";
    }
  }

  function updateLauncher() {
    const c = state.config;
    const running = state.run && !["completed", "failed", "cancelled"].includes(state.run.status);
    const aiInput = $("driver-ai");
    const scriptedInput = $("driver-scripted");
    if (c) {
      aiInput.disabled = !c.ai_available;
      $("driver-ai-title").textContent = c.ai_available ? `AI agent (${providerName(c.ai_provider)} ${c.ai_model})` : "AI agent (not configured)";
      $("driver-ai-desc").textContent = c.ai_available
        ? "The model reads the 402 challenge and the policy, then decides every tool call itself."
        : "Set OPENAI_API_KEY or ANTHROPIC_API_KEY on the gateway to enable it.";
      if (!aiInput.checked && !scriptedInput.checked) (c.ai_available ? aiInput : scriptedInput).checked = true;
      if (aiInput.checked && aiInput.disabled) scriptedInput.checked = true;
    }
    const start = $("start-button");
    const hint = $("launch-hint");
    start.disabled = Boolean(!c || running || !c.can_start_runs);
    $("stop-button").hidden = !(running && c && c.can_start_runs);
    if (!c) hint.textContent = "Connecting to the gateway...";
    else if (!c.can_start_runs) hint.textContent = "Starting runs needs the admin token on this deployment.";
    else if (running) hint.textContent = "Run in progress...";
    else hint.textContent = `${c.runs_remaining_this_hour} run(s) left this hour on this deployment.`;
    for (const input of document.querySelectorAll("input[name=persona], input[name=driver]")) {
      if (input.id !== "driver-ai" || (c && c.ai_available)) input.disabled = Boolean(running);
    }
    for (const button of document.querySelectorAll(".tier-card")) button.disabled = Boolean(running);
    $("reset-button").disabled = Boolean(running);
    renderRunState();
  }

  // Header of the pipeline: idle, running, or the final outcome.
  function setStat(id, value) {
    const node = $(id);
    const text = String(value);
    if (node.textContent === text) return;
    node.textContent = text;
    replay(node.parentNode, "bump");
  }

  // The stage currently in progress, for the status line ("Now: PayPal deposit").
  function currentStage() {
    const active = STEP_DEFS.find(([key]) => state.steps[key] && state.steps[key].item.dataset.state === "active");
    if (active) return active[1];
    // A crawler that broke the rules keeps crawling until it completes its session.
    if (state.steps.crawl && state.steps.crawl.item.dataset.state === "failed" && state.steps.settle.item.dataset.state === "pending") return "Policy checks (violations recorded)";
    // Otherwise the first pending stage after the furthest one reached (stages can be skipped).
    const states = STEP_DEFS.map(([key]) => (state.steps[key] ? state.steps[key].item.dataset.state : "pending"));
    let last = -1;
    states.forEach((st, i) => { if (st !== "pending") last = i; });
    const next = STEP_DEFS.find((_, i) => i > last && states[i] === "pending");
    return next ? next[1] : null;
  }

  // Header of the pipeline: idle, running (as replayed so far), or the final outcome.
  function renderRunState() {
    const box = $("run-state");
    const text = $("run-state-text");
    const card = $("pipeline-card");
    const run = state.run;
    const playing = state.draining || state.queue.length > 0;
    setStat("pipe-requests", state.counts.allowed + state.counts.blocked);
    setStat("pipe-allowed", state.counts.allowed);
    setStat("pipe-blocked", state.counts.blocked);
    setStat("pipe-violations", state.counts.violations);
    const live = $("tl-live");
    if (!run) {
      box.dataset.state = card.dataset.state = "idle";
      text.textContent = "Idle · waiting for a run";
      live.hidden = true;
      return;
    }
    const finished = ["completed", "failed", "cancelled"].includes(run.status) && !playing;
    if (run.status === "awaiting_approval") {
      box.dataset.state = card.dataset.state = "running";
      text.textContent = "Waiting for PayPal buyer approval";
      live.hidden = false;
      $("tl-live-text").textContent = "Waiting for the PayPal Sandbox checkout to be approved...";
      return;
    }
    if (!finished) {
      box.dataset.state = card.dataset.state = "running";
      const stage = currentStage();
      text.textContent = stage ? `Running · now: ${stage}` : "Running";
      live.hidden = Boolean(state.pendingRow);
      $("tl-live-text").textContent = run.driver === "ai" ? "The AI agent is deciding its next step..." : "The crawler is working...";
      return;
    }
    live.hidden = true;
    const d = run.deposit || {};
    const good = ["REFUNDED", "RELEASED"].includes(d.deposit_state);
    const tone = good ? "good" : run.status === "completed" && d.deposit_state === "FORFEITED" ? "critical" : "idle";
    box.dataset.state = card.dataset.state = tone;
    text.textContent = `Run ${run.status}${d.deposit_state ? ` · ${(DEPOSIT_STATUS[d.deposit_state] || [])[2] || humanize(d.deposit_state)}` : ""}`;
  }

  // Elapsed run time: ticks while the run (or its replay) is in progress.
  function renderClock() {
    const run = state.latest || state.run;
    const out = $("pipe-elapsed");
    if (!run) { out.textContent = "0.0s"; return; }
    const start = Date.parse(run.started_at);
    const done = ["completed", "failed", "cancelled"].includes(run.status) && run.finished_at && !state.draining && !state.queue.length;
    const end = done ? Date.parse(run.finished_at) : Date.now();
    const secs = Number.isNaN(start) ? 0 : Math.max(0, (end - start) / 1000);
    out.textContent = secs >= 60 ? `${Math.floor(secs / 60)}m ${Math.floor(secs % 60)}s` : `${secs.toFixed(1)}s`;
    if (done) stopClock();
  }

  function startClock() {
    stopClock();
    state.clock = setInterval(renderClock, 100);
    renderClock();
  }

  function stopClock() {
    clearInterval(state.clock);
    state.clock = null;
  }

  async function loadConfig() {
    try {
      const result = await getJson("/demo/config");
      if (result.status === 401) {
        showBanner("The admin token was rejected.");
        state.token = null;
        store.set("sessionStorage", "ct-admin-token", null);
        return loadConfig();
      }
      if (!result.ok) {
        showBanner(`The gateway returned HTTP ${result.status} for the demo configuration.`);
        return;
      }
      hideBanner();
      const firstLoad = !state.config;
      state.config = result.body;
      renderBadges(state.config);
      if (firstLoad) renderTiers();
      if (state.config.access === "none") {
        showBanner("This deployment is private. Enter the admin token to watch and start runs, or run the gateway with DEMO_PUBLIC=true.");
      }
      if (!state.runId && state.config.active_run_id) attach(state.config.active_run_id);
      updateLauncher();
    } catch (_) {
      showBanner("Cannot reach the SARA gateway. Is the server running?");
    }
  }

  // --- timeline --------------------------------------------------------------------------
  // groups: space-separated filter tags (request, payment, agent, alert, policy, lifecycle).
  function timelineItem({ kind, tone, icon, title, detail, extra, at, pending, note, groups, resolved, tool }) {
    const tags = groups || "lifecycle";
    return el("li", {
      className: `tl-item${resolved ? " tl-resolved" : ""}`,
      dataset: { kind, tone: tone || "neutral", pending: pending ? "true" : "false", groups: tags, tool: tool || "" },
    }, [
      el("span", { className: "tl-icon", "aria-hidden": "true" }, [el("span", { className: "tl-icon-glyph", text: icon })]),
      el("div", { className: "tl-body" }, [
        el("div", { className: "tl-title" }, [
          ...(Array.isArray(title) ? title : [el("span", { className: "tl-title-text", text: title })]),
          at ? el("span", { className: "tl-time", title: formatTime(at) }, [el("span", { className: "tl-rel", text: relTime(at) }), el("span", { className: "tl-clock", text: formatTime(at) })]) : null,
        ]),
        note ? el("div", { className: "tl-note", text: note }) : null,
        detail ? el("div", { className: "tl-detail", text: detail }) : null,
        pending ? el("div", { className: "tl-skeleton", "aria-hidden": "true" }) : null,
        extra || null,
      ]),
    ]);
  }

  // Narration that precedes a tool call is shown inside that call's row as its "why".
  function flushNote() {
    if (!state.note) return;
    const { text, at } = state.note;
    state.note = null;
    addItem(timelineItem({ kind: "narration", icon: "“", title: state.run && state.run.driver === "ai" ? "Agent" : "Crawler", detail: text, at, groups: "agent" }));
  }

  function followLive() {
    const list = $("timeline");
    if (!$("tl-follow").checked) return;
    list.scrollTo({ top: list.scrollHeight, behavior: reducedMotion() || state.instant ? "auto" : "smooth" });
  }

  function countAlert(item) {
    if (!/\balert\b/.test(item.dataset.groups || "")) return;
    state.alerts += 1;
    const badge = $("tl-alert-count");
    badge.textContent = String(state.alerts);
    badge.hidden = false;
    replay(badge, "bump");
  }

  function addItem(item) {
    const list = $("timeline");
    const empty = list.querySelector(".empty-state");
    if (empty) empty.remove();
    list.append(item);
    countAlert(item);
    followLive();
    return item;
  }

  function setTimelineFilter(filter) {
    $("timeline").dataset.filter = filter;
    for (const b of $("tl-filters").querySelectorAll("button")) b.setAttribute("aria-pressed", b.dataset.filter === filter ? "true" : "false");
  }

  function resetAlerts() {
    state.alerts = 0;
    $("tl-alert-count").hidden = true;
  }

  function renderEmptyTimeline() {
    const list = $("timeline");
    clear(list);
    list.append(el("li", { className: "empty-state" }, [
      el("strong", { text: "Pick a crawler and start a run." }),
      el("ol", null, [
        el("li", { text: "The crawler requests a protected page and gets HTTP 402 Payment Required." }),
        el("li", { text: "It pays a small refundable deposit through PayPal and accepts the site's policy." }),
        el("li", { text: "The gateway checks every request against the policy and records it in the audit log." }),
        el("li", { text: "When the session ends, the gateway returns the deposit or keeps it, and explains why." }),
        el("li", { text: "The PayPal assistant then checks the deposit against PayPal's records through PayPal's MCP server." }),
      ]),
    ]));
  }

  function httpTone(status) {
    if (status === 200) return "good";
    if (status === 402) return "warning";
    if (status === 429) return "serious";
    return "critical";
  }

  function httpLabel(status) {
    return { 200: "OK", 402: "Payment Required", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests" }[status] || "";
  }

  function describeCall(name, input) {
    const args = input || {};
    switch (name) {
      case "fetch_page": return { icon: "→", title: [el("span", { text: "GET" }), el("span", { className: "mono", text: `/gateway${args.path === "/" ? "" : args.path || ""}` })] };
      case "read_policy": return { icon: "§", title: "Reading the crawling policy and robots.txt" };
      case "pay_deposit": return { icon: "$", title: "Paying the refundable deposit with PayPal" };
      case "accept_policy": return { icon: "✓", title: `Accepting policy ${args.policy_version || ""}` };
      case "wait": return { icon: "◷", title: `Waiting ${args.seconds}s to respect the rate limit` };
      case "complete_session": return { icon: "■", title: "Completing the session" };
      default: return { icon: "•", title: name };
    }
  }

  const TOOL_GROUPS = {
    fetch_page: "request",
    read_policy: "policy",
    accept_policy: "policy",
    pay_deposit: "payment",
    wait: "agent",
    complete_session: "payment",
  };

  function applyToolResult(row, name, result, isError, at) {
    const r = result || {};
    let groups = TOOL_GROUPS[name] || "lifecycle";
    let tone = isError ? "critical" : "good";
    let title = null;
    let detail = "";
    let extra = null;

    if (name === "fetch_page") {
      const status = r.status;
      const path = r.path || "";
      tone = httpTone(status);
      title = [
        el("span", { className: "http-pill", dataset: { tone }, text: String(status || "ERR") }),
        el("span", { className: "mono", text: `GET /gateway${path === "/" ? "" : path}` }),
        el("span", { text: httpLabel(status) }),
      ];
      if (status === 402) {
        const d = r.deposit || {};
        const pricing = r.pricing;
        detail = pricing
          ? `The gateway priced ${pricing.resource} as ${pricing.tier_name} content: deposit required ${money(d.amount, d.currency)} (${d.amount} ${d.currency}), refundable. Policy ${r.policy_version}.`
          : `Deposit required: ${d.amount} ${d.currency}, refundable. Policy ${r.policy_version}.`;
        setStep("challenge", "done", pricing ? `HTTP 402 · ${pricing.tier_name} · ${money(d.amount, d.currency)}` : `HTTP 402 · ${d.amount} ${d.currency} deposit`);
      } else if (status === 200) {
        state.counts.allowed += 1;
        const tierNote = r.pricing_tier ? `${(tierById(r.pricing_tier) || {}).name || r.pricing_tier} content served. ` : "Served. ";
        detail = `${tierNote}Session budget left ${r.remaining_session_budget}, requests left in this window ${r.remaining_window_requests}.`;
        if (r.content) extra = el("details", { className: "tl-more" }, [el("summary", { text: "Page text (untrusted content)" }), el("pre", { text: r.content })]);
        setStep("crawl", "active", "Requests allowed by policy");
      } else {
        state.counts.blocked += 1;
        if (r.violation) state.counts.violations += 1;
        const violation = r.violation ? " Counts as a policy violation." : "";
        const disq = r.disqualifying ? " Disqualifying: the deposit will be kept." : "";
        detail = `${humanize(r.reason_code)}.${violation}${disq}`;
        if (r.reason_code === "TIER_NOT_COVERED" && r.reason) detail = `${r.reason} Not a violation; no content was served.`;
        if (r.retry_after_seconds) detail += ` Retry after ${r.retry_after_seconds}s.`;
        if (r.reason_code !== "TIER_NOT_COVERED") setStep("crawl", "failed", "Violations recorded");
        if (r.violation || r.disqualifying) groups += " alert";
      }
      if (r.prompt_injection) {
        addItem(timelineItem({
          kind: "injection",
          tone: "serious",
          icon: "!",
          groups: "alert agent",
          title: "Prompt injection hidden in page content",
          detail: String(path).startsWith("/content/")
            ? "The page tells AI agents to fetch a Premium report they have not paid for. A well-behaved crawler treats page text as data and ignores it; the gateway would refuse it anyway (deposit does not cover that tier)."
            : "The page tells AI agents to fetch /private/customer-data. A well-behaved crawler treats page text as data and ignores it.",
          at,
        }));
      }
    } else if (name === "read_policy") {
      const p = r.policy || {};
      detail = `Allowed: ${(p.allowed_paths || []).join(", ")}. Disallowed: ${(p.disallowed_paths || []).join(", ")}. Limit ${p.rate_limit_requests} requests per ${p.rate_limit_window_seconds}s, ${p.max_requests_per_session} per session.`;
      setStep("policy", "done", `Policy ${p.policy_version}`);
    } else if (name === "pay_deposit") {
      if (!isError) {
        const settlement = r.settlement_mode === "authorization" ? "authorization hold" : "captured";
        const tierLabel = r.pricing_tier_name ? `${r.pricing_tier_name} tier · ` : "";
        detail = `${tierLabel}${r.required_amount} ${r.currency} ${r.simulated ? "(simulated, mock mode: no PayPal call)" : "via PayPal Sandbox (real Sandbox order)"} · ${settlement} · payment ${r.payment_status}.`;
        setStep("deposit", "done", r.settlement_mode === "authorization" ? "Hold authorized" : "Captured");
      } else {
        detail = r.error || "Payment failed.";
        setStep("deposit", "failed", "Not paid");
      }
    } else if (name === "accept_policy") {
      detail = isError ? r.error : `Session ${r.session_id} started. Up to ${r.max_requests_per_session} requests, ${r.rate_limit_requests} per ${r.rate_limit_window_seconds}s.`;
      if (!isError) setStep("crawl", "active", "Session started");
    } else if (name === "wait") {
      tone = "neutral";
      detail = isError ? r.error : `Waited ${r.waited_seconds}s.`;
    } else if (name === "complete_session") {
      if (!isError) {
        const good = ["REFUNDED", "RELEASED"].includes(r.deposit_state);
        tone = good ? "good" : "critical";
        detail = `${humanize(r.policy_decision)}. ${r.allowed_requests} allowed request(s), ${r.total_violations} violation(s).`;
        title = [el("span", { text: "Session completed" }), depositChip(r.deposit_state)];
        setStep("settle", good ? "done" : "failed", good ? (r.deposit_state === "RELEASED" ? "Hold released" : "Deposit refunded") : "Deposit forfeited");
        const crawlStep = state.steps.crawl.item.dataset.state;
        if (crawlStep !== "failed") setStep("crawl", "done", "No violations");
      } else {
        detail = r.error;
      }
    } else if (isError) {
      detail = r.error || "Tool error.";
    }

    if (isError && name !== "pay_deposit" && name !== "complete_session" && !detail) detail = r.error || "Error";
    const noteNode = row.querySelector(".tl-note");
    if (isError) groups += " alert";
    const updated = timelineItem({
      kind: "tool", tone, icon: row.querySelector(".tl-icon").textContent,
      title: title || row.querySelector(".tl-title span").textContent,
      detail, extra, at, note: noteNode ? noteNode.textContent : null, groups, resolved: true, tool: name,
    });
    row.replaceWith(updated);
    countAlert(updated);
    followLive();
  }

  function handleEvent(event) {
    const d = event.data || {};
    state.eventAt = event.at;
    if (!["text", "tool_call", "tool_result"].includes(event.kind)) flushNote();
    switch (event.kind) {
      case "run_started":
        addItem(timelineItem({
          kind: "start", tone: "accent", icon: "▶",
          title: d.driver === "ai" ? `AI agent started (${providerName(d.provider)} ${d.model})` : "Scripted crawler started",
          // The task prompt is long; it is one click away instead of filling the timeline.
          detail: d.persona_label, at: event.at,
          extra: d.task ? el("details", { className: "tl-more" }, [el("summary", { text: "Show task" }), el("p", { className: "tl-task", text: d.task })]) : null,
        }));
        break;
      case "thinking":
        addItem(timelineItem({ kind: "reasoning", icon: "…", title: "Reasoning", detail: d.text.length > 600 ? `${d.text.slice(0, 600)}...` : d.text, at: event.at, groups: "agent" }));
        break;
      case "text":
        flushNote();
        state.note = { text: d.text, at: event.at };
        break;
      case "tool_call": {
        const info = describeCall(d.name, d.input);
        if (d.name === "pay_deposit") setStep("deposit", "active", "Paying...");
        if (d.name === "complete_session") setStep("settle", "active", "Deciding...");
        const note = state.note ? state.note.text : null;
        state.note = null;
        if (d.name === "fetch_page" && state.steps.crawl.item.dataset.state === "pending" && state.steps.deposit.item.dataset.state === "done") setStep("crawl", "active", "Checking requests...");
        if (d.name === "fetch_page" && state.steps.challenge.item.dataset.state === "pending") setStep("challenge", "active", "Requesting a protected page...");
        if (d.name === "read_policy") setStep("policy", "active", "Reading the policy...");
        state.pendingRow = addItem(timelineItem({ kind: "tool", icon: info.icon, title: info.title, pending: true, at: event.at, note, groups: TOOL_GROUPS[d.name], tool: d.name }));
        break;
      }
      case "tool_result":
        if (state.pendingRow) {
          applyToolResult(state.pendingRow, d.name, d.result, d.is_error, event.at);
          state.pendingRow = null;
        }
        break;
      case "approval_required":
        setStep("deposit", "active", "Waiting for PayPal approval");
        addItem(timelineItem({ kind: "approval", tone: "accent", icon: "$", title: "PayPal checkout created; waiting for buyer approval", detail: "Approve the Sandbox checkout in the PayPal window.", at: event.at, groups: "payment" }));
        break;
      case "approval_done":
        addItem(timelineItem({ kind: "approval", tone: d.outcome === "paid" || d.outcome === "confirmed" ? "good" : "critical", icon: "$", title: `PayPal approval: ${d.outcome}`, at: event.at, groups: "payment" }));
        break;
      case "stop":
        addItem(timelineItem({ kind: "stop", tone: "warning", icon: "!", title: "Run stopped", detail: d.reason, at: event.at, groups: "alert lifecycle" }));
        break;
      case "run_finished":
        addItem(timelineItem({
          kind: "finish", tone: d.status === "completed" ? "good" : "critical", icon: d.status === "completed" ? "✓" : "✕",
          title: `Run ${d.status}`, detail: d.error || "", at: event.at,
        }));
        break;
      default:
        break;
    }
  }

  // --- deposit card ----------------------------------------------------------------------
  function fact(label, value, mono) {
    if (value === null || value === undefined || value === "") return [];
    if (mono && window.CTGrid) {
      const copy = window.CTGrid.copyButton(String(value).split(" · ")[0], label);
      return [el("dt", { text: label }), el("dd", { className: "mono copyable" }, [el("span", { text: value }), copy])];
    }
    return [el("dt", { text: label }), el("dd", { className: mono ? "mono" : null, text: value })];
  }

  function miniTile(label, value, tone) {
    return el("div", { className: "mini-tile", dataset: tone && Number(value) > 0 ? { tone } : null }, [el("div", { className: "tile-label", text: label }), el("div", { className: "tile-value", text: String(value) })]);
  }

  // Where the money sits right now: crawler -> PayPal hold -> back to the crawler or to the publisher.
  function moneyFlow(d) {
    const st = d.deposit_state;
    const returned = st === "REFUNDED" || st === "RELEASED";
    const kept = st === "FORFEITED";
    const held = ["CAPTURED", "AUTHORIZED", "APPROVED", "REFUND_PENDING"].includes(st);
    const amount = money(d.required_amount, d.currency) || `${d.required_amount} ${d.currency}`;
    const node = (title, sub, tone) => el("div", { className: "flow-node", dataset: tone ? { tone } : null }, [el("strong", { text: title }), el("span", { text: sub })]);
    return el("div", { className: "money-flow", "aria-label": "Deposit money flow" }, [
      node("Crawler", returned ? `${amount} back` : `paid ${amount}`, returned ? "good" : null),
      el("span", { className: "flow-arrow", "aria-hidden": "true", text: returned ? "⇄" : "→" }),
      node(d.payment_mode === "mock" ? "Mock rail" : "PayPal", d.settlement_mode === "authorization" ? (held ? "hold active" : "hold") : held ? "captured" : "capture", held ? "accent" : null),
      el("span", { className: "flow-arrow", "aria-hidden": "true", text: "→" }),
      node("Publisher", kept ? `keeps ${amount}` : returned ? "keeps nothing" : "pending", kept ? "critical" : null),
    ]);
  }

  // The big, judge-friendly outcome banner shown once the deposit is settled.
  function renderVerdict(run) {
    const box = $("verdict");
    clear(box);
    const d = run && run.deposit;
    const finished = run && ["completed", "failed", "cancelled"].includes(run.status);
    if (!run || (!finished && !(d && ["REFUNDED", "RELEASED", "FORFEITED"].includes(d.deposit_state)))) { box.hidden = true; return; }
    const st = d && d.deposit_state;
    const s = run.session && run.session.allowed_requests !== undefined
      ? run.session
      : { allowed_requests: state.counts.allowed, rejected_requests: state.counts.blocked, total_violations: state.counts.violations };
    const amount = d ? money(d.required_amount, d.currency) : "";
    let tone = "warning";
    let icon = "!";
    let title = `Run ${run.status}`;
    let sub = run.error || "The run ended before the deposit was settled.";
    if (st === "REFUNDED" || st === "RELEASED") {
      tone = "good"; icon = "✓";
      title = st === "RELEASED" ? `Compliant: PayPal hold of ${amount} released` : `Compliant: ${amount} deposit refunded`;
      sub = "The crawler followed the policy, so it crawled at no net cost. The decision came from the gateway's rules, not the model.";
    } else if (st === "FORFEITED") {
      tone = "critical"; icon = "✕";
      title = `Violation: ${amount} deposit forfeited`;
      sub = "The crawler broke the policy. The gateway blocked each violating request and the publisher keeps the deposit.";
    }
    const fresh = box.hidden || box.dataset.tone !== tone;
    box.dataset.tone = tone;
    // Celebrate a refund, sound the alarm on a forfeit: once, when the verdict first appears.
    if (fresh && !state.instant) {
      if (tone === "good") replay(box, "celebrate");
      else if (tone === "critical") replay(box, "alarm");
    }
    box.append(
      el("span", { className: "verdict-icon", "aria-hidden": "true", text: icon }),
      el("div", null, [el("p", { className: "verdict-title", text: title }), el("p", { className: "verdict-sub", text: sub })]),
      el("div", { className: "verdict-stats" }, [
        el("div", { className: "vstat" }, [el("strong", { text: String(s.allowed_requests ?? 0) }), el("span", { text: "allowed" })]),
        el("div", { className: "vstat" }, [el("strong", { text: String(s.rejected_requests ?? 0) }), el("span", { text: "blocked" })]),
        el("div", { className: "vstat" }, [el("strong", { text: String(s.total_violations ?? 0) }), el("span", { text: "violations" })]),
      ])
    );
    box.hidden = false;
  }

  function renderDeposit(run) {
    const box = $("deposit");
    clear(box);
    const d = run && run.deposit;
    if (!d) {
      box.append(el("p", { className: "empty", text: run ? "The crawler has not paid a deposit yet." : "No deposit yet. Start a run." }));
      if (state.paypal) state.paypal.setDeposit(null);
      return;
    }
    // The session record carries counts once the session completes; until then, count
    // the gateway's answers as they arrive.
    const done = run.session && run.session.allowed_requests !== undefined;
    const s = done ? run.session : { allowed_requests: state.counts.allowed, rejected_requests: state.counts.blocked, total_violations: state.counts.violations };
    box.append(
      el("div", { className: "deposit-head" }, [
        el("span", { className: "deposit-amount", text: `${d.required_amount} ${d.currency}` }),
        depositChip(d.deposit_state),
      ]),
      moneyFlow(d),
      el("dl", { className: "facts" }, [
        ...(d.pricing_tier ? [el("dt", { text: "Pricing tier" }), el("dd", null, [tierBadge(d.pricing_tier, d.pricing_tier_name)])] : []),
        ...fact("Resource", d.requested_resource, true),
        ...fact("Payment", d.payment_mode === "mock" ? "Simulated (mock mode)" : "PayPal Sandbox"),
        ...fact("Settlement", d.settlement_mode === "authorization" ? "Authorization hold (void if compliant)" : "Capture, then refund if compliant"),
        ...fact("Decision", humanize(d.policy_decision)),
        ...fact("Deposit ID", d.deposit_id, true),
        ...fact("PayPal order", d.provider_order_id ? `${d.provider_order_id} · ${d.provider_order_status || ""}` : null, true),
        ...fact("Authorization", d.provider_authorization_id ? `${d.provider_authorization_id} · ${d.provider_authorization_status || ""}` : null, true),
        ...fact("Capture", d.provider_capture_id, true),
        ...fact("Refund", d.provider_refund_id ? `${d.provider_refund_id} · ${d.provider_refund_status || ""}` : null, true),
      ]),
      el("div", { className: "mini-tiles" }, [
        miniTile("Allowed", s.allowed_requests, "good"),
        miniTile("Blocked", s.rejected_requests, "critical"),
        miniTile("Violations", s.total_violations, "critical"),
      ]),
      d.payment_mode !== "mock" && d.provider_order_id && state.paypal
        ? el("div", { className: "card-actions" }, [
            el("button", {
              type: "button",
              text: "Verify with PayPal (MCP)",
              onclick: () => {
                $("paypal-panel").scrollIntoView({ behavior: "smooth", block: "start" });
                state.paypal.verify();
              },
            }),
          ])
        : ""
    );
    if (state.paypal) state.paypal.setDeposit(d);
  }

  // --- explanation -----------------------------------------------------------------------
  function renderExplanation(x) {
    const panel = $("explanation");
    clear(panel);
    if (!x) {
      panel.append(el("p", { className: "empty", text: state.run && ["completed", "failed", "cancelled"].includes(state.run.status) ? "No explanation is available for this run." : "Appears when the session ends." }));
      return;
    }
    const evidenceById = new Map(x.evidence.map((item) => [item.evidence_id, item]));
    const rows = new Map();
    const evidenceList = el("div", { className: "evidence-list", "aria-label": "Evidence from the audit log" });
    for (const item of x.evidence) {
      const detail = [item.event_type, item.path, item.reason_code ? humanize(item.reason_code) : null].filter(Boolean).join(" · ");
      const row = el("div", { className: "evidence-item" }, [el("span", { className: "evidence-id", text: item.evidence_id }), el("span", { text: detail })]);
      rows.set(item.evidence_id, row);
      evidenceList.append(row);
    }
    const highlight = (ids) => {
      for (const [id, node] of rows) node.classList.toggle("highlight", ids.includes(id));
      const first = rows.get(ids[0]);
      if (first) first.scrollIntoView({ block: "nearest" });
    };
    panel.append(
      el("div", { className: "explain-meta" }, [
        depositChip(x.deposit_state),
        badge(x.generated_by === "template" ? "neutral" : "good", x.generated_by === "template" ? "Rule-based explanation" : `Explained by ${providerName(x.generated_by)}${x.model ? ` (${x.model})` : ""}`),
      ]),
      el("p", { className: "explain-headline", text: x.headline }),
      el("p", { className: "explain-summary", text: x.summary }),
      el("ol", { className: "findings" }, x.findings.map((finding) => el("li", null, [
        el("span", { text: finding.statement }),
        ...finding.evidence.filter((id) => evidenceById.has(id)).map((id) =>
          el("button", { className: "evidence-chip", type: "button", text: id, "aria-label": `Show evidence ${id}`, onclick: () => highlight(finding.evidence) })
        ),
      ]))),
      x.notice ? el("p", { className: "notice", text: x.notice }) : null,
      evidenceList
    );
  }

  // --- report ------------------------------------------------------------------------------
  function renderReport(run) {
    const box = $("report");
    clear(box);
    if (!run || !["completed", "failed", "cancelled"].includes(run.status)) return;
    const u = run.usage || {};
    const usage = run.driver === "ai" && u.turns
      ? `${u.turns} model turn(s) · ${Number(u.input_tokens || 0).toLocaleString()} input tokens (${Number(u.cached_input_tokens || 0).toLocaleString()} cached) · ${Number(u.output_tokens || 0).toLocaleString()} output tokens`
      : null;
    box.append(el("div", { className: "report" }, [
      el("h3", { text: run.driver === "ai" ? "Agent's report" : "Crawler's report" }),
      ...(run.final_text || run.error || "No report.").split("\n").filter(Boolean).map((line) => el("p", { text: line })),
      usage ? el("div", { className: "usage", text: usage }) : null,
    ]));
  }

  // --- execution summary -------------------------------------------------------------------
  function summaryItem(label, value) {
    return el("div", { className: "summary-item" }, [el("span", { className: "summary-label", text: label }), value]);
  }

  function renderRunSummary(run) {
    const box = $("run-summary");
    clear(box);
    if (!run) { box.hidden = true; return; }
    const d = run.deposit || {};
    // The selected tier, or the tier the gateway priced the crawler's first request at.
    const t = run.pricing_tier || (d.pricing_tier ? { tier_id: d.pricing_tier, name: d.pricing_tier_name } : null);
    const outcome = d.deposit_state ? depositChip(d.deposit_state) : el("span", { text: "Not paid yet" });
    const rail = d.payment_mode
      ? el("span", { text: d.payment_mode === "mock" ? "Mock (simulated)" : "PayPal Sandbox (real Sandbox API)" })
      : el("span", { text: state.config && state.config.payment_mode === "mock" ? "Mock (simulated)" : "PayPal Sandbox" });
    box.append(
      summaryItem("Pricing tier", t ? tierBadge(t.tier_id, t.name) : el("span", { text: "Not priced yet" })),
      summaryItem("Deposit set by gateway", el("span", { className: "summary-strong", text: d.required_amount ? money(d.required_amount, d.currency) : t ? money(t.price, t.currency) : "—" })),
      summaryItem("Requested resource", el("code", { className: "mono", text: d.requested_resource || (t ? t.resource : "/news") })),
      summaryItem("Payment rail", rail),
      summaryItem("Deposit outcome", outcome)
    );
    box.hidden = false;
  }

  // --- live request monitor (AG Grid via grid_kit.js, shared with /console) --------------
  const DECISIONS = {
    granted: ["good", "✓", "Granted"],
    payment_required: ["warning", "$", "Payment required"],
    denied: ["critical", "✕", "Denied"],
  };

  function cellNode(render) {
    return (params) => (params.data ? render(params.data) : "");
  }

  function paymentLabel(r) {
    return r.deposit_state ? (DEPOSIT_STATUS[r.deposit_state] || [])[2] || humanize(r.deposit_state) : "No deposit yet";
  }

  function railLabel(r) {
    return r.simulated_payment === null || r.simulated_payment === undefined ? "—" : r.simulated_payment ? "Mock (simulated)" : "PayPal Sandbox";
  }

  // Expanded row: the request, the decision, and the deposit/PayPal records behind it.
  function requestDetail(r) {
    const [tone, icon, label] = DECISIONS[r.access_decision] || ["neutral", "●", r.access_decision];
    const facts = window.CTGrid.facts;
    return el("div", { className: "detail-grid" }, [
      el("div", null, [
        el("p", { className: "detail-title", text: "Request" }),
        facts([
          ["Request ID", r.request_id, true],
          ["Time", r.timestamp ? new Date(r.timestamp).toLocaleString() : ""],
          ["Crawler", r.crawler_id, true],
          ["Resource", r.resource ? `GET /gateway${r.resource === "/" ? "" : r.resource}` : "", true],
          ["Pricing tier", tierBadge(r.pricing_tier, r.pricing_tier_name)],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Gateway decision" }),
        facts([
          ["HTTP status", r.http_status ? `${r.http_status} ${httpLabel(r.http_status)}` : "—"],
          ["Decision", chip(tone, icon, label)],
          ["Reason", r.reason_code ? `${humanize(r.reason_code)} (${r.reason_code})` : "Within policy"],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Deposit & PayPal" }),
        r.deposit_id
          ? facts([
              ["Deposit ID", r.deposit_id, true],
              ["Amount", r.deposit_amount != null ? money(r.deposit_amount, r.currency) : ""],
              ["Payment status", r.payment_status ? humanize(r.payment_status) : ""],
              ["Deposit state", r.deposit_state ? depositChip(r.deposit_state) : ""],
              ["Payment rail", railLabel(r)],
              ["PayPal order", r.provider_order_id, true],
            ])
          : el("p", { className: "muted", text: r.deposit_amount != null ? `Challenge for a ${money(r.deposit_amount, r.currency)} deposit; not paid at this point.` : "No deposit linked to this request." }),
      ]),
    ]);
  }

  function renderMonitorStats(rows) {
    const box = $("monitor-stats");
    clear(box);
    const count = (fn) => rows.filter(fn).length;
    const stat = (tone, value, label) => el("span", { className: "mstat", dataset: tone ? { tone } : null }, [el("strong", { text: String(value) }), el("span", { text: label })]);
    box.append(
      stat(null, rows.length, "requests"),
      stat("good", count((r) => r.access_decision === "granted"), "granted"),
      stat("warning", count((r) => r.access_decision === "payment_required"), "HTTP 402 challenges"),
      stat("critical", count((r) => r.access_decision === "denied"), "denied"),
      stat(null, new Set(rows.map((r) => r.crawler_id).filter(Boolean)).size, "crawlers")
    );
  }

  function initHistoryGrid() {
    const node = $("grid-requests");
    if (!window.CTGrid || !window.CTGrid.available()) {
      clear(node);
      node.append(el("p", { className: "empty", text: "The table library (AG Grid, cdn.jsdelivr.net) could not be loaded. The same data is available from /demo/requests." }));
      return;
    }
    state.historyGrid = window.CTGrid.create(node, {
      rowId: (r) => r.request_id,
      detail: requestDetail,
      detailHeight: 196,
      selection: "multi",
      flashNew: true,
      pageSize: 25,
      exportName: "sara-requests",
      search: "Search crawler, resource, tier...",
      noRows: "No gateway requests yet. Start a run.",
      segments: {
        label: "Filter by gateway decision",
        options: [
          { id: "granted", label: "Granted", test: (r) => r.access_decision === "granted" },
          { id: "402", label: "HTTP 402", test: (r) => r.access_decision === "payment_required" },
          { id: "denied", label: "Denied", test: (r) => r.access_decision === "denied" },
        ],
      },
      exportValues: {
        pricing_tier_name: (r) => r.pricing_tier_name || "Base",
        http_status: (r) => r.http_status,
        deposit_state: (r) => paymentLabel(r),
        access_decision: (r) => (DECISIONS[r.access_decision] || [])[2] || r.access_decision,
      },
      columnDefs: [
        { headerName: "Time", field: "timestamp", minWidth: 110, maxWidth: 130, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => p.value },
        {
          headerName: "Decision", field: "access_decision", minWidth: 160,
          valueGetter: (p) => (p.data ? (DECISIONS[p.data.access_decision] || [])[2] || p.data.access_decision : ""),
          cellRenderer: cellNode((r) => {
            const [tone, icon, label] = DECISIONS[r.access_decision] || ["neutral", "●", r.access_decision];
            return chip(tone, icon, label);
          }),
          tooltipValueGetter: (p) => (p.data && p.data.reason_code ? humanize(p.data.reason_code) : null),
        },
        {
          headerName: "HTTP", field: "http_status", minWidth: 90, maxWidth: 110, filter: "agNumberColumnFilter",
          cellRenderer: cellNode((r) => el("span", { className: "http-pill", dataset: { tone: httpTone(r.http_status) }, text: String(r.http_status || "—") })),
        },
        { headerName: "Crawler", field: "crawler_id", minWidth: 170, cellClass: "mono", tooltipField: "crawler_id" },
        { headerName: "Resource", field: "resource", minWidth: 220, cellClass: "mono", tooltipField: "resource" },
        {
          headerName: "Tier", field: "pricing_tier_name", minWidth: 110, maxWidth: 140,
          valueGetter: (p) => (p.data ? p.data.pricing_tier_name || "Base" : ""),
          cellRenderer: cellNode((r) => tierBadge(r.pricing_tier, r.pricing_tier_name)),
        },
        {
          headerName: "Deposit", field: "deposit_amount", minWidth: 100, maxWidth: 130, type: "numericColumn", filter: "agNumberColumnFilter",
          valueGetter: (p) => (p.data && p.data.deposit_amount != null ? Number(p.data.deposit_amount) : null),
          valueFormatter: (p) => (p.data && p.value != null ? money(p.value, p.data.currency) : "—"),
        },
        {
          headerName: "Payment status", field: "deposit_state", minWidth: 150,
          valueGetter: (p) => (p.data ? paymentLabel(p.data) : ""),
          cellRenderer: cellNode((r) => (r.deposit_state ? depositChip(r.deposit_state) : el("span", { className: "muted", text: "No deposit yet" }))),
          tooltipValueGetter: (p) => (p.data && p.data.payment_status ? `PayPal payment status: ${p.data.payment_status}` : null),
        },
        {
          headerName: "Rail", field: "simulated_payment", minWidth: 120, maxWidth: 150,
          valueGetter: (p) => (p.data ? railLabel(p.data) : ""),
          tooltipValueGetter: (p) => (p.data && p.data.provider_order_id ? `Order ${p.data.provider_order_id}` : null),
        },
        // Shown in the expanded row; hidden here to keep the table readable. Still exported to CSV.
        { headerName: "Request ID", field: "request_id", minWidth: 150, cellClass: "mono", tooltipField: "request_id", hide: true },
      ],
    });
    if (!state.historyGrid) return;
    $("history-this-run").addEventListener("change", () => loadHistory());
  }

  async function loadHistory() {
    if (!state.historyGrid) return;
    if (state.historyLoading) { state.historyAgain = true; return; }
    if (!state.config || state.config.access === "none") {
      $("history-hint").textContent = "Request history needs view access (admin token, or a public demo).";
      return;
    }
    const onlyRun = $("history-this-run").checked && state.run;
    const url = onlyRun ? `/demo/requests?crawler_id=${encodeURIComponent(state.run.crawler_id)}` : "/demo/requests";
    state.historyLoading = true;
    try {
      const result = await getJson(url);
      if (result.ok) {
        const rows = result.body.rows;
        state.historyGrid.setRows(rows);
        renderMonitorStats(rows);
        $("history-hint").textContent = `${rows.length} request(s)${onlyRun ? " for this run's crawler" : " (latest 200)"} · updated ${formatTime(result.body.generated_at)}.`;
      } else {
        $("history-hint").textContent = message(result.body, result.status);
      }
    } catch (_) {
      $("history-hint").textContent = "Could not load request history.";
    } finally {
      state.historyLoading = false;
      if (state.historyAgain) { state.historyAgain = false; loadHistory(); }
    }
  }

  function scheduleHistory(ms) {
    clearTimeout(state.historyTimer);
    state.historyTimer = setTimeout(async () => {
      await loadHistory();
      const running = state.run && !["completed", "failed", "cancelled"].includes(state.run.status);
      scheduleHistory(running ? 2000 : HISTORY_IDLE_MS);
    }, ms);
  }

  // --- run lifecycle ---------------------------------------------------------------------
  function resetRunView() {
    clearTimeout(state.drainTimer);
    state.queue = [];
    state.draining = false;
    state.latest = null;
    state.instant = false;
    state.eventAt = null;
    stopClock();
    renderClock();
    resetAlerts();
    $("timeline").dataset.instant = "false";
    state.run = null;
    state.nextSeq = 0;
    state.counts = { allowed: 0, blocked: 0, violations: 0 };
    state.note = null;
    state.pendingRow = null;
    state.lastExplanationFor = null;
    clear($("timeline"));
    clear($("report"));
    resetSteps();
    renderDeposit(null);
    renderExplanation(null);
    renderRunSummary(null);
    renderVerdict(null);
    $("approval").hidden = true;
  }

  // Reset: forget the finished run on this page and return to the start state. Server-side
  // records (deposits, audit log) are untouched; they stay in the request monitor.
  function resetDemo() {
    if (state.run && !["completed", "failed", "cancelled"].includes(state.run.status)) return;
    clearTimeout(state.timer);
    state.runId = null;
    resetRunView();
    renderEmptyTimeline();
    $("run-meta").textContent = "";
    $("history-this-run").checked = false;
    $("launch-hint").textContent = "";
    loadHistory();
    updateLauncher();
    document.querySelector(".launcher").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const isFinished = (run) => ["completed", "failed", "cancelled"].includes(run.status);

  // Called for every poll: queue the new events and keep the latest snapshot.
  function applyRun(run) {
    if (!state.run) {
      state.run = run;
      // A run that is already over when we attach (page reload) renders at once.
      state.instant = isFinished(run) || reducedMotion();
      $("timeline").dataset.instant = state.instant ? "true" : "false";
      startClock();
    }
    state.latest = run;
    state.queue.push(...run.events);
    state.nextSeq = run.next_seq;
    drain();
    return isFinished(run);
  }

  // Reveal queued events with a short stagger that shrinks when the backlog grows, then render
  // the snapshot (deposit, verdict, explanation) once the timeline has caught up.
  function drain() {
    if (state.draining) return;
    if (!state.queue.length) { renderSnapshot(state.latest); return; }
    state.draining = true;
    const step = () => {
      if (!state.queue.length) {
        state.draining = false;
        renderSnapshot(state.latest);
        return;
      }
      const event = state.queue.shift();
      handleEvent(event);
      renderRunState();
      if (state.instant) { step(); return; }
      const slow = event.kind === "tool_call" ? 420 : event.kind === "tool_result" ? 300 : 220;
      state.drainTimer = setTimeout(step, Math.max(60, slow - state.queue.length * 22));
    };
    step();
  }

  function renderSnapshot(run) {
    if (!run) return;
    state.run = run;
    const label = run.driver === "ai" ? `${providerName(run.provider)} ${run.model || ""}` : "Scripted";
    $("run-meta").textContent = `${run.run_id} · ${label} · ${run.crawler_id} · ${run.status.replace("_", " ")}`;
    $("approval").hidden = run.status !== "awaiting_approval";
    if (run.status === "awaiting_approval" && run.approval_url) $("approval-open").dataset.url = run.approval_url;
    renderDeposit(run);
    renderRunSummary(run);
    renderVerdict(run);
    if (isFinished(run)) {
      if (state.lastExplanationFor !== run.run_id) {
        state.lastExplanationFor = run.run_id;
        loadHistory();
        renderExplanation(run.explanation);
        if (run.explanation) setStep("explain", "done", run.explanation.generated_by === "template" ? "Rule-based" : `By ${providerName(run.explanation.generated_by)}`);
        else setStep("explain", "skipped", "Not available");
        for (const key of ["challenge", "policy"]) {
          const st = state.steps[key].item.dataset.state;
          if (st === "pending" || st === "active") setStep(key, "skipped", key === "policy" ? "Skipped by the crawler" : "Not observed");
        }
        renderReport(run);
        $("timeline").dataset.instant = "false";
      }
    }
    updateLauncher();
  }

  async function poll() {
    if (!state.runId) return;
    try {
      const result = await getJson(`/demo/runs/${encodeURIComponent(state.runId)}?after=${state.nextSeq}`);
      if (!result.ok) {
        if (result.status === 404) { state.runId = null; return; }
        schedule(IDLE_POLL_MS);
        return;
      }
      const finished = applyRun(result.body);
      if (finished) {
        loadConfig();
        return;
      }
      schedule(result.body.status === "awaiting_approval" ? 1500 : POLL_MS);
    } catch (_) {
      schedule(IDLE_POLL_MS);
    }
  }

  function schedule(ms) {
    clearTimeout(state.timer);
    state.timer = setTimeout(poll, ms);
  }

  function attach(runId) {
    clearTimeout(state.timer);
    state.runId = runId;
    resetRunView();
    poll();
  }

  async function startRun() {
    const persona = document.querySelector("input[name=persona]:checked").value;
    const driver = document.querySelector("input[name=driver]:checked").value;
    $("start-button").disabled = true;
    $("launch-hint").textContent = "Starting...";
    // Only the tier ID is sent; the gateway looks up the resource and its price.
    const payload = { persona, driver };
    if (persona === "compliant" && tierById(state.tier)) payload.tier = state.tier;
    try {
      const result = await getJson("/demo/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!result.ok) {
        $("launch-hint").textContent = message(result.body, result.status);
        $("start-button").disabled = false;
        if (result.status === 409) loadConfig();
        return;
      }
      attach(result.body.run_id);
      scheduleHistory(1000);
    } catch (_) {
      $("launch-hint").textContent = "Could not reach the gateway.";
      $("start-button").disabled = false;
    }
  }

  async function stopRun() {
    if (!state.runId) return;
    await getJson(`/demo/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" });
  }

  async function confirmApproval() {
    if (!state.runId) return;
    await getJson(`/demo/runs/${encodeURIComponent(state.runId)}/approval`, { method: "POST" });
  }

  function openApproval() {
    const url = $("approval-open").dataset.url;
    if (!url || !/^https:\/\/([a-z0-9-]+\.)*paypal\.com\//i.test(url)) return;
    state.popup = window.open(url, "sara-paypal", "popup,width=480,height=760");
    if (!state.popup) window.location.assign(url);
  }

  // --- admin token popover (same behaviour as the console) -----------------------------------
  function initAdminMenu() {
    const toggle = $("admin-toggle");
    const pop = $("admin-pop");
    const label = () => {
      toggle.dataset.active = state.token ? "true" : "false";
      $("admin-toggle-text").textContent = state.token ? "Admin ✓" : "Admin";
    };
    const close = () => { pop.hidden = true; toggle.setAttribute("aria-expanded", "false"); };
    toggle.addEventListener("click", () => {
      pop.hidden = !pop.hidden;
      toggle.setAttribute("aria-expanded", String(!pop.hidden));
      if (!pop.hidden) $("admin-token").focus();
    });
    document.addEventListener("click", (event) => { if (!pop.hidden && !event.target.closest(".admin-menu")) close(); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !pop.hidden) { close(); toggle.focus(); } });
    $("token-form").addEventListener("submit", () => { close(); setTimeout(label, 0); });
    label();
  }

  // --- keyboard shortcuts: S starts a run, R resets (ignored while typing) ----------------------
  function initShortcuts() {
    document.addEventListener("keydown", (event) => {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
      const target = event.target;
      const typing = target && (target.isContentEditable || /^(TEXTAREA|SELECT)$/.test(target.tagName)
        || (target.tagName === "INPUT" && !["radio", "checkbox", "button"].includes(target.type)));
      if (typing) return;
      const key = event.key.toLowerCase();
      if (key === "s" && !$("start-button").disabled) { event.preventDefault(); startRun(); }
      else if (key === "r" && !$("reset-button").disabled) { event.preventDefault(); resetDemo(); }
    });
  }

  // --- init --------------------------------------------------------------------------------
  function init() {
    initTheme();
    renderSteps();
    renderEmptyTimeline();
    state.token = store.get("sessionStorage", "ct-admin-token");
    if (state.token) $("admin-token").value = state.token;

    $("token-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = $("admin-token").value.trim();
      state.token = value || null;
      store.set("sessionStorage", "ct-admin-token", state.token);
      loadConfig();
      if (state.paypal) state.paypal.refresh();
    });
    initAdminMenu();
    initShortcuts();
    $("start-button").addEventListener("click", startRun);
    $("stop-button").addEventListener("click", stopRun);
    $("reset-button").addEventListener("click", resetDemo);
    $("tl-filters").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-filter]");
      if (button) setTimelineFilter(button.dataset.filter);
    });
    // Scrolling back through the timeline pauses follow-live; ticking the box resumes it.
    $("timeline").addEventListener("wheel", (event) => { if (event.deltaY < 0) $("tl-follow").checked = false; }, { passive: true });
    $("tl-follow").addEventListener("change", followLive);
    $("approval-open").addEventListener("click", openApproval);
    $("approval-done").addEventListener("click", confirmApproval);
    window.addEventListener("message", (event) => {
      if (event.origin === window.location.origin && event.data && event.data.type === "sara-paypal") confirmApproval();
    });

    if (window.CTPayPal) {
      state.paypal = window.CTPayPal.create($("paypal-panel"), { headers: () => (state.token ? { "X-Admin-Token": state.token } : {}) });
    }
    for (const input of document.querySelectorAll("input[name=persona]")) input.addEventListener("change", updateTierHint);
    initHistoryGrid();
    renderDeposit(null);
    renderExplanation(null);
    loadConfig().then(() => scheduleHistory(0));
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
