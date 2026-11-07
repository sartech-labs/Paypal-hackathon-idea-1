/* AgentTrust agent studio: start the personal agent, answer its approval requests, and
   follow what it pays for. Talks to /demo/* (runs) and /.well-known/agent-commerce.json. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 1000;
  const state = { token: null, runId: null, after: 0, timer: null, currency: "USD", config: null, pending: new Map(), decisionKey: null, approvalUrl: null, startedAt: null };

  const store = {
    get(kind, key) { try { return window[kind].getItem(key); } catch (_) { return null; } },
    set(kind, key, value) { try { value === null ? window[kind].removeItem(key) : window[kind].setItem(key, value); } catch (_) { /* storage unavailable */ } },
  };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "class") node.className = value;
      else if (key.startsWith("data-") || key.startsWith("aria-") || key === "role" || key === "title" || key === "type") node.setAttribute(key, value);
      else node[key] = value;
    }
    for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
    return node;
  }
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

  function money(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "-";
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: state.currency }).format(n); } catch (_) { return `${n.toFixed(2)} ${state.currency}`; }
  }

  async function api(path, options = {}) {
    const headers = Object.assign({ Accept: "application/json" }, options.body ? { "Content-Type": "application/json" } : {});
    if (state.token) headers["X-Admin-Token"] = state.token;
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    let data = null;
    try { data = await resp.json(); } catch (_) { data = null; }
    if (!resp.ok) {
      const message = (data && (data.message || data.error)) || `HTTP ${resp.status}`;
      const error = new Error(message);
      error.status = resp.status;
      throw error;
    }
    return data;
  }

  function showBanner(text) { const b = $("banner"); clear(b); b.append(el("span", { text })); b.hidden = false; }
  function hideBanner() { $("banner").hidden = true; }

  // --- theme and admin token ------------------------------------------------------------
  const THEMES = ["dark", "light", "system"];
  function initTheme() {
    const apply = (pref) => { document.documentElement.dataset.theme = pref; $("theme-toggle").textContent = `Theme: ${pref}`; };
    let pref = store.get("localStorage", "ct-theme") || "dark";
    if (!THEMES.includes(pref)) pref = "dark";
    apply(pref);
    $("theme-toggle").addEventListener("click", () => {
      const current = store.get("localStorage", "ct-theme") || "dark";
      const next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
      store.set("localStorage", "ct-theme", next === "dark" ? null : next);
      apply(next);
    });
  }
  function initAdmin() {
    state.token = store.get("sessionStorage", "ct-admin-token");
    if (state.token) { $("admin-token").value = state.token; $("admin-toggle-text").textContent = "Admin ✓"; }
    $("admin-toggle").addEventListener("click", () => {
      const pop = $("admin-pop");
      pop.hidden = !pop.hidden;
      $("admin-toggle").setAttribute("aria-expanded", String(!pop.hidden));
      if (!pop.hidden) $("admin-token").focus();
    });
    $("token-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = $("admin-token").value.trim();
      state.token = value || null;
      store.set("sessionStorage", "ct-admin-token", state.token);
      $("admin-toggle-text").textContent = state.token ? "Admin ✓" : "Admin";
      $("admin-pop").hidden = true;
      loadConfig();
    });
  }

  // --- presets -----------------------------------------------------------------------------
  const PRESETS = [
    {
      label: "Research + buy everything",
      text: "I'm researching how AI agents pay websites. Read the public news on this site (use the refundable crawl deposit and make sure I get it back), buy the agent-economy video briefing if it's under $3, subscribe to the members plan if it costs less than $10 a month and summarise today's members feed, and order the Agent Economy Playbook e-book for me.",
    },
    {
      label: "Just the data",
      text: "Get me the agent traffic dataset and tell me which site category has the highest checkout conversion. Don't buy anything else.",
    },
    {
      label: "Shopping trip",
      text: "Order the printed annual report and two Sentinel News mugs for my team, then tell me the total I paid including shipping.",
    },
    {
      label: "Members feed (has a trap)",
      text: "Subscribe to the members plan if it's under $10 a month and summarise today's members feed for me. Don't buy anything else.",
    },
    {
      label: "Tight budget",
      text: "Read today's headlines through the crawl deposit, then get me the members-only daily feed. I only want to spend a few dollars.",
    },
  ];
  function initPresets() {
    const holder = $("presets");
    PRESETS.forEach((preset, index) => {
      const button = el("button", { type: "button", text: preset.label });
      button.addEventListener("click", () => { $("task").value = preset.text; if (preset.label === "Tight budget") $("budget").value = "5"; });
      holder.append(button);
      if (index === 0 && !$("task").value) $("task").value = preset.text;
    });
  }

  // --- offer catalog ------------------------------------------------------------------------
  function offerCard(type, icon, name, desc, rows) {
    return el(
      "article",
      { class: "offer", "data-type": type },
      el("div", { class: "offer-head" }, el("span", { class: "offer-icon", "aria-hidden": "true", text: icon }), el("span", { class: "offer-name", text: name })),
      el("p", { class: "offer-desc", text: desc }),
      el("ul", { class: "offer-list" }, rows.map(([label, price]) => el("li", {}, el("span", { text: label, title: label }), el("span", { class: "price", text: price })))),
    );
  }
  async function loadCatalog() {
    try {
      const catalog = await api("/.well-known/agent-commerce.json");
      state.currency = catalog.currency || "USD";
      document.querySelectorAll("[data-currency]").forEach((node) => {
        try { node.textContent = (0).toLocaleString(undefined, { style: "currency", currency: state.currency }).replace(/[\d.,\s]/g, "") || state.currency; } catch (_) { node.textContent = state.currency; }
      });
      $("merchant-name").textContent = catalog.merchant || "this site";
      const offers = catalog.offers || {};
      const grid = $("offer-grid");
      clear(grid);
      grid.append(
        offerCard("deposit", "↺", "Refundable crawl deposit", "Read protected pages under the crawling policy. Returned in full if the agent follows the rules.",
          ((offers.crawl_deposits || {}).tiers || []).map((t) => [`${t.name} tier`, money(t.price)])),
        offerCard("access", "▶", "Pay-per-access", "A fixed, one-off charge unlocks one video, dataset, or audio item.",
          (offers.pay_per_access || []).map((i) => [i.title, money(i.price)])),
        offerCard("subscription", "∞", "Subscription", "Recurring PayPal subscription for members-only pages. Always needs your approval.",
          (offers.subscriptions || []).map((p) => [`${p.name} (${p.billing})`, money(p.price)])),
        offerCard("product", "◫", "Products", "Digital downloads and shipped goods in one itemized PayPal order.",
          (offers.products || []).map((p) => [p.name, money(p.price)])),
      );
    } catch (err) {
      clear($("offer-grid"));
      $("offer-grid").append(el("p", { class: "empty", text: `Could not load the offer catalog: ${err.message}` }));
    }
  }

  async function loadConfig() {
    try {
      const config = await api("/demo/config");
      state.config = config;
      const badges = $("badges");
      clear(badges);
      const badge = (tone, text) => el("span", { class: "badge" }, el("span", { class: "badge-dot", "data-tone": tone }), text);
      badges.append(
        badge(config.payment_mode === "mock" ? "warning" : "good", config.payment_mode === "mock" ? "PayPal: simulated (mock mode)" : "PayPal Sandbox"),
        badge(config.ai_available ? "accent" : "warning", config.ai_available ? `AI: ${config.ai_provider === "openai" ? "OpenAI" : "Claude"} (${config.ai_model})` : "AI: no key configured"),
        badge("good", "Orders · Payments · Subscriptions APIs"),
      );
      const start = $("start-button");
      if (!config.ai_available) { start.disabled = true; $("launch-hint").textContent = "Set OPENAI_API_KEY or ANTHROPIC_API_KEY on the server to run the agent."; }
      else if (!config.can_start_runs) { start.disabled = true; $("launch-hint").textContent = "This deployment is private: enter the admin token to run the agent."; }
      else { start.disabled = !!state.runId; $("launch-hint").textContent = `${config.runs_remaining_this_hour} runs left this hour on this demo.`; }
      if (!state.runId && config.active_run_id) attach(config.active_run_id);
      if (config.paypal_js_client_id && !state.sdkLoaded) initHumanCheckout(config.paypal_js_client_id);
    } catch (err) {
      showBanner(`Could not load the demo configuration: ${err.message}`);
    }
  }

  // --- human checkout: PayPal JS SDK v6 (Sandbox mode only) --------------------------------
  // A person buys the same pay-per-access items with PayPal's own buttons. The site creates
  // and prices the order (buyer "human", so no Sandbox test-card autopay), the person approves
  // at PayPal, and the site captures and verifies it before unlocking anything.
  function showHumanResult(text, tone) {
    const box = $("human-result");
    box.textContent = text;
    box.dataset.tone = tone || "";
    box.hidden = false;
  }

  function initHumanCheckout(clientId) {
    state.sdkLoaded = true;
    const script = document.createElement("script");
    script.src = "https://www.sandbox.paypal.com/web-sdk/v6/core";
    script.async = true;
    script.onload = () => setupButtons(clientId).catch((err) => console.warn("PayPal buttons unavailable", err));
    document.head.append(script);
  }

  async function setupButtons(clientId) {
    const sdk = await window.paypal.createInstance({ clientId, components: ["paypal-payments"] });
    const eligibility = await sdk.findEligibleMethods({ currencyCode: state.currency });
    if (!eligibility.isEligible("paypal")) return;
    const catalog = await api("/.well-known/agent-commerce.json");
    const items = (catalog.offers || {}).pay_per_access || [];
    const grid = $("human-grid");
    clear(grid);
    const buyerId = `web-${Math.random().toString(16).slice(2, 10)}`;
    for (const item of items) {
      let ourOrderId = null;
      const session = sdk.createPayPalOneTimePaymentSession({
        async onApprove(data) {
          try {
            const paid = await api(`/commerce/orders/${encodeURIComponent(ourOrderId)}/capture`, { method: "POST", body: JSON.stringify({ agent_id: buyerId }) });
            if (paid.status !== "PAID") { showHumanResult(`Payment not completed (status ${paid.status}).`, "bad"); return; }
            const resp = await fetch(`/paid${item.unlocks}`, { headers: { Authorization: `Bearer ${paid.access_token}` } });
            const text = await resp.text();
            const preview = new DOMParser().parseFromString(text, "text/html").body.textContent || text;
            showHumanResult(`Paid ${money(paid.amount)} with PayPal (order ${data.orderId}, capture ${paid.provider_capture_id}).\n\n${preview.trim().slice(0, 900)}`);
          } catch (err) { showHumanResult(`Capture failed: ${err.message}`, "bad"); }
        },
        onCancel() { showHumanResult("Payment cancelled; nothing was charged.", "bad"); },
        onError(err) { showHumanResult(`PayPal error: ${(err && err.message) || err}`, "bad"); },
      });
      const button = document.createElement("paypal-button");
      button.setAttribute("type", "pay");
      button.addEventListener("click", () => {
        const createOrder = api("/commerce/checkout", {
          method: "POST",
          body: JSON.stringify({ agent_id: buyerId, offer_type: "access", item_id: item.item_id, buyer: "human", purpose: "Bought by a person with PayPal's buttons" }),
        }).then((order) => { ourOrderId = order.order_id; return { orderId: order.provider_order_id }; });
        session.start({ presentationMode: "auto" }, createOrder).catch((err) => showHumanResult(`Could not start checkout: ${err.message}`, "bad"));
      });
      grid.append(el("div", { class: "human-item" }, el("strong", { text: item.title }), el("span", { class: "price", text: `${money(item.price)} · one-off` }), button));
    }
    $("human-buy").hidden = !items.length;
  }

  // --- run lifecycle ------------------------------------------------------------------------
  function selectedTypes() { return Array.from(document.querySelectorAll('input[name="ptype"]:checked')).map((i) => i.value); }

  async function startRun() {
    const task = $("task").value.trim();
    if (task.length < 8) { showBanner("Write a request for the agent (at least a few words)."); return; }
    const types = selectedTypes();
    if (!types.length) { showBanner("Allow at least one payment type."); return; }
    const budget = Math.max(1, Math.min(200, Number($("budget").value) || 60)).toFixed(2);
    const threshold = Math.max(0, Math.min(200, Number($("threshold").value) || 0)).toFixed(2);
    hideBanner();
    $("start-button").disabled = true;
    try {
      const maxPer = $("max-per").value.trim();
      const merchants = $("merchants").value.split(",").map((m) => m.trim()).filter(Boolean);
      const body = { driver: "ai", persona: "assistant", task, budget, approval_threshold: threshold, allowed_types: types, purchase_review: $("purchase-review").checked };
      if (maxPer !== "" && Number(maxPer) > 0) body.max_per_purchase = Math.min(200, Number(maxPer)).toFixed(2);
      if (merchants.length) body.allowed_merchants = merchants;
      const run = await api("/demo/runs", { method: "POST", body: JSON.stringify(body) });
      resetView();
      attach(run.run_id, run);
    } catch (err) {
      $("start-button").disabled = false;
      showBanner(err.message);
    }
  }

  function resetView() {
    clear($("activity"));
    state.pending.clear();
    state.after = 0;
    state.decisionKey = null;
    $("report").hidden = true;
    clear($("report"));
    $("decision").hidden = true;
    $("paypal-approval").hidden = true;
    renderWallet(null);
  }

  function attach(runId, initial) {
    state.runId = runId;
    state.startedAt = Date.now();
    $("stop-button").hidden = false;
    $("start-button").disabled = true;
    if (initial) handleSnapshot(initial);
    clearTimeout(state.timer);
    poll();
  }

  async function poll() {
    if (!state.runId) return;
    try {
      const snap = await api(`/demo/runs/${encodeURIComponent(state.runId)}?after=${state.after}`);
      handleSnapshot(snap);
      if (["completed", "failed", "cancelled"].includes(snap.status)) { finish(snap); return; }
    } catch (err) {
      if (err.status === 404) { finish(null); return; }
    }
    state.timer = setTimeout(poll, POLL_MS);
  }

  function finish(snap) {
    state.runId = null;
    $("stop-button").hidden = true;
    $("decision").hidden = true;
    $("paypal-approval").hidden = true;
    loadConfig();
    if (snap) renderReport(snap);
  }

  function setRunState(status) {
    const node = $("run-state");
    const map = { running: ["running", "Working"], awaiting_decision: ["waiting", "Waiting for your approval"], awaiting_approval: ["waiting", "Waiting for PayPal approval"], completed: ["completed", "Finished"], failed: ["failed", "Failed"], cancelled: ["cancelled", "Stopped"] };
    const [tone, label] = map[status] || ["idle", "Idle"];
    node.dataset.state = tone;
    $("run-state-text").textContent = label;
  }

  function handleSnapshot(snap) {
    if (state.activityEmpty !== false) { clear($("activity")); state.activityEmpty = false; }
    setRunState(snap.status);
    if (snap.task) $("run-sub").textContent = `Task: ${snap.task}`;
    for (const event of snap.events || []) renderEvent(event);
    state.after = snap.next_seq;
    renderWallet(snap.mandate);
    renderDecision(snap);
    const approval = $("paypal-approval");
    state.approvalUrl = snap.approval_url || null;
    approval.hidden = !(snap.status === "awaiting_approval" && snap.approval_url);
  }

  // --- decisions --------------------------------------------------------------------------
  const TYPE_LABEL = { deposit: "Crawl deposit", access: "Pay-per-access", subscription: "Subscription", product: "Product order" };
  function renderDecision(snap) {
    const box = $("decision");
    const pending = snap.status === "awaiting_decision" ? snap.pending_decision : null;
    if (!pending) { box.hidden = true; state.decisionKey = null; return; }
    const key = `${pending.title}|${pending.amount}|${snap.next_seq}`;
    if (state.decisionKey && box.hidden === false) return;
    state.decisionKey = key;
    $("decision-kind").textContent = `${TYPE_LABEL[pending.payment_type] || "Purchase"} · approval needed`;
    $("decision-title").textContent = pending.title;
    $("decision-amount").textContent = `${money(pending.amount)}${pending.recurring ? " / billing cycle" : ""}`;
    $("decision-reason").textContent = pending.reason ? `Agent's reason: "${pending.reason}"` : "The agent gave no reason.";
    const review = pending.review;
    $("decision-review").hidden = !review;
    if (review) $("decision-review").textContent = `AI purchase review: ${review.verdict}${(review.concerns || []).length ? ` · ${review.concerns.join(" ")}` : ""}`;
    $("decision-meta").textContent = pending.recurring
      ? `Recurring charges always need your approval. Remaining budget: ${money(pending.remaining_budget)}.`
      : review && review.verdict === "ask" && Number(pending.amount) <= Number((state.lastMandate || {}).approval_threshold || Infinity)
        ? `The purchase review asked for your decision. Remaining budget: ${money(pending.remaining_budget)}.`
        : `Above your approval threshold. Remaining budget: ${money(pending.remaining_budget)}.`;
    $("approve-button").disabled = false;
    $("decline-button").disabled = false;
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "center" });
    $("approve-button").focus({ preventScroll: true });
  }
  async function decide(approve) {
    if (!state.runId) return;
    $("approve-button").disabled = true;
    $("decline-button").disabled = true;
    try {
      await api(`/demo/runs/${encodeURIComponent(state.runId)}/decision`, { method: "POST", body: JSON.stringify({ approve }) });
      $("decision").hidden = true;
      state.decisionKey = null;
    } catch (err) {
      showBanner(err.message);
      $("approve-button").disabled = false;
      $("decline-button").disabled = false;
    }
  }

  // --- activity feed ------------------------------------------------------------------------
  function clock(at) {
    if (!at) return "";
    const date = new Date(at);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function codeText(text) { return el("code", { text }); }

  function addAct({ icon, cat = "", kind = "tool", title, detail, preview, at, pill, status }) {
    const body = el("div", {},
      el("p", { class: "act-title" }, title, pill ? el("span", { class: "amount-pill", text: pill }) : null, status ? el("span", { class: "status-pill", "data-tone": status[0], text: status[1], style: "margin-left:6px" }) : null),
      detail ? el("p", { class: "act-detail" }, detail) : null,
      preview ? el("pre", { class: "act-preview", text: preview }) : null,
    );
    const item = el("li", { class: "act", "data-cat": cat, "data-kind": kind }, el("span", { class: "act-icon", "aria-hidden": "true", text: icon }), body, el("span", { class: "act-time", text: clock(at) }));
    const list = $("activity");
    list.append(item);
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 400;
    if (nearBottom) item.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return item;
  }

  const TOOL_META = {
    discover_offers: ["🧭", "Reading the site's offer catalog"],
    check_mandate: ["👛", "Checking your spending mandate"],
    fetch_page: ["↗", "Requesting a page"],
    read_policy: ["§", "Reading the crawling policy"],
    pay_deposit: ["$", "Paying the refundable crawl deposit"],
    accept_policy: ["✓", "Accepting the crawling policy"],
    wait: ["⏳", "Waiting for the rate limit"],
    complete_session: ["■", "Completing the crawl session"],
    buy_access: ["▶", "Buying pay-per-access item"],
    subscribe: ["∞", "Subscribing"],
    cancel_subscription: ["✕", "Cancelling subscription"],
    purchase_products: ["◫", "Placing a product order"],
    open_paid_content: ["📖", "Opening paid content"],
  };

  function callTitle(name, input) {
    const [, label] = TOOL_META[name] || ["•", name];
    const args = input || {};
    if (name === "fetch_page" || name === "open_paid_content") return [label, " ", codeText(args.path || "")];
    if (name === "buy_access") return [label, " ", codeText(args.item_id || "")];
    if (name === "subscribe") return [label, " to ", codeText(args.plan_id || "")];
    if (name === "purchase_products") return [label, ": ", (args.items || []).map((i) => `${i.quantity}× ${i.sku}`).join(", ")];
    if (name === "wait") return [`${label} (${args.seconds}s)`];
    return [label];
  }

  function describeResult(name, result, isError) {
    if (isError) return { cat: "bad", status: ["bad", "Refused"], detail: result.error || "Error" };
    switch (name) {
      case "discover_offers": {
        const o = result.offers || {};
        return { cat: "good", detail: `${(o.pay_per_access || []).length} pay-per-access items, ${(o.subscriptions || []).length} subscription plan, ${(o.products || []).length} products, ${((o.crawl_deposits || {}).tiers || []).length} deposit tiers. Payments: ${(result.payments || {}).processor || "PayPal"} (${(result.payments || {}).mode || ""}).` };
      }
      case "check_mandate": {
        const m = result.mandate || {};
        return { detail: `Budget ${money(m.budget)}, remaining ${money(m.remaining)}, approval above ${money(m.approval_threshold)}.` };
      }
      case "fetch_page":
        if (result.status === 402) return { cat: "pay", status: ["warn", "402 Payment required"], detail: `Deposit ${money((result.deposit || {}).amount)} (${((result.pricing || {}).tier_name) || "base"} tier), refundable.` };
        if (result.status === 200) return { cat: "good", status: ["good", "200"], preview: stripTags(result.content), detail: `Budget left ${result.remaining_session_budget}, window left ${result.remaining_window_requests}.` };
        return { cat: "bad", status: ["bad", `${result.status} blocked`], detail: `${result.reason_code || ""} ${result.reason || ""}` };
      case "read_policy": {
        const p = result.policy || {};
        return { detail: `Policy ${p.policy_version}: allowed ${(p.allowed_paths || []).join(", ")}; rate limit ${p.rate_limit_requests} per ${p.rate_limit_window_seconds}s.` };
      }
      case "pay_deposit":
        return { cat: "pay", pill: money(result.required_amount), status: ["info", result.payment_status || result.status], detail: `Deposit ${result.deposit_id || ""} held through PayPal (${result.payment_mode}).` };
      case "accept_policy":
        return { cat: "good", detail: `Session ${result.session_id} started: ${result.max_requests_per_session} requests max.` };
      case "wait":
        return { detail: `Waited ${result.waited_seconds}s.` };
      case "complete_session": {
        const good = ["REFUNDED", "RELEASED"].includes(result.deposit_state);
        return { cat: good ? "good" : "bad", status: [good ? "good" : "bad", result.deposit_state || "done"], detail: result.decision_reason || "" };
      }
      case "buy_access":
      case "purchase_products":
        return { cat: "pay", pill: money(result.amount), status: ["good", "PAID"], detail: [`PayPal order `, codeText(result.paypal_order_id || "-"), ` · capture `, codeText(result.paypal_capture_id || "-"), result.fulfillment_status ? ` · ${result.fulfillment_status.toLowerCase()}` : "", Number(result.shipping) > 0 ? ` · shipping ${money(result.shipping)}` : ""] };
      case "subscribe":
        return { cat: "pay", pill: `${money(result.amount)} / ${result.billing}`, status: ["good", "ACTIVE"], detail: ["PayPal subscription ", codeText(result.paypal_subscription_id || "-"), ` unlocks ${result.unlocks_prefix}`] };
      case "cancel_subscription":
        return { status: ["warn", result.status || "CANCELLED"] };
      case "open_paid_content":
        if (result.status === 200) return { cat: "good", status: ["good", "Unlocked"], preview: stripTags(result.content) };
        return { cat: "bad", status: ["bad", `${result.status}`], detail: result.error || "" };
      default:
        return { detail: JSON.stringify(result).slice(0, 200) };
    }
  }
  function stripTags(text) { return String(text || "").replace(/<\/?untrusted_page_content>/g, "").trim().slice(0, 700); }

  function renderEvent(event) {
    const data = event.data || {};
    switch (event.kind) {
      case "run_started":
        addAct({ icon: "✦", cat: "pay", kind: "say", title: `Aria received your request`, detail: data.task, at: event.at });
        break;
      case "thinking":
        addAct({ icon: "…", kind: "thinking", title: "Reasoning", detail: data.text, at: event.at });
        break;
      case "text":
        addAct({ icon: "✎", kind: "say", title: "Agent", detail: data.text, at: event.at });
        break;
      case "tool_call": {
        const [icon] = TOOL_META[data.name] || ["•"];
        const item = addAct({ icon, title: callTitle(data.name, data.input), at: event.at });
        item.classList.add("pending");
        if (data.input && data.input.reason) item.querySelector("div").append(el("p", { class: "act-detail", text: `Reason: ${data.input.reason}` }));
        state.pending.set(data.name, item);
        break;
      }
      case "tool_result": {
        const item = state.pending.get(data.name);
        const info = describeResult(data.name, data.result || {}, data.is_error);
        if (item) {
          state.pending.delete(data.name);
          item.classList.remove("pending");
          if (info.cat) item.dataset.cat = info.cat;
          const body = item.querySelector("div");
          const title = body.querySelector(".act-title");
          if (info.pill) title.append(el("span", { class: "amount-pill", text: info.pill }));
          if (info.status) title.append(el("span", { class: "status-pill", "data-tone": info.status[0], text: info.status[1], style: "margin-left:6px" }));
          if (info.detail) body.append(el("p", { class: "act-detail" }, info.detail));
          if (info.preview) body.append(el("pre", { class: "act-preview", text: info.preview }));
          if (data.result && data.result.prompt_injection) body.append(el("p", { class: "act-detail", text: "⚠ This page contains instructions aimed at AI agents. The agent treats page content as untrusted data." }));
        }
        break;
      }
      case "purchase_review": {
        const tone = data.verdict === "allow" ? "good" : data.verdict === "block" ? "bad" : "ask";
        const label = data.verdict === "allow" ? "Review: looks right" : data.verdict === "block" ? "Review: blocked" : "Review: ask the user";
        addAct({ icon: "⚖", cat: tone, title: [`Purchase review: ${data.title}`], pill: money(data.amount), status: [data.verdict === "allow" ? "good" : data.verdict === "block" ? "bad" : "warn", label], detail: (data.concerns || []).join(" ") || null, at: event.at });
        break;
      }
      case "decision_required":
        addAct({ icon: "?", cat: "ask", title: [`Asked you to approve: ${data.title}`], pill: money(data.amount), detail: data.reason ? `Reason: ${data.reason}` : null, at: event.at });
        break;
      case "decision_done": {
        const tone = data.outcome === "approved" ? "good" : "bad";
        addAct({ icon: data.outcome === "approved" ? "✓" : "✕", cat: tone, title: `You ${data.outcome === "approved" ? "approved" : data.outcome === "timeout" ? "did not answer (declined)" : "declined"}: ${data.title}`, at: event.at });
        break;
      }
      case "approval_required":
        addAct({ icon: "P", cat: "ask", title: "PayPal approval needed", detail: "Approve the order or subscription at PayPal to continue.", at: event.at });
        break;
      case "approval_done":
        addAct({ icon: "P", cat: data.outcome === "timeout" ? "bad" : "good", title: `PayPal approval: ${data.outcome}`, at: event.at });
        break;
      case "stop":
        addAct({ icon: "■", cat: "bad", title: "Agent stopped", detail: data.reason, at: event.at });
        break;
      default:
        break;
    }
  }

  // --- wallet -------------------------------------------------------------------------------
  const STATUS_TONE = { PAID: "good", ACTIVE: "good", REFUNDED: "good", RELEASED: "good", HELD: "info", FORFEITED: "bad", DECLINED_BY_USER: "bad", CANCELLED: "warn", BLOCKED: "bad", BLOCKED_BY_REVIEW: "bad" };
  const STATUS_LABEL = { DECLINED_BY_USER: "Declined by you", HELD: "Held (refundable)", BLOCKED: "Blocked by mandate", BLOCKED_BY_REVIEW: "Blocked by review" };
  function renderWallet(mandate) {
    const list = $("purchases");
    clear(list);
    const tally = $("type-tally");
    clear(tally);
    const counts = { deposit: 0, access: 0, subscription: 0, product: 0 };
    state.lastMandate = mandate;
    if (!mandate) {
      ["w-budget", "w-spent", "w-remaining"].forEach((id) => { $(id).textContent = "-"; });
      $("w-meter").style.width = "0";
      list.append(el("li", { class: "empty", text: "No payments yet." }));
    } else {
      $("w-budget").textContent = money(mandate.budget);
      $("w-spent").textContent = money(mandate.spent_net);
      $("w-remaining").textContent = money(mandate.remaining);
      const pct = Math.min(100, Math.max(0, (Number(mandate.spent_net) / Math.max(0.01, Number(mandate.budget))) * 100));
      $("w-meter").style.width = `${pct}%`;
      const purchases = mandate.purchases || [];
      if (!purchases.length) list.append(el("li", { class: "empty", text: "No payments yet." }));
      for (const p of purchases) {
        const notPaid = p.status === "DECLINED_BY_USER" || String(p.status).startsWith("BLOCKED");
        if (!notPaid && counts[p.type] !== undefined) counts[p.type] += 1;
        const ref = p.paypal_subscription_id || p.paypal_capture_id || p.paypal_order_id || p.ref;
        list.append(el("li", { class: "purchase", "data-status": p.status },
          el("span", { class: "purchase-title", text: p.title }),
          el("span", { class: "purchase-amount", text: `${money(p.amount)}${p.recurring ? "/mo" : ""}` }),
          el("span", { class: "purchase-meta" },
            el("span", { class: "type-tag", text: TYPE_LABEL[p.type] || p.type }),
            el("span", { class: "status-pill", "data-tone": STATUS_TONE[p.status] || "info", text: STATUS_LABEL[p.status] || p.status }),
            ref ? codeText(ref) : null),
          p.note ? el("span", { class: "purchase-note", text: p.note }) : null,
        ));
      }
    }
    const ICONS = { deposit: "↺", access: "▶", subscription: "∞", product: "◫" };
    for (const type of Object.keys(counts)) {
      tally.append(el("li", { "data-used": String(counts[type] > 0) },
        el("span", { class: "tt-icon", "aria-hidden": "true", text: ICONS[type] }),
        el("span", { text: TYPE_LABEL[type] }),
        el("span", { class: "tt-count", text: counts[type] ? `${counts[type]} used` : "not used" })));
    }
  }

  // --- report (a small, safe Markdown renderer: text is escaped by building DOM nodes) ----
  function inline(text) {
    const parts = [];
    const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    let match;
    while ((match = re.exec(text))) {
      if (match.index > last) parts.push(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      parts.push(token.startsWith("**") ? el("strong", { text: token.slice(2, -2) }) : el("code", { text: token.slice(1, -1) }));
      last = match.index + token.length;
    }
    if (last < text.length) parts.push(document.createTextNode(text.slice(last)));
    return parts;
  }
  function renderMarkdown(src) {
    const root = el("div", { class: "report-body" });
    const lines = String(src || "").replace(/\r/g, "").split("\n");
    let list = null;
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        list = null;
        const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const table = el("table");
        table.append(el("thead", {}, el("tr", {}, cells(line).map((c) => el("th", {}, inline(c))))));
        const tbody = el("tbody");
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { tbody.append(el("tr", {}, cells(lines[i]).map((c) => el("td", {}, inline(c))))); i += 1; }
        i -= 1;
        table.append(tbody);
        root.append(el("div", { class: "table-wrap" }, table));
        continue;
      }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { list = null; root.append(el("hr")); continue; }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) { list = null; root.append(el(heading[1].length <= 2 ? "h3" : "h4", {}, inline(heading[2]))); continue; }
      const bullet = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
      if (bullet) {
        if (!list) { list = el("ul"); root.append(list); }
        list.append(el("li", {}, inline(bullet[1])));
        continue;
      }
      list = null;
      if (line.trim()) root.append(el("p", {}, inline(line)));
    }
    return root;
  }
  function renderReport(snap) {
    const report = $("report");
    clear(report);
    const failed = snap.status !== "completed";
    report.dataset.tone = failed ? "bad" : "good";
    report.append(el("div", { class: "report-head" },
      el("h3", { text: failed ? (snap.status === "cancelled" ? "Run stopped" : "Run failed") : "Aria's report" }),
      el("span", { class: "status-pill", "data-tone": failed ? "bad" : "good", text: snap.status })));
    if (snap.final_text) report.append(renderMarkdown(snap.final_text));
    else report.append(el("p", { text: snap.error || "The agent finished without a written report." }));
    const usage = snap.usage || {};
    if (usage.turns) report.append(el("p", { class: "usage-line", text: `${usage.turns} model turns · ${usage.input_tokens} input / ${usage.output_tokens} output tokens · ${snap.model || ""}` }));
    report.hidden = false;
    report.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // --- wiring -------------------------------------------------------------------------------
  function init() {
    initTheme();
    initAdmin();
    initPresets();
    renderWallet(null);
    $("start-button").addEventListener("click", startRun);
    $("stop-button").addEventListener("click", async () => {
      if (!state.runId) return;
      try { await api(`/demo/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" }); } catch (err) { showBanner(err.message); }
    });
    $("approve-button").addEventListener("click", () => decide(true));
    $("decline-button").addEventListener("click", () => decide(false));
    $("paypal-open").addEventListener("click", () => { if (state.approvalUrl) window.open(state.approvalUrl, "_blank", "noopener"); });
    $("paypal-done").addEventListener("click", async () => {
      if (!state.runId) return;
      try { await api(`/demo/runs/${encodeURIComponent(state.runId)}/approval`, { method: "POST" }); } catch (err) { showBanner(err.message); }
    });
    $("show-thinking").addEventListener("change", (event) => { $("activity").classList.toggle("with-thinking", event.target.checked); });
    loadCatalog();
    loadConfig();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
