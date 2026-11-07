/* SARA agent studio.
   Right: an ordinary chat assistant (Aria). The person types a request; the assistant's
   steps, approval requests and final answer appear as chat messages.
   Left: what happens behind the scenes on the website (SARA) and at PayPal, step by step.
   Talks to /demo/* (runs) and /.well-known/agent-commerce.json (the site's offer catalog). */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const POLL_MS = 1000;
  const state = {
    token: null, runId: null, after: 0, timer: null, currency: "USD", config: null,
    catalog: null, merchant: "this website", names: {},
    log: null, steps: null, pending: new Map(), stepCount: 0, startedAt: null,
    askKey: null, askCard: null, paypalCard: null, approvalUrl: null,
    crawl: { pages: 0, blocked: 0, waits: 0 }, lastMandate: null,
  };

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
      else if (key.startsWith("data-") || key.startsWith("aria-") || key === "role" || key === "title" || key === "type" || key === "id") node.setAttribute(key, value);
      else node[key] = value;
    }
    for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
    return node;
  }
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
  const code = (text) => el("code", { text });

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
      const detail = data && (data.detail || data);
      const message = (detail && (detail.message || detail.error)) || (data && (data.message || data.error)) || `HTTP ${resp.status}`;
      const error = new Error(message);
      error.status = resp.status;
      throw error;
    }
    return data;
  }

  function showBanner(text) { const b = $("banner"); clear(b); b.append(el("span", { text })); b.hidden = false; }
  function hideBanner() { $("banner").hidden = true; }

  // --- theme and admin token ------------------------------------------------------------------
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

  // --- limits popover --------------------------------------------------------------------------
  function limits() {
    const budget = Math.max(1, Math.min(200, Number($("budget").value) || 60));
    const threshold = Math.max(0, Math.min(200, Number($("threshold").value) || 0));
    return { budget, threshold };
  }
  function updateLimitsSummary() {
    const { budget, threshold } = limits();
    const short = (n) => money(n).replace(/\.00(?!\d)/, "");
    $("limits-summary").textContent = `${short(budget)} budget · asks above ${short(threshold)}`;
  }
  function initLimits() {
    const pop = $("limits-pop");
    const toggle = $("limits-toggle");
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      pop.hidden = !pop.hidden;
      toggle.setAttribute("aria-expanded", String(!pop.hidden));
    });
    document.addEventListener("click", (event) => {
      if (!pop.hidden && !pop.contains(event.target) && event.target !== toggle) { pop.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
    });
    ["budget", "threshold"].forEach((id) => $(id).addEventListener("input", updateLimitsSummary));
    $("show-thinking").addEventListener("change", (event) => {
      document.querySelectorAll(".steps").forEach((list) => list.classList.toggle("with-thinking", event.target.checked));
    });
    updateLimitsSummary();
  }

  // --- suggestions: what a person would actually ask (they don't know the site's catalog) ------
  function suggestions() {
    const m = state.merchant;
    return [
      { tag: "Read and buy", text: `Catch me up on today's news from ${m}, and get me whatever they have on how AI agents pay websites: videos, guides, anything useful.` },
      { tag: "Just look", text: `What does ${m} sell to AI assistants? Tell me what's worth it, but don't buy anything.` },
      { tag: "Subscribe (this page hides a trap)", text: `Sign me up for the members feed on ${m} if it's under $10 a month, and summarise today's feed.` },
      { tag: "Shopping", text: `Order two ${m} mugs for my team and tell me the total with shipping.` },
    ];
  }
  function renderSuggestions() {
    const holder = $("suggestions");
    clear(holder);
    holder.append(el("p", { class: "scenario-head", text: "Try asking" }));
    for (const s of suggestions()) {
      const button = el("button", { type: "button", class: "suggestion" }, el("small", { text: s.tag }), s.text);
      button.addEventListener("click", () => { $("task").value = s.text; autosize(); send(); });
      holder.append(button);
    }
    holder.append(el("p", { class: "scenario-head", text: "Or watch another agent visit the site" }));
    const rogue = el("button", { type: "button", class: "suggestion rogue-pick" },
      el("small", { text: "A rule-breaking scraper (scripted red-team bot)" }),
      "Pays the deposit without reading the rules, goes for admin pages and private data, then floods the site with requests. See what SARA does.");
    rogue.addEventListener("click", () => sendRogue());
    holder.append(rogue);
  }

  // --- offer catalog (left) --------------------------------------------------------------------
  function offerCard(type, icon, name, desc, rows) {
    return el("article", { class: "offer", "data-type": type },
      el("div", { class: "offer-head" }, el("span", { class: "offer-icon", "aria-hidden": "true", text: icon }), el("span", { class: "offer-name", text: name })),
      el("p", { class: "offer-desc", text: desc }),
      el("ul", { class: "offer-list" }, rows.map(([label, price]) => el("li", {}, el("span", { text: label, title: label }), el("span", { class: "price", text: price })))));
  }
  async function loadCatalog() {
    try {
      const catalog = await api("/.well-known/agent-commerce.json");
      state.catalog = catalog;
      state.currency = catalog.currency || "USD";
      state.merchant = catalog.merchant || "this website";
      document.querySelectorAll("[data-currency]").forEach((node) => {
        try { node.textContent = (0).toLocaleString(undefined, { style: "currency", currency: state.currency }).replace(/[\d.,\s]/g, "") || state.currency; } catch (_) { node.textContent = state.currency; }
      });
      $("merchant-name").textContent = state.merchant;
      const offers = catalog.offers || {};
      for (const i of offers.pay_per_access || []) state.names[i.item_id] = i.title;
      for (const p of offers.subscriptions || []) state.names[p.plan_id] = p.name;
      for (const p of offers.products || []) state.names[p.sku] = p.name;
      const grid = $("offer-grid");
      clear(grid);
      grid.append(
        offerCard("deposit", "↺", "Refundable crawl deposit", "To read protected pages under the site's rules. Returned in full if the agent follows them.",
          ((offers.crawl_deposits || {}).tiers || []).map((t) => [`${t.name} content`, money(t.price)])),
        offerCard("access", "▶", "Pay-per-access", "A one-off charge unlocks one video, dataset, or audio item.",
          (offers.pay_per_access || []).map((i) => [i.title, money(i.price)])),
        offerCard("subscription", "∞", "Subscriptions", "A recurring PayPal subscription for members-only pages.",
          (offers.subscriptions || []).map((p) => [`${p.name} (${p.billing})`, money(p.price)])),
        offerCard("product", "◫", "Products", "Digital downloads and shipped goods, in one itemized PayPal order.",
          (offers.products || []).map((p) => [p.name, money(p.price)])),
      );
      updateLimitsSummary();
      if (!state.runId && !$("messages").querySelector(".msg.user")) renderSuggestions();
    } catch (err) {
      clear($("offer-grid"));
      $("offer-grid").append(el("p", { class: "empty", text: `Could not load the offer catalog: ${err.message}` }));
      renderSuggestions();
    }
  }
  const nameOf = (id) => state.names[id] || id;

  async function loadConfig() {
    try {
      const config = await api("/demo/config");
      state.config = config;
      const badges = $("badges");
      clear(badges);
      const badge = (tone, text) => el("span", { class: "badge" }, el("span", { class: "badge-dot", "data-tone": tone }), text);
      badges.append(
        badge(config.payment_mode === "mock" ? "warning" : "good", config.payment_mode === "mock" ? "PayPal: simulated (mock mode)" : "Live on the PayPal Sandbox"),
        badge(config.ai_available ? "accent" : "warning", config.ai_available ? `Assistant: ${config.ai_provider === "openai" ? "OpenAI" : "Claude"} (${config.ai_model})` : "Assistant: no AI key configured"),
        badge("good", `Deposits settle by ${config.settlement_mode === "authorization" ? "PayPal hold" : "capture and refund"}`),
      );
      let hint = "Aria can make mistakes. It can only spend within your limits.";
      state.blocked = null;
      state.runBlocked = config.can_start_runs ? null : "This deployment is private: enter the admin token (top right) to chat.";
      if (state.runBlocked) state.blocked = state.runBlocked;
      else if (!config.ai_available) state.blocked = "Set OPENAI_API_KEY or ANTHROPIC_API_KEY on the server to chat with the assistant. (The rule-breaking scraper still works.)";
      else if (config.runs_remaining_this_hour !== undefined) hint = `${hint} ${config.runs_remaining_this_hour} chats left this hour on this demo.`;
      $("launch-hint").textContent = state.blocked || hint;
      syncComposer();
      if (!state.runId && config.active_run_id) attach(config.active_run_id, null, true);
      if (config.paypal_js_client_id && !state.sdkLoaded) initHumanCheckout(config.paypal_js_client_id);
    } catch (err) {
      showBanner(`Could not load the demo configuration: ${err.message}`);
    }
  }

  // --- human checkout: PayPal JS SDK v6 (Sandbox mode only) -------------------------------------
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
    const catalog = state.catalog || await api("/.well-known/agent-commerce.json");
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

  // --- chat: messages ----------------------------------------------------------------------------
  function scrollChat(force) {
    const box = $("chat-scroll");
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
    if (force || near) box.scrollTop = box.scrollHeight;
  }
  function addMessage(role, content, extraClass) {
    const bubble = el("div", { class: "bubble" }, content);
    const who = role === "ai" && state.runId && state.mode === "rogue" ? " rogue" : "";
    const item = el("li", { class: `msg ${role}${who}${extraClass ? ` ${extraClass}` : ""}` }, bubble);
    $("messages").append(item);
    scrollChat(true);
    return bubble;
  }
  function ensureLog() {
    if (state.log) return state.steps;
    const summaryText = el("span", { text: "Working…" });
    const steps = el("ol", { class: `steps${$("show-thinking").checked ? " with-thinking" : ""}` });
    const details = el("details", { class: "worklog", open: true }, el("summary", {}, el("span", { class: "spinner", "aria-hidden": "true" }), summaryText), steps);
    details.summaryText = summaryText;
    addMessage("ai", details);
    state.log = details;
    state.steps = steps;
    return steps;
  }
  function closeLog(label) {
    if (!state.log) return;
    state.log.dataset.done = "true";
    state.log.summaryText.textContent = label;
    state.log.open = false;
    state.log = null;
    state.steps = null;
  }
  function addStep({ icon = "•", text, sub, tone, kind, preview }) {
    const steps = ensureLog();
    const body = el("span", { class: "step-text" }, text, sub ? el("span", { class: "step-sub" }, sub) : null, preview ? el("span", { class: "step-preview", text: preview }) : null);
    const item = el("li", { class: "step", "data-tone": tone || null, "data-kind": kind || null }, el("span", { class: "step-icon", "aria-hidden": "true", text: icon }), body);
    steps.append(item);
    if (kind !== "thinking") state.stepCount += 1;
    if (state.log) state.log.summaryText.textContent = `Working… (${state.stepCount} steps)`;
    scrollChat();
    return item;
  }
  function finishStep(item, { tone, sub, preview, icon }) {
    item.classList.remove("pending");
    if (tone) item.dataset.tone = tone;
    if (icon) item.querySelector(".step-icon").textContent = icon;
    const body = item.querySelector(".step-text");
    if (sub) body.append(el("span", { class: "step-sub" }, sub));
    if (preview) body.append(el("span", { class: "step-preview", text: preview }));
    scrollChat();
  }

  // --- the left-hand flow ---------------------------------------------------------------------------
  const STAGES = ["discover", "rules", "deposit", "crawl", "buy", "settle"];
  function stage(name, value) {
    const node = document.querySelector(`.stage[data-stage="${name}"]`);
    if (!node) return;
    if (value === "active") {
      document.querySelectorAll('.stage[data-state="active"]').forEach((other) => { if (other !== node) other.dataset.state = "done"; });
    }
    if (node.dataset.state === "bad" && value === "done") return;
    node.dataset.state = value;
  }
  function live(name, ...content) {
    const node = document.querySelector(`[data-live="${name}"]`);
    if (!node) return;
    clear(node);
    node.append(...content.flat(2).filter((c) => c !== null && c !== undefined && c !== ""));
  }
  function liveItem(name, ...content) {
    const node = document.querySelector(`[data-live="${name}"]`);
    if (node) node.append(el("li", {}, ...content.flat(2).filter((c) => c !== null && c !== undefined && c !== "")));
  }
  function resetFlow() {
    STAGES.forEach((name) => { const node = document.querySelector(`.stage[data-stage="${name}"]`); if (node) delete node.dataset.state; });
    document.querySelectorAll("[data-live]").forEach(clear);
    state.crawl = { pages: 0, blocked: 0, waits: 0 };
  }
  function crawlLive() {
    const c = state.crawl;
    live("crawl", `${c.pages} page${c.pages === 1 ? "" : "s"} served`, c.blocked ? ` · ${c.blocked} blocked` : " · 0 blocked", c.waits ? ` · slowed down ${c.waits}× for the rate limit` : "");
  }

  // --- tool steps: chat wording + the left-hand flow -------------------------------------------------
  const pageText = (text) => String(text || "").replace(/<\/?untrusted_page_content>/g, "").replace(/\s+\n/g, "\n").trim().slice(0, 500);

  function onToolCall(name, args) {
    args = args || {};
    const m = state.merchant;
    const map = {
      discover_offers: ["🧭", `Looking at what ${m} offers agents`],
      check_mandate: ["👛", "Checking my spending limits"],
      fetch_page: ["↗", ["Opening ", code(args.path || "/")]],
      read_policy: ["§", "Reading the site's rules"],
      pay_deposit: ["$", "Paying the refundable deposit with PayPal"],
      accept_policy: ["✓", "Agreeing to the site's rules"],
      wait: ["⏳", `Waiting ${args.seconds || ""}s (the site's rate limit)`],
      complete_session: ["■", `Finishing my session with ${m}`],
      buy_access: ["▶", `Buying “${nameOf(args.item_id)}”`],
      subscribe: ["∞", `Subscribing to ${nameOf(args.plan_id)}`],
      cancel_subscription: ["✕", "Cancelling the subscription"],
      purchase_products: ["◫", ["Ordering ", (args.items || []).map((i) => `${i.quantity}× ${nameOf(i.sku)}`).join(", ")]],
      open_paid_content: ["📖", ["Opening ", code(args.path || "")]],
    };
    const [icon, text] = map[name] || ["•", name];
    const item = addStep({ icon, text });
    item.classList.add("pending");
    state.pending.set(name, item);

    if (name === "discover_offers") stage("discover", "active");
    if (name === "pay_deposit") stage("deposit", "active");
    if (name === "accept_policy" || (name === "fetch_page" && state.inSession)) stage("crawl", "active");
    if (["buy_access", "subscribe", "purchase_products"].includes(name)) stage("buy", "active");
    if (name === "complete_session") stage("settle", "active");
  }

  function onToolResult(name, result, isError) {
    result = result || {};
    const item = state.pending.get(name);
    state.pending.delete(name);
    const done = (info) => { if (item) finishStep(item, info); };

    if (isError) {
      done({ tone: "bad", icon: "✕", sub: result.error || "Not allowed" });
      if (["buy_access", "subscribe", "purchase_products"].includes(name)) liveItem("buy", el("span", { class: "status-pill", "data-tone": "bad", text: "NOT BOUGHT" }), " ", String(result.error || "").slice(0, 160));
      return;
    }
    switch (name) {
      case "discover_offers": {
        const o = result.offers || {};
        const tiers = ((o.crawl_deposits || {}).tiers || []).map((t) => money(t.price)).join(" / ");
        done({ tone: "good", sub: `Found its agent catalog: ${(o.pay_per_access || []).length} items, ${(o.subscriptions || []).length} subscription, ${(o.products || []).length} products, deposits ${tiers}.` });
        stage("discover", "done");
        live("discover", "Read ", code("/.well-known/agent-commerce.json"), `: deposit tiers ${tiers}; ${(o.pay_per_access || []).length} pay-per-access items, ${(o.subscriptions || []).length} plan, ${(o.products || []).length} products. All paid through PayPal.`);
        break;
      }
      case "check_mandate": {
        const md = result.mandate || {};
        done({ sub: `Budget ${money(md.budget)}, ${money(md.remaining)} left; I ask you above ${money(md.approval_threshold)}.` });
        break;
      }
      case "fetch_page":
        if (result.status === 402) {
          const amount = (result.deposit || {}).amount;
          const tier = (result.pricing || {}).tier_name || "base";
          const id = result.identity || {};
          const base = (result.pricing || {}).price;
          const rec = id.record || {};
          const why = id.standing === "trusted"
            ? `half the usual ${money(base)}: I sign my requests, and my record here is clean (${rec.clean} deposit${rec.clean === 1 ? "" : "s"} returned)`
            : id.standing === "flagged" ? `raised from ${money(base)}: a deposit of mine was kept before` : null;
          done({ tone: "pay", icon: "$", sub: `The site asks for a refundable ${money(amount)} deposit first (${tier} content${why ? `; ${why}` : ""}).` });
          stage("rules", "done");
          live("rules", code(`402 Payment Required`), ` for ${(result.pricing || {}).resource || "the page"}: ${tier} content, refundable deposit ${money(amount)}.`,
            id.verified ? [el("br"), `Verified agent “${id.agent}” (${id.operator}), signed with Web Bot Auth. `, id.standing === "trusted" ? `Clean record (${rec.clean} returned, ${rec.forfeited || 0} kept), so it pays half the ${money(base)} price.` : id.standing === "flagged" ? `It has ${rec.forfeited} kept deposit(s), so it pays more.` : "No record yet, so it pays the standard price."] : [el("br"), "Unsigned agent: standard price."]);
        } else if (result.status === 200) {
          state.crawl.pages += 1;
          done({ tone: "good", icon: "✓", sub: "Read it.", preview: pageText(result.content) });
          stage("crawl", "active");
          crawlLive();
        } else if (![401, 403, 429].includes(result.status)) {
          done({ icon: "–", sub: `Not found on the site (${result.status}). Not a rule break.` });
        } else {
          state.crawl.blocked += 1;
          done({ tone: "bad", icon: "✕", sub: `Blocked by the site: ${result.status} ${result.reason || result.reason_code || ""}` });
          stage("crawl", "bad");
          crawlLive();
        }
        break;
      case "read_policy": {
        const p = result.policy || {};
        done({ sub: `Allowed: ${(p.allowed_paths || []).join(", ")}. At most ${p.rate_limit_requests} requests per ${p.rate_limit_window_seconds}s.` });
        const rules = document.querySelector('[data-live="rules"]');
        if (rules) rules.append(el("br"), `Rules: allowed ${(p.allowed_paths || []).join(", ")}; disallowed ${(p.disallowed_paths || []).join(", ") || "none"}; ${p.rate_limit_requests} requests per ${p.rate_limit_window_seconds}s; ${p.max_requests_per_session} pages per session.`);
        break;
      }
      case "pay_deposit": {
        const held = result.payment_status === "AUTHORIZED" ? "held by PayPal (nothing charged)" : (result.payment_status || result.status || "paid").toLowerCase();
        done({ tone: "pay", sub: [`${money(result.required_amount)} ${held}`, result.payment_method_label ? `, paid with ${result.payment_method_label}` : "", "."] });
        stage("deposit", "done");
        live("deposit", `${money(result.required_amount)} · `, code(result.deposit_id || "-"), ` · ${result.payment_status === "AUTHORIZED" ? "PayPal authorization (a hold)" : `PayPal ${String(result.payment_status || "payment").toLowerCase()}`}`,
          result.paypal_order_id ? [" · order ", code(result.paypal_order_id)] : "", result.payment_method_label ? ` · paid with ${result.payment_method_label}` : "");
        break;
      }
      case "accept_policy":
        state.inSession = true;
        done({ sub: `Session open: up to ${result.max_requests_per_session} pages.` });
        stage("crawl", "active");
        crawlLive();
        break;
      case "wait":
        state.crawl.waits += 1;
        done({});
        crawlLive();
        break;
      case "complete_session": {
        state.inSession = false;
        const s = result.deposit_state;
        const good = s === "REFUNDED" || s === "RELEASED";
        const words = s === "RELEASED" ? "Deposit returned: PayPal released the hold." : s === "REFUNDED" ? "Deposit refunded through PayPal." : s === "FORFEITED" ? "The site kept the deposit." : `Deposit: ${s || "settling"}`;
        done({ tone: good ? "good" : "bad", icon: good ? "✓" : "✕", sub: words });
        if (state.crawl.blocked === 0) stage("crawl", "done");
        stage("settle", good ? "done" : "bad");
        live("settle", el("span", { class: "status-pill", "data-tone": good ? "good" : "bad", text: s || "SETTLED" }), " ", good ? (s === "RELEASED" ? "The hold was voided: the agent followed every rule, so no money moved." : "Refunded in full: the agent followed every rule.") : (result.decision_reason || "Rules were broken, so the site keeps the deposit."));
        break;
      }
      case "buy_access":
      case "purchase_products": {
        done({ tone: "pay", icon: "✓", sub: [`Paid ${money(result.amount)} with PayPal`, result.paid_with ? ` (${result.paid_with})` : "", Number(result.shipping) > 0 ? `, including ${money(result.shipping)} shipping` : "", "."] });
        liveItem("buy", el("span", { class: "status-pill", "data-tone": "good", text: "PAID" }), " ", el("span", { class: "amount", text: money(result.amount) }), ` ${result.title || nameOf(result.item_id) || "order"} · PayPal order `, code(result.paypal_order_id || "-"), " · capture ", code(result.paypal_capture_id || "-"));
        break;
      }
      case "subscribe":
        done({ tone: "pay", icon: "✓", sub: `Subscribed: ${money(result.amount)} / ${result.billing}.` });
        liveItem("buy", el("span", { class: "status-pill", "data-tone": "good", text: "ACTIVE" }), " ", el("span", { class: "amount", text: `${money(result.amount)}/${result.billing}` }), " PayPal subscription ", code(result.paypal_subscription_id || "-"));
        break;
      case "cancel_subscription":
        done({ sub: result.status || "Cancelled." });
        break;
      case "open_paid_content":
        if (result.status === 200) done({ tone: "good", icon: "✓", sub: "Unlocked.", preview: pageText(result.content) });
        else done({ tone: "bad", icon: "✕", sub: `${result.status}: ${result.error || "not unlocked"}` });
        break;
      default:
        done({});
    }
    if (result.prompt_injection && item) {
      finishStep(item, { sub: "⚠ This page contains instructions aimed at AI assistants. I'm ignoring them." });
    }
  }

  // --- approvals inside the chat ---------------------------------------------------------------------
  const TYPE_LABEL = { deposit: "Refundable deposit", access: "One-off purchase", subscription: "Subscription", product: "Order" };
  function renderAsk(snap) {
    const pending = snap.status === "awaiting_decision" ? snap.pending_decision : null;
    if (!pending) return;
    const key = `${pending.title}|${pending.amount}|${pending.payment_type}`;
    if (state.askKey === key && state.askCard) return;
    state.askKey = key;
    const review = pending.review;
    const why = pending.recurring
      ? "It's a recurring charge, so I always check with you."
      : Number(pending.amount) > Number((state.lastMandate || {}).approval_threshold ?? Infinity)
        ? `It's above your ${money((state.lastMandate || {}).approval_threshold)} limit.`
        : "The purchase check asked me to confirm with you.";
    const approve = el("button", { type: "button", class: "primary", id: "approve-button", text: "Approve" });
    const decline = el("button", { type: "button", id: "decline-button", text: "Decline" });
    const card = el("div", { class: "ask-card" },
      el("p", { class: "ask-kind", text: `${TYPE_LABEL[pending.payment_type] || "Purchase"} · needs your OK` }),
      el("p", { class: "ask-title", text: pending.title }),
      el("p", { class: "ask-amount", text: `${money(pending.amount)}${pending.recurring ? " / month" : ""}` }),
      pending.reason ? el("p", { class: "ask-why", text: `Why I want it: ${pending.reason}` }) : null,
      el("p", { class: "ask-meta", text: `${why}${review ? ` Purchase check: ${review.verdict === "allow" ? "matches your request" : review.verdict}.` : ""} ${money(pending.remaining_budget)} left in your budget.` }),
      el("div", { class: "ask-actions" }, decline, approve));
    approve.addEventListener("click", () => decide(true));
    decline.addEventListener("click", () => decide(false));
    closeLog(`Worked on it (${state.stepCount} steps so far)`);
    addMessage("ai", [el("p", { text: `Before I buy this, can you confirm?` }), card]);
    state.askCard = card;
    liveItem("buy", el("span", { class: "status-pill", "data-tone": "warn", text: "ASKING YOU" }), " ", el("span", { class: "amount", text: money(pending.amount) }), ` ${pending.title}: ${why}`);
    approve.focus({ preventScroll: true });
  }
  function resolveAsk(outcome) {
    const card = state.askCard;
    if (!card) return;
    const actions = card.querySelector(".ask-actions");
    if (actions) actions.remove();
    card.append(el("p", { class: "ask-done", text: outcome === "approved" ? "✓ You approved this." : outcome === "timeout" ? "No answer, so I didn't buy it." : "✕ You declined. I won't buy it." }));
    state.askCard = null;
    state.askKey = null;
  }
  async function decide(approve) {
    if (!state.runId) return;
    const card = state.askCard;
    if (card) card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    try {
      await api(`/demo/runs/${encodeURIComponent(state.runId)}/decision`, { method: "POST", body: JSON.stringify({ approve }) });
      resolveAsk(approve ? "approved" : "declined");
    } catch (err) {
      showBanner(err.message);
      if (card) card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
  }
  function renderPayPalApproval(snap) {
    const waiting = snap.status === "awaiting_approval" && snap.approval_url;
    state.approvalUrl = snap.approval_url || null;
    if (waiting && !state.paypalCard) {
      const open = el("button", { type: "button", class: "primary", text: "Open PayPal" });
      const doneBtn = el("button", { type: "button", text: "I approved it" });
      open.addEventListener("click", () => { if (state.approvalUrl) window.open(state.approvalUrl, "_blank", "noopener"); });
      doneBtn.addEventListener("click", async () => {
        try { await api(`/demo/runs/${encodeURIComponent(state.runId)}/approval`, { method: "POST" }); } catch (err) { showBanner(err.message); }
      });
      state.paypalCard = el("div", { class: "ask-card paypal" },
        el("p", { class: "ask-kind", text: "PayPal" }),
        el("p", { class: "ask-title", text: "Approve the payment at PayPal" }),
        el("p", { class: "ask-why", text: "The site set this payment up at PayPal. Approve it there with your PayPal account (a Sandbox personal account here), then come back and I'll carry on." }),
        el("div", { class: "ask-actions" }, open, doneBtn));
      closeLog(`Worked on it (${state.stepCount} steps so far)`);
      addMessage("ai", state.paypalCard);
    } else if (!waiting && state.paypalCard) {
      const actions = state.paypalCard.querySelector(".ask-actions");
      if (actions) actions.replaceWith(el("p", { class: "ask-done", text: "✓ PayPal approval received." }));
      state.paypalCard = null;
    }
  }

  // --- run lifecycle -------------------------------------------------------------------------------------
  function autosize() { const t = $("task"); t.style.height = "auto"; t.style.height = `${Math.min(160, t.scrollHeight)}px`; }
  function syncComposer() {
    const running = !!state.runId;
    $("stop-button").hidden = !running;
    $("start-button").hidden = running;
    $("start-button").disabled = running || !!state.blocked;
    $("task").disabled = running;
    $("task").placeholder = running ? "Aria is working…" : "Ask Aria to do something…";
  }

  async function send() {
    const task = $("task").value.trim();
    if (state.runId) return;
    if (state.blocked) { showBanner(state.blocked); return; }
    if (task.length < 8) { $("task").focus(); return; }
    const { budget, threshold } = limits();
    hideBanner();
    $("start-button").disabled = true;
    try {
      const body = {
        driver: "ai", persona: "assistant", task,
        budget: budget.toFixed(2), approval_threshold: threshold.toFixed(2),
        purchase_review: $("purchase-review").checked, save_payment_method: $("save-payment-method").checked,
      };
      const run = await api("/demo/runs", { method: "POST", body: JSON.stringify(body) });
      $("task").value = "";
      autosize();
      attach(run.run_id, run, false, task);
    } catch (err) {
      $("start-button").disabled = false;
      showBanner(err.message);
    }
  }

  async function sendRogue() {
    if (state.runId) return;
    if (state.runBlocked) { showBanner(state.runBlocked); return; }
    hideBanner();
    try {
      const run = await api("/demo/runs", { method: "POST", body: JSON.stringify({ driver: "scripted", persona: "rogue" }) });
      state.mode = "rogue";
      attach(run.run_id, run, false, `Show me what happens when a badly behaved scraper visits ${state.merchant}.`);
    } catch (err) {
      showBanner(err.message);
    }
  }

  function startConversation(task) {
    $("suggestions").hidden = true;
    resetFlow();
    renderWallet(null);
    state.pending.clear();
    state.stepCount = 0;
    state.log = null;
    state.inSession = false;
    state.askKey = null;
    state.askCard = null;
    state.paypalCard = null;
    addMessage("user", task);
    ensureLog();
  }

  function attach(runId, initial, resumed, task) {
    state.runId = runId;
    if (initial && initial.persona) state.mode = initial.persona === "rogue" ? "rogue" : "assistant";
    else if (!resumed && state.mode !== "rogue") state.mode = "assistant";
    document.body.dataset.mode = state.mode;
    $("wallet-title").textContent = state.mode === "rogue" ? "The scraper's money" : "Your money";
    state.after = 0;
    state.startedAt = Date.now();
    syncComposer();
    startConversation(task || (initial && initial.task) || "…");
    state.userTaskShown = !!(task || (initial && initial.task));
    if (initial) handleSnapshot(initial);
    clearTimeout(state.timer);
    poll();
  }

  async function poll() {
    if (!state.runId) return;
    try {
      const snap = await api(`/demo/runs/${encodeURIComponent(state.runId)}?after=${state.after}`);
      if (snap.persona && (snap.persona === "rogue") !== (state.mode === "rogue")) {
        state.mode = snap.persona === "rogue" ? "rogue" : "assistant";
        document.body.dataset.mode = state.mode;
      }
      if (!state.userTaskShown && snap.task && state.mode !== "rogue") {
        const first = $("messages").querySelector(".msg.user .bubble");
        if (first) first.textContent = snap.task;
        state.userTaskShown = true;
      }
      handleSnapshot(snap);
      if (["completed", "failed", "cancelled"].includes(snap.status)) { finish(snap); return; }
    } catch (err) {
      if (err.status === 404) { finish(null); return; }
    }
    state.timer = setTimeout(poll, POLL_MS);
  }

  function setRunState(status) {
    const node = $("run-state");
    const map = { starting: ["running", "Starting"], running: ["running", state.mode === "rogue" ? "The scraper is working" : "The assistant is working"], awaiting_decision: ["waiting", "Waiting for your OK (in the chat)"], awaiting_approval: ["waiting", "Waiting for PayPal approval"], completed: ["completed", "Finished"], failed: ["failed", "Failed"], cancelled: ["cancelled", "Stopped"] };
    const [tone, label] = map[status] || ["idle", "Waiting for your request"];
    node.dataset.state = tone;
    $("run-state-text").textContent = label;
  }

  function handleSnapshot(snap) {
    setRunState(snap.status);
    state.lastMandate = snap.mandate || state.lastMandate;
    for (const event of snap.events || []) renderEvent(event);
    if (typeof snap.next_seq === "number") state.after = snap.next_seq;
    if (snap.mandate) renderWallet(snap.mandate);
    else if (snap.deposit && snap.deposit.required_amount) renderDepositOnly(snap.deposit);
    renderAsk(snap);
    renderPayPalApproval(snap);
  }

  function renderEvent(event) {
    const data = event.data || {};
    switch (event.kind) {
      case "thinking":
        addStep({ icon: "…", text: data.text, kind: "thinking" });
        break;
      case "text":
        if (state.mode === "rogue") { addStep({ icon: "»", text: data.text, kind: "note" }); break; }
        closeLog(`Worked on it (${state.stepCount} steps so far)`);
        addMessage("ai", renderMarkdown(data.text), "note");
        break;
      case "tool_call":
        onToolCall(data.name, data.input);
        break;
      case "tool_result":
        onToolResult(data.name, data.result, data.is_error);
        break;
      case "purchase_review": {
        const ok = data.verdict === "allow";
        const check = addStep({ icon: "⚖", tone: ok ? "good" : data.verdict === "block" ? "bad" : null, text: `Purchase check on “${data.title}” (${money(data.amount)})`, sub: ok ? "A second AI confirmed it matches what you asked for." : data.verdict === "block" ? `Blocked: ${(data.concerns || []).join(" ")}` : `Unsure, so I'll ask you. ${(data.concerns || []).join(" ")}` });
        // Show the check before the purchase it is checking.
        const buying = ["buy_access", "subscribe", "purchase_products"].map((n) => state.pending.get(n)).find(Boolean);
        if (buying && buying.parentNode === check.parentNode) buying.before(check);
        stage("buy", "active");
        liveItem("buy", el("span", { class: "status-pill", "data-tone": ok ? "good" : data.verdict === "block" ? "bad" : "warn", text: `AI CHECK: ${String(data.verdict).toUpperCase()}` }), " ", el("span", { class: "amount", text: money(data.amount) }), ` ${data.title}`);
        break;
      }
      case "decision_done":
        if (state.askCard) resolveAsk(data.outcome);
        ensureLog();
        liveItem("buy", el("span", { class: "status-pill", "data-tone": data.outcome === "approved" ? "good" : "bad", text: data.outcome === "approved" ? "YOU APPROVED" : "YOU DECLINED" }), ` ${data.title}`);
        break;
      case "approval_done":
        ensureLog();
        break;
      case "stop":
        addStep({ icon: "■", tone: "bad", text: "Stopped", sub: data.reason });
        break;
      default:
        break;
    }
  }

  function finish(snap) {
    state.runId = null;
    clearTimeout(state.timer);
    const secs = Math.max(1, Math.round((Date.now() - state.startedAt) / 1000));
    const took = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
    closeLog(`Worked for ${took} · ${state.stepCount} steps`);
    if (state.askCard) resolveAsk("timeout");
    if (snap) {
      setRunState(snap.status);
      STAGES.forEach((name) => { const node = document.querySelector(`.stage[data-stage="${name}"]`); if (node && node.dataset.state === "active") node.dataset.state = "done"; });
      if (state.mode === "rogue") {
        renderVerdict(snap);
      } else if (snap.status === "completed" && snap.final_text) {
        const answer = renderMarkdown(snap.final_text);
        answer.classList.add("answer");
        const m = snap.mandate || {};
        const foot = m.budget ? el("p", { class: "answer-foot", text: `Spent ${money(m.spent_net)} net of returned deposits · ${money(m.remaining)} of ${money(m.budget)} left. Every payment is listed on the left under “Your money”.` }) : null;
        addMessage("ai", [answer, foot]);
      } else if (snap.status === "cancelled") {
        addMessage("ai", el("p", { text: "Stopped. Anything I already paid for is listed on the left; an open deposit is settled automatically." }), "note");
      } else {
        addMessage("ai", el("p", { text: `Sorry, I couldn't finish: ${snap.error || "the run failed."}` }), "note");
      }
    }
    syncComposer();
    loadConfig();
    $("task").focus({ preventScroll: true });
  }

  function renderVerdict(snap) {
    const x = snap.explanation;
    const dep = snap.deposit || {};
    const kept = String(dep.deposit_state || "").includes("FORFEIT");
    const body = [];
    body.push(el("p", { class: "verdict-head" }, el("span", { class: "status-pill", "data-tone": kept ? "bad" : "good", text: dep.deposit_state || "SETTLED" }), " ", x ? x.headline : (kept ? "The site kept the deposit." : "The deposit was returned.")));
    if (x && x.summary) body.push(el("p", { text: x.summary }));
    if (x && (x.findings || []).length) body.push(el("ul", {}, x.findings.map((f) => el("li", { text: f.statement }))));
    if (x) body.push(el("p", { class: "answer-foot", text: x.generated_by === "template" ? "Rule-based explanation from the site's audit log." : `Explained by AI (${x.model || x.generated_by}), citing the site's audit log. The rules decided the outcome; the AI only explains it.` }));
    const item = el("li", { class: "msg ai sara" }, el("div", { class: "bubble" }, el("p", { class: "sara-name", text: `${state.merchant} · protected by SARA` }), ...body));
    $("messages").append(item);
    scrollChat(true);
    if (kept) stage("settle", "bad");
    live("settle", el("span", { class: "status-pill", "data-tone": kept ? "bad" : "good", text: dep.deposit_state || "SETTLED" }), " ", x ? x.headline : "");
  }

  // --- wallet (left) -------------------------------------------------------------------------------------------
  const STATUS_TONE = { PAID: "good", ACTIVE: "good", REFUNDED: "good", RELEASED: "good", HELD: "info", FORFEITED: "bad", DECLINED_BY_USER: "bad", CANCELLED: "warn", BLOCKED: "bad", BLOCKED_BY_REVIEW: "bad" };
  const STATUS_LABEL = { DECLINED_BY_USER: "Declined by you", HELD: "Held (refundable)", RELEASED: "Returned (hold released)", REFUNDED: "Refunded", BLOCKED: "Blocked by your limits", BLOCKED_BY_REVIEW: "Blocked by the purchase check" };
  const KIND_LABEL = { deposit: "Deposit", access: "Pay-per-access", subscription: "Subscription", product: "Product order" };
  function renderWallet(mandate) {
    const list = $("purchases");
    clear(list);
    if (!mandate) {
      ["w-budget", "w-spent", "w-remaining"].forEach((id) => { $(id).textContent = "-"; });
      $("w-meter").style.width = "0";
      list.append(el("li", { class: "empty", text: "No payments yet." }));
      return;
    }
    $("w-budget").textContent = money(mandate.budget);
    $("w-spent").textContent = money(mandate.spent_net);
    $("w-remaining").textContent = money(mandate.remaining);
    const pct = Math.min(100, Math.max(0, (Number(mandate.spent_net) / Math.max(0.01, Number(mandate.budget))) * 100));
    $("w-meter").style.width = `${pct}%`;
    const purchases = mandate.purchases || [];
    if (!purchases.length) list.append(el("li", { class: "empty", text: "No payments yet." }));
    for (const p of purchases) {
      const ref = p.paypal_subscription_id || p.paypal_capture_id || p.paypal_order_id || p.ref;
      list.append(el("li", { class: "purchase", "data-status": p.status },
        el("span", { class: "purchase-title", text: p.title }),
        el("span", { class: "purchase-amount", text: `${money(p.amount)}${p.recurring ? "/mo" : ""}` }),
        el("span", { class: "purchase-meta" },
          el("span", { class: "type-tag", text: KIND_LABEL[p.type] || p.type }),
          el("span", { class: "status-pill", "data-tone": STATUS_TONE[p.status] || "info", text: STATUS_LABEL[p.status] || p.status }),
          ref ? code(ref) : null),
        p.note ? el("span", { class: "purchase-note", text: p.note }) : null));
    }
  }

  function renderDepositOnly(dep) {
    const list = $("purchases");
    clear(list);
    const kept = String(dep.deposit_state || "").includes("FORFEIT");
    const returned = ["REFUNDED", "RELEASED"].includes(dep.deposit_state);
    $("w-budget").textContent = "-";
    $("w-spent").textContent = returned ? money(0) : money(dep.required_amount);
    $("w-remaining").textContent = "-";
    $("w-meter").style.width = kept ? "100%" : "0";
    list.append(el("li", { class: "purchase", "data-status": dep.deposit_state },
      el("span", { class: "purchase-title", text: `Refundable crawl deposit (${dep.pricing_tier_name || "base"} content)` }),
      el("span", { class: "purchase-amount", text: money(dep.required_amount) }),
      el("span", { class: "purchase-meta" },
        el("span", { class: "type-tag", text: "Deposit" }),
        el("span", { class: "status-pill", "data-tone": kept ? "bad" : returned ? "good" : "info", text: kept ? "Kept by the site" : (STATUS_LABEL[dep.deposit_state] || dep.deposit_state || "Held") }),
        dep.deposit_id ? code(dep.deposit_id) : null)));
  }

  // --- a small, safe Markdown renderer (text is escaped by building DOM nodes) ----------------------
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
    const root = el("div", {});
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

  // --- new chat ----------------------------------------------------------------------------------------------
  function newChat() {
    if (state.runId) { showBanner("Stop the assistant first, or wait for it to finish."); return; }
    const messages = $("messages");
    clear(messages);
    messages.append(el("li", { class: "msg ai welcome" }, el("div", { class: "bubble" },
      el("p", { text: "Hi! I'm Aria. I can read websites and buy things for you, within the spending limits you set." }),
      el("p", { text: "What would you like me to do?" }))));
    $("suggestions").hidden = false;
    renderSuggestions();
    resetFlow();
    renderWallet(null);
    state.mode = "assistant";
    document.body.dataset.mode = "assistant";
    $("wallet-title").textContent = "Your money";
    setRunState("idle");
    hideBanner();
    $("task").focus();
  }

  // --- wiring ------------------------------------------------------------------------------------------------
  function init() {
    initTheme();
    initAdmin();
    initLimits();
    renderWallet(null);
    renderSuggestions();
    $("composer").addEventListener("submit", (event) => { event.preventDefault(); send(); });
    $("task").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
    });
    $("task").addEventListener("input", autosize);
    $("stop-button").addEventListener("click", async () => {
      if (!state.runId) return;
      try { await api(`/demo/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" }); } catch (err) { showBanner(err.message); }
    });
    $("new-chat").addEventListener("click", newChat);
    syncComposer();
    loadCatalog();
    loadConfig();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
