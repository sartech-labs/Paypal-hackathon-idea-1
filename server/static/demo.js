/* CrawlerTrust live demo: start a crawler run, follow it live, and verify with PayPal.
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
  };

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
  const THEMES = ["system", "light", "dark"];
  function applyTheme(pref) {
    if (pref === "light" || pref === "dark") document.documentElement.dataset.theme = pref;
    else delete document.documentElement.dataset.theme;
    $("theme-toggle").textContent = `Theme: ${pref}`;
  }
  function initTheme() {
    let pref = store.get("localStorage", "ct-theme") || "system";
    if (!THEMES.includes(pref)) pref = "system";
    applyTheme(pref);
    $("theme-toggle").addEventListener("click", () => {
      const current = store.get("localStorage", "ct-theme") || "system";
      const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
      store.set("localStorage", "ct-theme", next === "system" ? null : next);
      applyTheme(next);
    });
  }

  // --- steps -----------------------------------------------------------------------------
  const STEP_DEFS = [
    ["challenge", "402 challenge", "The gateway asks for a deposit"],
    ["policy", "Read the policy", "Allowed paths, limits, refund rules"],
    ["deposit", "PayPal deposit", "Refundable payment"],
    ["crawl", "Crawl under policy", "Every request checked"],
    ["settle", "Settlement", "Refund, release, or forfeit"],
    ["explain", "Explanation", "Why, with audit evidence"],
  ];

  function renderSteps() {
    const list = $("steps");
    clear(list);
    STEP_DEFS.forEach(([key, label, hint], index) => {
      const status = el("span", { className: "step-status", text: hint });
      const item = el("li", { className: "step", dataset: { state: "pending" } }, [
        el("span", { className: "step-num", "aria-hidden": "true", text: String(index + 1) }),
        el("span", { className: "step-label", text: label }),
        status,
      ]);
      state.steps[key] = { item, status, hint };
      list.append(item);
    });
  }

  function setStep(key, stepState, text) {
    const step = state.steps[key];
    if (!step) return;
    step.item.dataset.state = stepState;
    step.status.textContent = text || step.hint;
  }

  function resetSteps() {
    for (const [key, step] of Object.entries(state.steps)) setStep(key, "pending", step.hint);
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
      badge("neutral", `Deposit ${c.deposit_amount} ${c.currency} · policy ${c.policy_version}`)
    );
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
      state.config = result.body;
      renderBadges(state.config);
      if (state.config.access === "none") {
        showBanner("This deployment is private. Enter the admin token to watch and start runs, or run the gateway with DEMO_PUBLIC=true.");
      }
      if (!state.runId && state.config.active_run_id) attach(state.config.active_run_id);
      updateLauncher();
    } catch (_) {
      showBanner("Cannot reach the CrawlerTrust gateway. Is the server running?");
    }
  }

  // --- timeline --------------------------------------------------------------------------
  function timelineItem({ kind, tone, icon, title, detail, extra, at, pending, note }) {
    return el("li", { className: "tl-item", dataset: { kind, tone: tone || "neutral", pending: pending ? "true" : "false" } }, [
      el("span", { className: "tl-icon", "aria-hidden": "true", text: icon }),
      el("div", { className: "tl-body" }, [
        el("div", { className: "tl-title" }, [...(Array.isArray(title) ? title : [el("span", { text: title })]), at ? el("span", { className: "tl-time", text: formatTime(at) }) : null]),
        note ? el("div", { className: "tl-note", text: note }) : null,
        detail ? el("div", { className: "tl-detail", text: detail }) : null,
        extra || null,
      ]),
    ]);
  }

  // Narration that precedes a tool call is shown inside that call's row as its "why".
  function flushNote() {
    if (!state.note) return;
    const { text, at } = state.note;
    state.note = null;
    addItem(timelineItem({ kind: "narration", icon: "“", title: state.run && state.run.driver === "ai" ? "Agent" : "Crawler", detail: text, at }));
  }

  function addItem(item) {
    const list = $("timeline");
    const empty = list.querySelector(".empty-state");
    if (empty) empty.remove();
    list.append(item);
    list.scrollTop = list.scrollHeight;
    return item;
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

  function applyToolResult(row, name, result, isError, at) {
    const r = result || {};
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
        detail = `Deposit required: ${d.amount} ${d.currency}, refundable. Policy ${r.policy_version}.`;
        setStep("challenge", "done", `HTTP 402 · ${d.amount} ${d.currency} deposit`);
      } else if (status === 200) {
        state.counts.allowed += 1;
        detail = `Served. Session budget left ${r.remaining_session_budget}, requests left in this window ${r.remaining_window_requests}.`;
        if (r.content) extra = el("details", { className: "tl-more" }, [el("summary", { text: "Page text (untrusted content)" }), el("pre", { text: r.content })]);
        setStep("crawl", "active", "Requests allowed by policy");
      } else {
        state.counts.blocked += 1;
        if (r.violation) state.counts.violations += 1;
        const violation = r.violation ? " Counts as a policy violation." : "";
        const disq = r.disqualifying ? " Disqualifying: the deposit will be kept." : "";
        detail = `${humanize(r.reason_code)}.${violation}${disq}`;
        if (r.retry_after_seconds) detail += ` Retry after ${r.retry_after_seconds}s.`;
        setStep("crawl", "failed", "Violations recorded");
      }
      if (r.prompt_injection) {
        addItem(timelineItem({
          kind: "injection",
          tone: "serious",
          icon: "!",
          title: "Prompt injection hidden in page content",
          detail: "The page tells AI agents to fetch /private/customer-data. A well-behaved crawler treats page text as data and ignores it.",
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
        detail = `${r.required_amount} ${r.currency} ${r.simulated ? "(simulated)" : "via PayPal Sandbox"} · ${settlement} · payment ${r.payment_status}.`;
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
    const updated = timelineItem({ kind: "tool", tone, icon: row.querySelector(".tl-icon").textContent, title: title || row.querySelector(".tl-title span").textContent, detail, extra, at, note: noteNode ? noteNode.textContent : null });
    row.replaceWith(updated);
  }

  function handleEvent(event) {
    const d = event.data || {};
    if (!["text", "tool_call", "tool_result"].includes(event.kind)) flushNote();
    switch (event.kind) {
      case "run_started":
        addItem(timelineItem({
          kind: "start", tone: "accent", icon: "▶",
          title: d.driver === "ai" ? `AI agent started (${providerName(d.provider)} ${d.model})` : "Scripted crawler started",
          detail: `${d.persona_label}. Task: ${d.task}`, at: event.at,
        }));
        break;
      case "thinking":
        addItem(timelineItem({ kind: "reasoning", icon: "…", title: "Reasoning", detail: d.text.length > 600 ? `${d.text.slice(0, 600)}...` : d.text, at: event.at }));
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
        state.pendingRow = addItem(timelineItem({ kind: "tool", icon: info.icon, title: info.title, pending: true, at: event.at, note }));
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
        addItem(timelineItem({ kind: "approval", tone: "accent", icon: "$", title: "PayPal checkout created; waiting for buyer approval", detail: "Approve the Sandbox checkout in the PayPal window.", at: event.at }));
        break;
      case "approval_done":
        addItem(timelineItem({ kind: "approval", tone: d.outcome === "paid" || d.outcome === "confirmed" ? "good" : "critical", icon: "$", title: `PayPal approval: ${d.outcome}`, at: event.at }));
        break;
      case "stop":
        addItem(timelineItem({ kind: "stop", tone: "warning", icon: "!", title: "Run stopped", detail: d.reason, at: event.at }));
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
    return [el("dt", { text: label }), el("dd", { className: mono ? "mono" : null, text: value })];
  }

  function miniTile(label, value) {
    return el("div", { className: "mini-tile" }, [el("div", { className: "tile-label", text: label }), el("div", { className: "tile-value", text: String(value) })]);
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
      el("dl", { className: "facts" }, [
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
        miniTile("Allowed", s.allowed_requests),
        miniTile("Blocked", s.rejected_requests),
        miniTile("Violations", s.total_violations),
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
        : null
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

  // --- run lifecycle ---------------------------------------------------------------------
  function resetRunView() {
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
    $("approval").hidden = true;
  }

  function applyRun(run) {
    state.run = run;
    for (const event of run.events) handleEvent(event);
    state.nextSeq = run.next_seq;
    const label = run.driver === "ai" ? `${providerName(run.provider)} ${run.model || ""}` : "Scripted";
    $("run-meta").textContent = `${run.run_id} · ${label} · ${run.crawler_id} · ${run.status.replace("_", " ")}`;
    $("approval").hidden = run.status !== "awaiting_approval";
    if (run.status === "awaiting_approval" && run.approval_url) $("approval-open").dataset.url = run.approval_url;
    renderDeposit(run);
    const finished = ["completed", "failed", "cancelled"].includes(run.status);
    if (finished) {
      if (state.lastExplanationFor !== run.run_id) {
        state.lastExplanationFor = run.run_id;
        renderExplanation(run.explanation);
        if (run.explanation) setStep("explain", "done", run.explanation.generated_by === "template" ? "Rule-based" : `By ${providerName(run.explanation.generated_by)}`);
        else setStep("explain", "skipped", "Not available");
        if (state.steps.policy.item.dataset.state === "pending") setStep("policy", "skipped", "Skipped by the crawler");
        renderReport(run);
      }
    }
    updateLauncher();
    return finished;
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
    try {
      const result = await getJson("/demo/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona, driver }),
      });
      if (!result.ok) {
        $("launch-hint").textContent = message(result.body, result.status);
        $("start-button").disabled = false;
        if (result.status === 409) loadConfig();
        return;
      }
      attach(result.body.run_id);
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
    state.popup = window.open(url, "crawlertrust-paypal", "popup,width=480,height=760");
    if (!state.popup) window.location.assign(url);
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
    $("start-button").addEventListener("click", startRun);
    $("stop-button").addEventListener("click", stopRun);
    $("approval-open").addEventListener("click", openApproval);
    $("approval-done").addEventListener("click", confirmApproval);
    window.addEventListener("message", (event) => {
      if (event.origin === window.location.origin && event.data && event.data.type === "crawlertrust-paypal") confirmApproval();
    });

    if (window.CTPayPal) {
      state.paypal = window.CTPayPal.create($("paypal-panel"), { headers: () => (state.token ? { "X-Admin-Token": state.token } : {}) });
    }
    renderDeposit(null);
    renderExplanation(null);
    loadConfig();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
