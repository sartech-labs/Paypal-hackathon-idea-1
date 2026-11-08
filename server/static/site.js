/* Sentinel News: the protected website itself, as a person sees it in a browser.
   Articles come from /website/content (visible article text only), the shop from the same offer
   catalog agents read (/.well-known/agent-commerce.json). People pay with PayPal's buttons
   (JS SDK v6, Sandbox) and approve subscriptions at PayPal. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { currency: "USD", catalog: null, buyerId: `web-${Math.random().toString(16).slice(2, 10)}`, subRef: null };

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "class") node.className = value;
      else node.setAttribute(key, value);
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
    const resp = await fetch(path, Object.assign({}, options, { headers }));
    let data = null;
    try { data = await resp.json(); } catch (_) { data = null; }
    if (!resp.ok) {
      const detail = data && (data.detail || data);
      throw new Error((detail && (detail.message || detail.error)) || `HTTP ${resp.status}`);
    }
    return data;
  }

  function showResult(text, tone) {
    const box = $("buy-result");
    box.textContent = text;
    box.dataset.tone = tone || "";
    box.hidden = false;
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // --- articles ------------------------------------------------------------------------------
  const SECTION = { index: "Front page", general: "World", technology: "Technology", economy: "Economy", "tier:public": "Local", "tier:standard": "Analysis", "tier:premium": "Premium research" };
  function story(article, section, big) {
    return el("article", { class: big ? "story lead-story" : "story" },
      el("p", { class: "kicker", text: section }),
      el(big ? "h3" : "h4", { class: "headline", text: article.headline }),
      el("p", { class: "dek", text: article.text }));
  }
  async function loadContent() {
    try {
      const data = await api("/website/content");
      state.currency = data.currency || state.currency;
      const news = [];
      const reports = [];
      for (const page of data.pages) {
        const section = SECTION[page.category] || page.heading;
        for (const a of page.articles) (page.category === "tier:standard" || page.category === "tier:premium" ? reports : news).push([a, section, page.category]);
      }
      const lead = $("lead");
      clear(lead);
      if (news.length) lead.append(story(news[0][0], news[0][1], true));
      const grid = $("stories");
      clear(grid);
      news.slice(1).forEach(([a, s]) => grid.append(story(a, s)));
      const rgrid = $("report-grid");
      clear(rgrid);
      reports.forEach(([a, s, cat]) => rgrid.append(el("article", { class: `report${cat === "tier:premium" ? " premium" : ""}` },
        el("p", { class: "kicker", text: s }), el("h4", { class: "headline", text: a.headline }), el("p", { class: "dek", text: a.text }))));
      const facts = $("agents-facts");
      clear(facts);
      const tiers = (data.pricing_tiers || []).slice().reverse().map((t) => `${money(t.price)} ${t.name.toLowerCase()}`).join(", ");
      facts.append(
        el("li", {}, el("strong", { text: "Refundable deposit: " }), `${tiers || "priced by content"}. Held with PayPal, returned if the agent follows the rules.`),
        el("li", {}, el("strong", { text: "Rules: " }), "stay on the allowed pages in robots.txt, keep to the rate limit and the pages-per-session budget. No admin pages, no private data."),
        el("li", {}, el("strong", { text: "Verified agents " }), "(Web Bot Auth signatures) with a clean record pay half. Agents that lost deposits pay more, or are turned away."),
      );
    } catch (err) {
      $("site-error").textContent = `Could not load the news: ${err.message}`;
      $("site-error").hidden = false;
    }
  }

  // --- shop --------------------------------------------------------------------------------------
  function shopItem(title, desc, price, extra) {
    const slot = el("div", { class: "pp-slot" });
    const card = el("div", { class: "shop-item" },
      el("div", { class: "shop-text" }, el("strong", { text: title }), el("p", { text: desc })),
      el("div", { class: "shop-buy" }, el("span", { class: "shop-price", text: price }), extra ? el("span", { class: "shop-extra", text: extra }) : null, slot));
    return { card, slot };
  }
  async function loadShop() {
    try {
      const catalog = await api("/.well-known/agent-commerce.json");
      state.catalog = catalog;
      state.currency = catalog.currency || state.currency;
      const offers = catalog.offers || {};
      const access = $("shelf-access");
      const products = $("shelf-products");
      clear(access);
      clear(products);
      state.slots = [];
      for (const item of offers.pay_per_access || []) {
        const { card, slot } = shopItem(item.title, item.description, money(item.price), "one-off");
        access.append(card);
        state.slots.push({ slot, body: { offer_type: "access", item_id: item.item_id }, unlocks: item.unlocks, title: item.title });
      }
      for (const p of offers.products || []) {
        const ship = p.shipping && Number(p.shipping) > 0 ? `+ ${money(p.shipping)} shipping` : (p.fulfillment === "digital" ? "download" : null);
        const { card, slot } = shopItem(p.name, p.description, money(p.price), ship);
        products.append(card);
        state.slots.push({ slot, body: { offer_type: "product", items: [{ sku: p.sku, quantity: 1 }] }, title: p.name });
      }
      const plan = (offers.subscriptions || [])[0];
      if (plan) {
        state.plan = plan;
        $("pro-title").textContent = plan.name;
        if (plan.description) $("pro-desc").textContent = plan.description;
        $("pro-price").textContent = `${money(plan.price)} ${String(plan.billing || "every 1 month").replace(/^every 1 /i, "per ")}`;
        const benefits = $("pro-benefits");
        clear(benefits);
        (plan.benefits || []).forEach((b) => benefits.append(el("li", { text: b })));
      }
    } catch (err) {
      $("site-error").textContent = `Could not load the shop: ${err.message}`;
      $("site-error").hidden = false;
    }
  }

  // PayPal JS SDK v6 buttons (Sandbox only; the demo config says whether they are available).
  async function initPayPal() {
    let config;
    try { config = await api("/demo/config"); } catch (_) { config = {}; }
    const clientId = config.paypal_js_client_id;
    if (!clientId) { $("paypal-note").hidden = false; return; }
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://www.sandbox.paypal.com/web-sdk/v6/core";
      script.async = true;
      script.onload = resolve;
      script.onerror = () => reject(new Error("PayPal SDK did not load"));
      document.head.append(script);
    });
    const sdk = await window.paypal.createInstance({ clientId, components: ["paypal-payments"] });
    const eligibility = await sdk.findEligibleMethods({ currencyCode: state.currency });
    if (!eligibility.isEligible("paypal")) { $("paypal-note").hidden = false; return; }
    for (const entry of state.slots || []) mountButton(sdk, entry);
  }

  function mountButton(sdk, entry) {
    let ourOrderId = null;
    const session = sdk.createPayPalOneTimePaymentSession({
      async onApprove(data) {
        try {
          const paid = await api(`/commerce/orders/${encodeURIComponent(ourOrderId)}/capture`, { method: "POST", body: JSON.stringify({ agent_id: state.buyerId }) });
          if (paid.status !== "PAID") { showResult(`Payment not completed (status ${paid.status}).`, "bad"); return; }
          let preview = "";
          if (entry.unlocks && paid.access_token) {
            const resp = await fetch(`/paid${entry.unlocks}`, { headers: { Authorization: `Bearer ${paid.access_token}` } });
            const text = await resp.text();
            preview = (new DOMParser().parseFromString(text, "text/html").body.textContent || text).trim().slice(0, 700);
          }
          showResult(`Thank you! Paid ${money(paid.amount)} with PayPal for “${entry.title}”.\nPayPal order ${data.orderId} · capture ${paid.provider_capture_id}${preview ? `\n\n${preview}` : ""}`);
        } catch (err) { showResult(`Capture failed: ${err.message}`, "bad"); }
      },
      onCancel() { showResult("Payment cancelled. Nothing was charged.", "bad"); },
      onError(err) { showResult(`PayPal error: ${(err && err.message) || err}`, "bad"); },
    });
    const button = document.createElement("paypal-button");
    button.setAttribute("type", "pay");
    button.addEventListener("click", () => {
      const createOrder = api("/commerce/checkout", {
        method: "POST",
        body: JSON.stringify(Object.assign({ agent_id: state.buyerId, buyer: "human", purpose: "Bought on the website with PayPal" }, entry.body)),
      }).then((order) => { ourOrderId = order.order_id; return { orderId: order.provider_order_id }; });
      session.start({ presentationMode: "auto" }, createOrder).catch((err) => showResult(`Could not start checkout: ${err.message}`, "bad"));
    });
    entry.slot.append(button);
  }

  // --- subscription: approved by the person at PayPal ---------------------------------------
  async function subscribe() {
    if (!state.plan) return;
    const button = $("subscribe-button");
    button.disabled = true;
    try {
      const sub = await api("/commerce/subscriptions", { method: "POST", body: JSON.stringify({ agent_id: state.buyerId, plan_id: state.plan.plan_id, purpose: "Subscribed on the website" }) });
      state.subRef = sub.subscription_ref;
      if (sub.status === "ACTIVE") { subscribed(sub); return; }
      if (sub.approval_url) window.open(sub.approval_url, "_blank", "noopener");
      $("sub-hint").textContent = "Approve the subscription in the PayPal window, then come back and confirm.";
      $("confirm-sub").hidden = false;
    } catch (err) {
      $("sub-hint").textContent = `Could not start the subscription: ${err.message}`;
      button.disabled = false;
    }
  }
  async function confirmSub() {
    if (!state.subRef) return;
    try {
      const sub = await api(`/commerce/subscriptions/${encodeURIComponent(state.subRef)}/activate`, { method: "POST", body: JSON.stringify({ agent_id: state.buyerId }) });
      if (sub.status === "ACTIVE") subscribed(sub);
      else $("sub-hint").textContent = `PayPal says the subscription is ${sub.status}. Approve it at PayPal first.`;
    } catch (err) { $("sub-hint").textContent = `Not active yet: ${err.message}`; }
  }
  function subscribed(sub) {
    $("confirm-sub").hidden = true;
    $("subscribe-button").hidden = true;
    $("sub-hint").textContent = `Welcome to ${state.plan.name}! PayPal subscription ${sub.provider_subscription_id || sub.subscription_ref} is active.`;
  }

  function init() {
    $("mast-date").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
    $("subscribe-button").addEventListener("click", subscribe);
    $("confirm-sub").addEventListener("click", confirmSub);
    loadContent();
    loadShop().then(() => initPayPal()).catch((err) => console.warn("PayPal buttons unavailable", err));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
