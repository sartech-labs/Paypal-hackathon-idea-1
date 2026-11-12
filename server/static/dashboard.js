/* SARA publisher console.
   All values that originate from crawlers (IDs, paths, reasons) are inserted with
   textContent, never innerHTML. Every number shown comes from the gateway's own endpoints:
   /dashboard/data, /demo/requests, /demo/config, and /policies. */
(() => {
  "use strict";

  const REFRESH_MS = 3000;
  const SLOW_FETCH_MS = 400;
  const REQUESTS_EVERY_MS = 6000;
  // Every console page gets the same header: a title and one line saying what it is for.
  const VIEWS = {
    overview: { title: "Overview", sub: "How agents are treating your websites, and what their deposits did." },
    setup: { title: "Website setup", sub: "The websites behind SARA, and the deposit and rules agents must follow on each." },
    playground: { title: "Playground", sub: "Send an AI agent at one of your websites to see what the gateway allows, blocks, and charges." },
    deposits: { title: "Deposits", sub: "Every refundable PayPal deposit, why it was returned or kept, and a check against PayPal's own records." },
    operations: { title: "Agent activity", sub: "Every agent session and request the gateway answered, with the deposit behind it." },
    security: { title: "Violations", sub: "Requests that broke your rules, and the audit log behind each decision." },
    commerce: { title: "Earnings", sub: "Money agents paid this site through PayPal, including deposits kept from rule-breakers." },
    analytics: { title: "Analytics", sub: "Interactive charts of the gateway's records. Click a segment to filter the page." },
    policy: { title: "Policy", sub: "The machine-readable policy every agent accepts, and an AI assistant to draft a new one." },
  };

  const state = {
    token: null,
    data: null,
    requests: null,
    requestsAt: 0,
    requestsLoading: false,
    selectedDepositId: null,
    selectedUpdatedAt: null,
    // Follow the newest deposit in the inspector until the user picks one themselves.
    autoSelect: true,
    previous: null,
    grids: {},
    chartAsTable: false,
    timer: null,
    inFlight: false,
    paypal: null,
    view: "setup",
    policyLoaded: false,
    config: null,
    configReadOnly: false,
    applying: false,
    activating: false,
    editingDeposit: false,
    editingRate: false,
    sites: [],
    sitesError: null,
    setupMessage: null,
    selectedSiteId: null,
    activeSiteId: null,
    addingSite: false,
    sitesLoadToken: 0,
    playground: null,
    playgroundSiteId: null,
    pgLoadToken: 0,
    pgCanStart: null,
    pgSelected: null,
    pgResults: {},
    pgSteps: [],
    pgRunning: false,
    pgCurrentIndex: -1,
    pgRunToken: 0,
    pgActiveRunId: null,
    // Suppress automatic settlement toasts while the playground response panel is the source of truth.
    pgQuietUntil: 0,
  };

  const $ = (id) => document.getElementById(id);

  // --- storage (may be unavailable in private windows) ------------------------------
  const store = {
    get(area, key) {
      try { return window[area].getItem(key); } catch (_) { return null; }
    },
    set(area, key, value) {
      try {
        if (value === null) window[area].removeItem(key);
        else window[area].setItem(key, value);
      } catch (_) { /* ignore */ }
    },
  };

  // --- small DOM helpers ---------------------------------------------------------------
  function el(tag, props, children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === undefined || value === null) continue;
        if (key === "text") node.textContent = String(value);
        else if (key === "className") node.className = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value);
      }
    }
    for (const child of children || []) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // Node.append(null) inserts the text "null", so optional children go through here.
  function appendAll(node, ...children) {
    for (const child of children) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
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

  function providerName(code) {
    if (code === "claude" || code === "anthropic") return "Claude";
    if (code === "openai") return "OpenAI";
    return "AI";
  }

  function formatTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatDateTime(iso) {
    if (!iso) return "";
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
  }

  function compact(value) {
    const number = Number(value) || 0;
    if (Math.abs(number) >= 1e6) return `${(number / 1e6).toFixed(1).replace(/\.0$/, "")}M`;
    if (Math.abs(number) >= 1e4) return `${(number / 1e3).toFixed(1).replace(/\.0$/, "")}K`;
    return number.toLocaleString();
  }

  function money(amount, currency) {
    const value = Number(amount);
    if (amount === null || amount === undefined || amount === "" || Number.isNaN(value)) return "";
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: currency || "USD" }).format(value);
    } catch (_) {
      return `${amount} ${currency || ""}`.trim();
    }
  }

  // --- status chips: icon + label, color never carries meaning alone -------------------
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

  const DECISIONS = {
    granted: ["good", "✓", "Granted"],
    payment_required: ["warning", "$", "Payment required"],
    denied: ["critical", "✕", "Denied"],
  };

  function chip(tone, icon, label) {
    return el("span", { className: "chip" }, [
      el("span", { className: "chip-icon", dataset: { tone }, "aria-hidden": "true", text: icon }),
      el("span", { text: label }),
    ]);
  }

  function depositLabel(stateCode) {
    return (DEPOSIT_STATUS[stateCode] || [])[2] || humanize(stateCode);
  }

  function depositChip(stateCode) {
    const [tone, icon, label] = DEPOSIT_STATUS[stateCode] || ["neutral", "●", humanize(stateCode)];
    return chip(tone, icon, label);
  }

  function sessionLabel(row) {
    if (row.disqualified) return "Disqualified";
    if (row.status === "ACTIVE") return "Active";
    if (row.status === "COMPLETED") return "Completed";
    return humanize(row.status);
  }

  function sessionChip(row) {
    if (row.disqualified) return chip("critical", "✕", "Disqualified");
    if (row.status === "ACTIVE") return chip("neutral", "●", "Active");
    if (row.status === "COMPLETED") return chip("good", "✓", "Completed");
    return chip("neutral", "○", humanize(row.status));
  }

  function allowedChip(allowed) {
    if (allowed === true) return chip("good", "✓", "Allowed");
    if (allowed === false) return chip("critical", "✕", "Blocked");
    return el("span", { className: "muted", text: "—" });
  }

  function tierBadge(id, name) {
    return el("span", { className: "tier-badge", dataset: { tier: id || "base" }, text: name || (id ? id : "Base") });
  }

  function httpTone(status) {
    if (status === 200) return "good";
    if (status >= 300 && status < 400) return "neutral";
    if (status === 402) return "warning";
    if (status === 429) return "serious";
    return "critical";
  }

  // Audit events grouped by what they are about, from the event type's name.
  function eventCategory(type) {
    const t = String(type || "");
    if (t === "POLICY_VIOLATION") return "Violation";
    if (t.startsWith("GATEWAY_") || t === "CHALLENGE_ISSUED") return "Gateway";
    if (/^DEPOSIT_(REFUND|RELEASE|FORFEIT)|^REFUND_|^HOLD_/.test(t)) return "Settlement";
    if (/^DEPOSIT_|^ORDER_|^PAYMENT_|^PROVIDER_/.test(t)) return "Payment";
    return "Session";
  }

  const RETURNED = new Set(["REFUNDED", "RELEASED"]);

  // --- theme ---------------------------------------------------------------------------
  const THEMES = ["dark", "light", "system"];

  function applyTheme(pref) {
    document.documentElement.dataset.theme = pref;
    $("theme-toggle").textContent = `Theme: ${pref}`;
    if (window.CTAnalytics) window.CTAnalytics.themeChanged();
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

  // --- banner --------------------------------------------------------------------------
  function showBanner(message) {
    const banner = $("banner");
    clear(banner);
    banner.append(el("span", { text: message }));
    banner.hidden = false;
  }

  function hideBanner() {
    $("banner").hidden = true;
  }

  // --- fetch helpers -------------------------------------------------------------------
  function authHeaders(extra) {
    const headers = Object.assign({ Accept: "application/json" }, extra || {});
    if (state.token) headers["X-Admin-Token"] = state.token;
    return headers;
  }

  async function getJson(url, options) {
    const response = await fetch(url, options);
    let body = null;
    try { body = await response.json(); } catch (_) { body = null; }
    return { ok: response.ok, status: response.status, body };
  }

  // --- views (sidebar navigation, deep-linkable through the URL hash) --------------------
  function showView(view, fromHash) {
    if (!VIEWS[view]) view = "overview";
    state.view = view;
    for (const section of document.querySelectorAll(".view")) section.hidden = section.dataset.view !== view;
    for (const link of document.querySelectorAll(".sidenav-link[data-view]")) {
      if (link.dataset.view === view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    // The section holding the open page is expanded and marked; the others close.
    const activeLink = document.querySelector(`.sidenav-link[data-view="${view}"]`);
    const activeSection = activeLink ? activeLink.closest(".sidenav-section") : null;
    for (const section of document.querySelectorAll(".sidenav-section")) {
      setSectionOpen(section, section === activeSection);
      section.dataset.active = String(section === activeSection);
    }
    if ($("site-filter-wrap")) $("site-filter-wrap").hidden = !FILTERED_VIEWS.has(view) || state.sites.length < 2;
    $("page-title").textContent = VIEWS[view].title;
    if ($("page-sub")) $("page-sub").textContent = VIEWS[view].sub;
    if (!fromHash) history.replaceState(null, "", view === "overview" ? window.location.pathname : `#${view}`);
    if (view === "setup" || view === "playground") {
      loadSetup();
      if (!state.paypalAccount || !state.paypalAccount.connected) loadPayPalAccount();
    }
    if (view === "operations" || view === "analytics") loadRequests(true);
    if (view === "policy") loadPolicy();
    if (view === "commerce") loadCommerce();
    if (view === "analytics" && window.CTAnalytics) window.CTAnalytics.open(analyticsContext());
  }

  function setSectionOpen(section, open) {
    const toggle = section.querySelector("[data-section-toggle]");
    const subs = section.querySelector(".sidenav-subs");
    if (toggle) toggle.setAttribute("aria-expanded", String(open));
    if (subs) subs.hidden = !open;
    section.dataset.open = String(open);
  }

  function initViews() {
    for (const link of document.querySelectorAll(".sidenav-link[data-view]")) {
      link.addEventListener("click", () => showView(link.dataset.view));
    }
    // A section heading opens its first page (which expands the section), or collapses
    // the section when it is already open.
    for (const toggle of document.querySelectorAll("[data-section-toggle]")) {
      toggle.addEventListener("click", () => {
        const section = toggle.closest(".sidenav-section");
        if (section.dataset.open === "true") setSectionOpen(section, false);
        else showView(toggle.dataset.first);
      });
    }
    window.addEventListener("hashchange", () => showView(window.location.hash.slice(1), true));
    showView(window.location.hash.slice(1) || "overview", true);
  }

  // --- grids ---------------------------------------------------------------------------
  function cellNode(render) {
    return (params) => (params.data ? render(params.data) : "");
  }

  function depositDetail(d) {
    const facts = window.CTGrid.facts;
    return el("div", { className: "detail-grid" }, [
      el("div", null, [
        el("p", { className: "detail-title", text: "Deposit" }),
        facts([
          ["Deposit ID", d.deposit_id, true],
          ["Crawler", d.crawler_id, true],
          ["Session", d.session_id, true],
          ["Amount", money(d.amount, d.currency)],
          ["Pricing tier", tierBadge(d.pricing_tier, d.pricing_tier_name)],
          ["Resource", d.requested_resource, true],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Decision & settlement" }),
        facts([
          ["Outcome", depositChip(d.deposit_state)],
          ["Policy decision", humanize(d.policy_decision)],
          ["Settlement", d.settlement_mode === "authorization" ? "Authorization hold (void if compliant)" : "Capture, then refund if compliant"],
          ["Refund status", d.refund_status ? humanize(d.refund_status) : ""],
          ["Created", formatDateTime(d.created_at)],
          ["Updated", formatDateTime(d.updated_at)],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: d.payment_mode === "mock" ? "Payment (mock mode)" : "PayPal records" }),
        facts([
          ["Payment status", humanize(d.payment_status)],
          ["Rail", d.payment_mode === "mock" ? "Simulated" : "PayPal"],
          ["Order", d.provider_order_id, true],
          ["Capture", d.provider_capture_id, true],
          ["Authorization", d.provider_authorization_id, true],
          ["Refund", d.provider_refund_id, true],
        ]),
      ]),
    ]);
  }

  function sessionDetail(s) {
    const facts = window.CTGrid.facts;
    const deposit = state.data ? state.data.deposits.find((d) => d.deposit_id === s.deposit_id) : null;
    return el("div", { className: "detail-grid" }, [
      el("div", null, [
        el("p", { className: "detail-title", text: "Session" }),
        facts([
          ["Session ID", s.session_id, true],
          ["Crawler", s.crawler_id, true],
          ["Started", formatDateTime(s.created_at)],
          ["Completed", s.completed_at ? formatDateTime(s.completed_at) : "Still active"],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Requests" }),
        facts([
          ["Total", String(s.total_requests)],
          ["Allowed", String(s.allowed_requests)],
          ["Blocked", String(s.rejected_requests)],
          ["Violations", String(s.total_violations)],
          ["Status", sessionChip(s)],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Deposit" }),
        deposit
          ? facts([
              ["Deposit ID", deposit.deposit_id, true],
              ["Amount", money(deposit.amount, deposit.currency)],
              ["Outcome", depositChip(deposit.deposit_state)],
              ["Tier", tierBadge(deposit.pricing_tier, deposit.pricing_tier_name)],
              ["PayPal order", deposit.provider_order_id, true],
            ])
          : el("p", { className: "muted", text: s.deposit_id ? `Deposit ${s.deposit_id}` : "No deposit linked." }),
      ]),
    ]);
  }

  function requestDetail(r) {
    const [tone, icon, label] = DECISIONS[r.access_decision] || ["neutral", "●", r.access_decision];
    const facts = window.CTGrid.facts;
    return el("div", { className: "detail-grid" }, [
      el("div", null, [
        el("p", { className: "detail-title", text: "Request" }),
        facts([
          ["Request ID", r.request_id, true],
          ["Time", formatDateTime(r.timestamp)],
          ["Crawler", r.crawler_id, true],
          ["Resource", r.resource ? `GET ${r.resource === "/" ? "/" : r.resource}` : "", true],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Gateway decision" }),
        facts([
          ["HTTP status", r.http_status ? String(r.http_status) : "—"],
          ["Decision", chip(tone, icon, label)],
          ["Reason", r.reason_code ? `${humanize(r.reason_code)} (${r.reason_code})` : "Within policy"],
          ["Tier", tierBadge(r.pricing_tier, r.pricing_tier_name)],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Deposit & PayPal" }),
        r.deposit_id
          ? facts([
              ["Deposit ID", r.deposit_id, true],
              ["Amount", r.deposit_amount != null ? money(r.deposit_amount, r.currency) : ""],
              ["Deposit state", r.deposit_state ? depositChip(r.deposit_state) : ""],
              ["PayPal order", r.provider_order_id, true],
            ])
          : el("p", { className: "muted", text: "No deposit linked to this request." }),
      ]),
    ]);
  }

  function eventDetail(e) {
    const facts = window.CTGrid.facts;
    return el("div", { className: "detail-grid" }, [
      el("div", null, [
        el("p", { className: "detail-title", text: "Audit event" }),
        facts([
          ["Event ID", e.event_id, true],
          ["Type", e.event_type, true],
          ["Category", eventCategory(e.event_type)],
          ["Time", formatDateTime(e.timestamp)],
        ]),
      ]),
      el("div", null, [
        el("p", { className: "detail-title", text: "Subject" }),
        facts([
          ["Crawler", e.crawler_id, true],
          ["Session", e.session_id, true],
          ["Deposit", e.deposit_id, true],
          ["Path", e.path, true],
          ["Reason", e.reason_code ? `${humanize(e.reason_code)} (${e.reason_code})` : ""],
          ["Result", e.allowed === true ? "Allowed" : e.allowed === false ? "Blocked" : ""],
        ]),
      ]),
    ]);
  }

  function requestColumns() {
    return [
      { headerName: "Time", field: "timestamp", minWidth: 110, maxWidth: 130, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
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
      { headerName: "Crawler", field: "crawler_id", minWidth: 160, cellClass: "mono", tooltipField: "crawler_id" },
      { headerName: "Resource", field: "resource", minWidth: 200, cellClass: "mono", tooltipField: "resource" },
      {
        headerName: "Tier", field: "pricing_tier_name", minWidth: 105, maxWidth: 130,
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
        valueGetter: (p) => (p.data ? (p.data.deposit_state ? depositLabel(p.data.deposit_state) : "No deposit yet") : ""),
        cellRenderer: cellNode((r) => (r.deposit_state ? depositChip(r.deposit_state) : el("span", { className: "muted", text: "No deposit yet" }))),
      },
      { headerName: "Reason", field: "reason_code", minWidth: 150, valueFormatter: (p) => (p.value ? humanize(p.value) : "—") },
    ];
  }

  function initGrids() {
    if (!window.CTGrid || !window.CTGrid.available()) {
      for (const id of ["grid-deposits", "grid-sessions", "grid-events", "grid-requests", "grid-violations", "grid-orders", "grid-subscriptions"]) {
        const node = $(id);
        clear(node);
        node.append(el("p", { className: "empty", text: "The table library (AG Grid, cdn.jsdelivr.net) could not be loaded. Data is still available from /dashboard/data." }));
      }
      return false;
    }
    const create = window.CTGrid.create;
    initCommerceGrids(create);

    state.grids.deposits = create($("grid-deposits"), {
      rowId: (d) => d.deposit_id,
      detail: depositDetail,
      detailHeight: 210,
      selection: "single",
      pageSize: 10,
      autoHeight: true,
      flashNew: true,
      exportName: "sara-deposits",
      search: "Search deposits...",
      noRows: "No deposits yet. Run a crawler from the live demo.",
      noExport: ["explain"],
      onRowClicked: (row) => selectDeposit(row, true),
      segments: {
        label: "Filter by outcome",
        options: [
          { id: "returned", label: "Returned", test: (d) => RETURNED.has(d.deposit_state) },
          { id: "forfeited", label: "Forfeited", test: (d) => d.deposit_state === "FORFEITED" },
          { id: "held", label: "Held", test: (d) => !RETURNED.has(d.deposit_state) && d.deposit_state !== "FORFEITED" },
        ],
      },
      totals: (rows) => {
        const currency = rows[0].currency;
        const total = rows.reduce((sum, r) => sum + (Number(r.amount) || 0), 0);
        return { deposit_id: "__total", crawler_id: `Total · ${rows.length} deposit${rows.length === 1 ? "" : "s"}`, amount: total.toFixed(2), currency };
      },
      exportValues: {
        deposit_state: (d) => depositLabel(d.deposit_state),
        pricing_tier_name: (d) => d.pricing_tier_name || "Base",
        amount: (d) => d.amount,
      },
      columnDefs: [
        { headerName: "Outcome", field: "deposit_state", minWidth: 150, valueGetter: (p) => (p.data ? depositLabel(p.data.deposit_state) : ""), cellRenderer: cellNode((d) => depositChip(d.deposit_state)) },
        { headerName: "Crawler", field: "crawler_id", minWidth: 160, cellClass: "mono" },
        {
          headerName: "Tier", field: "pricing_tier_name", minWidth: 105, maxWidth: 125,
          valueGetter: (p) => (p.data ? p.data.pricing_tier_name || "Base" : ""),
          cellRenderer: cellNode((d) => tierBadge(d.pricing_tier, d.pricing_tier_name)),
        },
        {
          headerName: "Deposit", field: "amount", minWidth: 100, maxWidth: 120, type: "numericColumn", filter: "agNumberColumnFilter",
          valueGetter: (p) => (p.data ? Number(p.data.amount) : null),
          valueFormatter: (p) => (p.data && p.value != null ? money(p.value, p.data.currency) : ""),
        },
        { headerName: "Resource", field: "requested_resource", minWidth: 190, cellClass: "mono", tooltipField: "requested_resource" },
        { headerName: "Payment", field: "payment_status", minWidth: 110, maxWidth: 140, valueFormatter: (p) => humanize(p.value) },
        { headerName: "Settlement", field: "settlement_mode", maxWidth: 125, valueFormatter: (p) => (p.value === "authorization" ? "Hold" : p.value ? "Refund" : "") },
        { headerName: "Decision", field: "policy_decision", minWidth: 150, valueFormatter: (p) => humanize(p.value) },
        { headerName: "PayPal order", field: "provider_order_id", minWidth: 150, cellClass: "mono", tooltipField: "provider_order_id" },
        { headerName: "Created", field: "created_at", maxWidth: 120, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
        {
          headerName: "",
          colId: "explain",
          sortable: false,
          filter: false,
          resizable: false,
          maxWidth: 104,
          minWidth: 96,
          pinned: "right",
          cellRenderer: cellNode((d) =>
            el("button", {
              className: "cell-button",
              type: "button",
              text: "Explain",
              "aria-label": `Explain deposit ${d.deposit_id}`,
              onclick: (event) => { event.stopPropagation(); selectDeposit(d, true); },
            })
          ),
        },
      ],
    });

    state.grids.sessions = create($("grid-sessions"), {
      rowId: (s) => s.session_id,
      pageSize: 10,
      autoHeight: true,
      detail: sessionDetail,
      detailHeight: 176,
      exportName: "sara-sessions",
      search: "Search sessions...",
      noRows: "No agent sessions yet. Send a test agent from Websites → Playground.",
      segments: {
        label: "Filter by status",
        options: [
          { id: "active", label: "Active", test: (s) => s.status === "ACTIVE" && !s.disqualified },
          { id: "clean", label: "Compliant", test: (s) => s.status === "COMPLETED" && !s.disqualified },
          { id: "dq", label: "Disqualified", test: (s) => s.disqualified },
        ],
      },
      totals: (rows) => ({
        session_id: "__total",
        crawler_id: `Total · ${rows.length} session${rows.length === 1 ? "" : "s"}`,
        allowed_requests: rows.reduce((a, r) => a + r.allowed_requests, 0),
        rejected_requests: rows.reduce((a, r) => a + r.rejected_requests, 0),
        total_violations: rows.reduce((a, r) => a + r.total_violations, 0),
      }),
      exportValues: { status: (s) => sessionLabel(s) },
      columnDefs: [
        { headerName: "Status", field: "status", minWidth: 150, valueGetter: (p) => (p.data && p.data.session_id !== "__total" ? sessionLabel(p.data) : ""), cellRenderer: cellNode(sessionChip) },
        { headerName: "Crawler", field: "crawler_id", minWidth: 170, cellClass: "mono" },
        { headerName: "Allowed", field: "allowed_requests", maxWidth: 120, type: "numericColumn", filter: "agNumberColumnFilter" },
        { headerName: "Blocked", field: "rejected_requests", maxWidth: 120, type: "numericColumn", filter: "agNumberColumnFilter" },
        { headerName: "Violations", field: "total_violations", maxWidth: 130, type: "numericColumn", filter: "agNumberColumnFilter", cellClassRules: { "cell-critical": (p) => !p.node.rowPinned && p.value > 0 } },
        { headerName: "Deposit", field: "deposit_id", minWidth: 150, cellClass: "mono" },
        { headerName: "Started", field: "created_at", maxWidth: 120, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
      ],
    });

    state.grids.requests = create($("grid-requests"), {
      rowId: (r) => r.request_id,
      detail: requestDetail,
      detailHeight: 176,
      flashNew: true,
      exportName: "sara-requests",
      search: "Search requests...",
      noRows: "No gateway requests yet.",
      segments: {
        label: "Filter by gateway decision",
        options: [
          { id: "granted", label: "Granted", test: (r) => r.access_decision === "granted" },
          { id: "402", label: "HTTP 402", test: (r) => r.access_decision === "payment_required" },
          { id: "denied", label: "Denied", test: (r) => r.access_decision === "denied" },
        ],
      },
      exportValues: {
        access_decision: (r) => (DECISIONS[r.access_decision] || [])[2] || r.access_decision,
        deposit_state: (r) => (r.deposit_state ? depositLabel(r.deposit_state) : ""),
        pricing_tier_name: (r) => r.pricing_tier_name || "Base",
        reason_code: (r) => r.reason_code || "",
      },
      columnDefs: requestColumns(),
    });

    state.grids.violations = create($("grid-violations"), {
      rowId: (e) => e.event_id,
      detail: eventDetail,
      detailHeight: 160,
      flashNew: true,
      pageSize: 10,
      autoHeight: true,
      exportName: "sara-violations",
      search: "Search violations...",
      noRows: "No policy violations recorded. Every crawler has behaved so far.",
      exportValues: { reason_code: (e) => humanize(e.reason_code) },
      columnDefs: [
        { headerName: "Time", field: "timestamp", maxWidth: 120, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
        { headerName: "Violation", field: "reason_code", minWidth: 200, valueGetter: (p) => (p.data ? humanize(p.data.reason_code) : ""), cellRenderer: cellNode((e) => chip("critical", "✕", humanize(e.reason_code) || "Violation")) },
        { headerName: "Crawler", field: "crawler_id", minWidth: 170, cellClass: "mono" },
        { headerName: "Path", field: "path", minWidth: 200, cellClass: "mono", tooltipField: "path" },
        { headerName: "Deposit", field: "deposit_id", minWidth: 150, cellClass: "mono" },
      ],
    });

    state.grids.events = create($("grid-events"), {
      rowId: (e) => e.event_id,
      detail: eventDetail,
      detailHeight: 160,
      flashNew: true,
      exportName: "sara-audit",
      search: "Search audit events...",
      noRows: "No audit events yet.",
      segments: {
        label: "Filter by category",
        options: ["Gateway", "Payment", "Settlement", "Violation", "Session"].map((c) => ({ id: c, label: c, test: (e) => eventCategory(e.event_type) === c })),
      },
      exportValues: {
        allowed: (e) => (e.allowed === true ? "Allowed" : e.allowed === false ? "Blocked" : ""),
        event_type: (e) => e.event_type,
      },
      columnDefs: [
        { headerName: "Time", field: "timestamp", maxWidth: 120, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
        {
          headerName: "Category", colId: "category", minWidth: 120, maxWidth: 140,
          valueGetter: (p) => (p.data ? eventCategory(p.data.event_type) : ""),
          cellRenderer: (p) => (p.value ? el("span", { className: "cat-chip", dataset: { cat: p.value }, text: p.value }) : ""),
        },
        // Readable event name; the raw code is in the tooltip, the expanded row, and the CSV.
        { headerName: "Event", field: "event_type", minWidth: 220, valueFormatter: (p) => humanize(p.value), tooltipField: "event_type" },
        { headerName: "Crawler", field: "crawler_id", minWidth: 150, cellClass: "mono" },
        { headerName: "Path", field: "path", minWidth: 140, cellClass: "mono" },
        { headerName: "Reason", field: "reason_code", minWidth: 150, valueFormatter: (p) => humanize(p.value) },
        { headerName: "Result", field: "allowed", maxWidth: 120, valueGetter: (p) => (p.data ? (p.data.allowed === true ? "Allowed" : p.data.allowed === false ? "Blocked" : "") : ""), cellRenderer: cellNode((e) => allowedChip(e.allowed)) },
      ],
    });

    return true;
  }

  // --- render: header, KPIs, charts ----------------------------------------------------
  // A KPI card. With `go`, the card is a button that drills down into the matching table.
  function tile(label, value, sub, tone, icon, go) {
    const card = el(go ? "button" : "div", {
      className: `tile${go ? " tile-link" : ""}`,
      type: go ? "button" : null,
      title: go ? go.hint : null,
      dataset: tone ? { tone } : null,
      onclick: go ? () => drill(go) : null,
    }, [
      el("div", { className: "tile-head" }, [
        el("div", { className: "tile-label", text: label }),
        icon ? el("span", { className: "tile-icon", "aria-hidden": "true", text: icon }) : null,
      ]),
      el("div", { className: "tile-value", text: value }),
      sub ? el("div", { className: "tile-sub", text: sub }) : null,
      go ? el("span", { className: "tile-go", "aria-hidden": "true", text: "→" }) : null,
    ]);
    return card;
  }

  // Drill-down: open a view, apply a segment filter to one of its tables, and scroll to it.
  function drill({ view, grid, segment, target }) {
    showView(view);
    const ctl = grid ? state.grids[grid] : null;
    if (ctl) ctl.setSegment(segment || "all");
    const node = $(target);
    if (node) {
      const card = node.closest(".card") || node;
      card.scrollIntoView({ behavior: "smooth", block: "start" });
      card.classList.remove("flash-target");
      void card.offsetWidth;
      card.classList.add("flash-target");
    }
  }

  function renderKpis(k) {
    const kpis = $("kpis");
    clear(kpis);
    const settled = k.deposits_returned + k.deposits_forfeited;
    const totalRequests = k.gateway_requests_allowed + k.gateway_requests_rejected;
    kpis.append(
      tile("Kept from rule-breakers", money(k.amount_forfeited, k.currency),
        `${compact(k.deposits_forfeited)} deposit${k.deposits_forfeited === 1 ? "" : "s"} kept`, "critical", "✕",
        { view: "deposits", grid: "deposits", segment: "forfeited", target: "grid-deposits", hint: "Show kept deposits" }),
      tile("Returned to honest agents", money(k.amount_returned, k.currency),
        settled ? `${compact(k.deposits_returned)} of ${compact(settled)} settled deposits` : "No settled deposits yet", "good", "↺",
        { view: "deposits", grid: "deposits", segment: "returned", target: "grid-deposits", hint: "Show returned deposits" }),
      tile("Agent sessions", compact(k.sessions_total),
        k.sessions_active ? `${compact(k.sessions_active)} active now` : "None active now", "accent", "◉",
        { view: "operations", grid: "sessions", segment: "all", target: "grid-sessions", hint: "Open agent activity" }),
      tile("Requests blocked", compact(k.gateway_requests_rejected),
        totalRequests ? `${Math.round((k.gateway_requests_rejected / totalRequests) * 100)}% of ${compact(totalRequests)} requests · ${compact(k.policy_violations)} violations` : "No requests yet",
        k.gateway_requests_rejected ? "warning" : "good", "⊘",
        { view: "security", target: "grid-violations", hint: "Open violations" })
    );
  }

  // The latest deposits as a short, readable list; each opens the Deposits page on that row.
  function relativeTime(iso) {
    const then = Date.parse(iso || "");
    if (!Number.isFinite(then)) return "";
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.round(hours / 24);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }

  function openDeposit(d) {
    showView("deposits");
    selectDeposit(d, true);
    const card = $("explain-card");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderRecent(deposits) {
    const list = $("recent-list");
    if (!list) return;
    clear(list);
    const latest = deposits.slice().sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 6);
    if (!latest.length) {
      list.append(el("li", { className: "empty", text: "No decisions yet. Send a test agent from Websites → Playground." }));
      return;
    }
    for (const d of latest) {
      const what = d.requested_resource ? `asked for ${d.requested_resource}` : "crawl session";
      list.append(el("li", null, [
        el("button", { type: "button", className: "recent-row", onclick: () => openDeposit(d) }, [
          depositChip(d.deposit_state),
          el("span", { className: "recent-main" }, [
            el("span", { className: "recent-agent", text: d.crawler_id }),
            el("span", { className: "recent-what", text: `${what} · ${d.pricing_tier_name || "Base"} tier` }),
          ]),
          el("span", { className: "recent-amount", text: money(d.amount, d.currency) }),
          el("span", { className: "recent-time", text: relativeTime(d.updated_at) }),
        ]),
      ]));
    }
  }

  function renderSecurityKpis(data) {
    const box = $("security-kpis");
    clear(box);
    const k = data.kpis;
    const violations = data.events.filter((e) => e.event_type === "POLICY_VIOLATION");
    const offenders = new Set(violations.map((e) => e.crawler_id).filter(Boolean));
    const disqualified = data.sessions.filter((s) => s.disqualified).length;
    const total = k.gateway_requests_allowed + k.gateway_requests_rejected;
    const top = Object.entries(k.violations_by_reason || {})[0];
    box.append(
      tile("Policy violations", compact(k.policy_violations), top ? `Most common: ${humanize(top[0])}` : "None recorded", k.policy_violations ? "critical" : "good", "⚠",
        { view: "security", target: "grid-violations", hint: "Open the violations table" }),
      tile("Offending crawlers", compact(offenders.size), "In the latest 250 audit events", offenders.size ? "critical" : "good", "☠",
        { view: "security", grid: "events", segment: "Violation", target: "grid-events", hint: "Show violation events in the audit feed" }),
      tile("Disqualified sessions", compact(disqualified), `of ${compact(data.sessions.length)} sessions`, disqualified ? "critical" : "good", "⛔",
        { view: "operations", grid: "sessions", segment: "dq", target: "grid-sessions", hint: "Show disqualified sessions" }),
      tile("Block rate", total ? `${Math.round((k.gateway_requests_rejected / total) * 100)}%` : "—", `${compact(k.gateway_requests_rejected)} of ${compact(total)} gateway requests`, "warning", "⊘",
        { view: "operations", grid: "requests", segment: "denied", target: "grid-requests", hint: "Show denied requests" })
    );
    $("count-violations").textContent = compact(k.policy_violations);
    if ($("count-violations-parent")) {
      $("count-violations-parent").textContent = compact(k.policy_violations);
      $("count-violations-parent").hidden = !k.policy_violations;
    }
    $("count-sessions").textContent = compact(data.sessions.length);
    $("count-sessions").title = `${data.sessions.length} agent sessions, ${k.sessions_active} active`;
  }

  function showTooltip(target, valueText, labelText) {
    const tooltip = $("tooltip");
    clear(tooltip);
    tooltip.append(el("strong", { text: valueText }), el("span", { text: labelText }));
    tooltip.hidden = false;
    const rect = target.getBoundingClientRect();
    const top = Math.max(8, rect.top - tooltip.offsetHeight - 6);
    const left = Math.min(window.innerWidth - tooltip.offsetWidth - 8, Math.max(8, rect.left + rect.width / 2 - tooltip.offsetWidth / 2));
    tooltip.style.top = `${top}px`;
    tooltip.style.left = `${left}px`;
  }

  function hideTooltip() {
    $("tooltip").hidden = true;
  }

  function renderViolations(byReason) {
    const container = $("violations-chart");
    clear(container);
    const entries = Object.entries(byReason || {}).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    if (!entries.length) {
      container.append(el("p", { className: "empty", text: "No violations recorded yet." }));
      return;
    }

    if (state.chartAsTable) {
      const body = el("tbody", null, entries.map(([reason, count]) =>
        el("tr", null, [el("td", { text: humanize(reason) }), el("td", { className: "num", text: count.toLocaleString() })])
      ));
      container.append(el("table", { className: "viz-table" }, [
        el("thead", null, [el("tr", null, [el("th", { text: "Reason" }), el("th", { className: "num", text: "Violations" })])]),
        body,
      ]));
      return;
    }

    const max = Math.max(...entries.map(([, count]) => count));
    const bars = el("div", { className: "bars", role: "list" });
    for (const [reason, count] of entries) {
      const label = humanize(reason);
      const valueText = `${count.toLocaleString()} violation${count === 1 ? "" : "s"}`;
      const row = el("div", {
        className: "bar-row",
        role: "listitem",
        tabindex: "0",
        "aria-label": `${label}: ${valueText}`,
      }, [
        el("span", { className: "bar-label", text: label }),
        el("span", { className: "bar-track" }, [
          el("span", { className: "bar", style: `width: ${Math.max(2, (count / max) * 100 * 0.85)}%; --bar-color: var(--status-critical)` }),
          el("span", { className: "bar-value", text: count.toLocaleString() }),
        ]),
      ]);
      const show = () => showTooltip(row.querySelector(".bar"), valueText, label);
      row.addEventListener("pointerenter", show);
      row.addEventListener("focus", show);
      row.addEventListener("pointerleave", hideTooltip);
      row.addEventListener("blur", hideTooltip);
      bars.append(row);
    }
    container.append(bars);
  }

  // Share of deposits by outcome, with the value behind each group.
  function renderOutcomes(deposits) {
    const box = $("outcome-chart");
    clear(box);
    if (!deposits.length) {
      box.append(el("p", { className: "empty", text: "No deposits yet." }));
      return;
    }
    const groups = [
      ["Returned", "var(--status-good)", (d) => RETURNED.has(d.deposit_state)],
      ["Forfeited", "var(--status-critical)", (d) => d.deposit_state === "FORFEITED"],
      ["Held / in progress", "var(--accent)", (d) => ["CAPTURED", "AUTHORIZED", "APPROVED", "PENDING", "REFUND_PENDING"].includes(d.deposit_state)],
      ["Failed", "var(--status-serious)", (d) => d.deposit_state === "FAILED"],
    ];
    const currency = deposits[0].currency;
    const rows = groups.map(([label, color, test]) => {
      const list = deposits.filter(test);
      return { label, color, count: list.length, value: list.reduce((a, d) => a + (Number(d.amount) || 0), 0) };
    }).filter((g) => g.count);
    const bar = el("div", { className: "share-bar", role: "img", "aria-label": rows.map((g) => `${g.label}: ${g.count}`).join(", ") });
    for (const g of rows) bar.append(el("span", { className: "share-seg", style: `width:${(g.count / deposits.length) * 100}%; background:${g.color}`, title: `${g.label}: ${g.count}` }));
    box.append(
      bar,
      el("ul", { className: "share-legend" }, rows.map((g) => el("li", null, [
        el("span", { className: "share-swatch", style: `background:${g.color}` }),
        el("span", { text: `${g.label} · ${g.count} deposit${g.count === 1 ? "" : "s"}` }),
        el("span", { className: "num", text: money(g.value, currency) }),
      ])))
    );
  }

  // --- deposit inspector ---------------------------------------------------------------
  function selectDeposit(row, byUser) {
    if (!row || row.deposit_id === "__total") return;
    if (byUser) state.autoSelect = false;
    state.selectedDepositId = row.deposit_id;
    state.selectedUpdatedAt = row.updated_at;
    renderDepositFacts(row);
    loadExplanation(row.deposit_id);
    if (state.paypal) state.paypal.setDeposit(row);
    const grid = state.grids.deposits;
    if (grid) {
      const node = grid.api.getRowNode(row.deposit_id);
      if (node && !node.isSelected()) node.setSelected(true, true);
    }
  }

  function renderDepositFacts(d) {
    const box = $("deposit-facts");
    clear(box);
    if (!d) return;
    box.append(
      el("div", { className: "deposit-head" }, [
        el("span", { className: "deposit-amount", text: money(d.amount, d.currency) }),
        depositChip(d.deposit_state),
      ]),
      window.CTGrid.facts([
        ["Crawler", d.crawler_id, true],
        ["Tier", tierBadge(d.pricing_tier, d.pricing_tier_name)],
        ["Rail", d.payment_mode === "mock" ? "Simulated" : "PayPal"],
        ["PayPal order", d.provider_order_id, true],
        ["Refund", d.provider_refund_id, true],
      ]),
      el("hr", { className: "divider" })
    );
  }

  async function loadExplanation(depositId) {
    const panel = $("explanation");
    $("explain-sub").textContent = `Deposit ${depositId}`;
    // Never leave another deposit's explanation on screen while this one loads.
    if (panel.dataset.depositId !== depositId) {
      clear(panel);
      panel.append(el("p", { className: "empty", text: "Explaining this deposit..." }));
      panel.dataset.depositId = depositId;
    }
    panel.classList.add("refreshing");
    try {
      const result = await getJson(`/dashboard/deposits/${encodeURIComponent(depositId)}/explanation`, { headers: authHeaders() });
      // A newer selection wins: drop answers for a deposit that is no longer selected.
      if (state.selectedDepositId && state.selectedDepositId !== depositId) return;
      if (!result.ok) {
        clear(panel);
        panel.append(el("p", { className: "empty", text: (result.body && result.body.message) || `Could not load the explanation (HTTP ${result.status}).` }));
        return;
      }
      renderExplanation(result.body);
    } catch (_) {
      clear(panel);
      panel.append(el("p", { className: "empty", text: "Could not reach the gateway." }));
    } finally {
      panel.classList.remove("refreshing");
    }
  }

  function renderExplanation(x) {
    const panel = $("explanation");
    clear(panel);
    const meta = el("div", { className: "explain-meta" }, [
      depositChip(x.deposit_state),
      el("span", { className: "badge" }, [
        el("span", { className: "badge-dot", dataset: { tone: x.generated_by === "template" ? "neutral" : "good" } }),
        el("span", { text: x.generated_by === "template" ? "Rule-based explanation" : `Explained by ${providerName(x.generated_by)}${x.model ? ` (${x.model})` : ""}` }),
      ]),
    ]);

    const evidenceById = new Map(x.evidence.map((item) => [item.evidence_id, item]));
    const evidenceList = el("div", { className: "evidence-list", "aria-label": "Evidence from the audit log" });
    const evidenceRows = new Map();
    for (const item of x.evidence) {
      const detail = [item.event_type, item.path, item.reason_code ? humanize(item.reason_code) : null].filter(Boolean).join(" · ");
      const rowNode = el("div", { className: "evidence-item", id: `evidence-${item.evidence_id}` }, [
        el("span", { className: "evidence-id", text: item.evidence_id }),
        el("span", null, [el("span", { text: detail }), el("div", { className: "hint", text: formatDateTime(item.timestamp) })]),
      ]);
      evidenceRows.set(item.evidence_id, rowNode);
      evidenceList.append(rowNode);
    }

    const highlight = (ids) => {
      for (const [id, node] of evidenceRows) node.classList.toggle("highlight", ids.includes(id));
      const first = evidenceRows.get(ids[0]);
      if (first) first.scrollIntoView({ block: "nearest" });
    };

    const findings = el("ol", { className: "findings" }, x.findings.map((finding) =>
      el("li", null, [
        el("span", { text: finding.statement }),
        ...finding.evidence.filter((id) => evidenceById.has(id)).map((id) =>
          el("button", {
            className: "evidence-chip",
            type: "button",
            text: id,
            "aria-label": `Show evidence ${id}`,
            onclick: () => highlight(finding.evidence),
          })
        ),
      ])
    ));

    appendAll(
      panel,
      meta,
      el("p", { className: "explain-headline", text: x.headline }),
      el("p", { className: "explain-summary", text: x.summary }),
      findings,
      x.notice ? el("p", { className: "notice", text: x.notice }) : null,
      x.omitted_events ? el("p", { className: "notice", text: `${x.omitted_events} repetitive events omitted from the evidence.` }) : null,
      evidenceList
    );
  }

  // --- websites, access rules, and the plan playground --------------------------------

  // The backend owns the website list. localStorage only remembers which website the
  // console was showing, and is used when the backend list cannot be read.
  const SITE_SELECTED_KEY = "ct-console-site-id";
  const SITE_FILTER_KEY = "ct-console-site-filter";
  // Pages whose numbers, lists, and charts follow the website filter.
  const FILTERED_VIEWS = new Set(["overview", "deposits", "operations", "security", "analytics"]);
  const LEGACY_SITE_STORE_KEY = "ct-console-sites-v4";
  const CURRENCY_SYMBOLS = { USD: "$" };

  const PG_BEHAVIORS = [
    { id: "crawl_allowed", label: "Crawl allowed paths", tone: "safe", action: "allowed_page" },
    { id: "follow_rate_limit", label: "Follow website rate limit", tone: "safe", action: "polite_pace" },
    { id: "crawl_disallowed", label: "Crawl disallowed paths", tone: "bad", action: "disallowed_path" },
    { id: "violate_rate_limit", label: "Violate website rate limit", tone: "bad", action: "rate_limit_burst" },
  ];

  const RATE_UNIT_LABELS = { 1: "second", 60: "minute", 3600: "hour" };
  const RATE_UNITS = [1, 60, 3600];

  // The console only offers sec/min/hour. Legacy configs (for example a 330s window
  // written by an older Crawl-delay merge) map to the nearest preset for editing.
  function normalizeRateUnit(unit) {
    const seconds = Number(unit);
    if (!Number.isFinite(seconds) || seconds <= 0) return 60;
    if (RATE_UNITS.includes(seconds)) return seconds;
    return RATE_UNITS.reduce(
      (best, candidate) => (Math.abs(candidate - seconds) < Math.abs(best - seconds) ? candidate : best),
      60
    );
  }

  function rateUnitLabel(unit) {
    const seconds = normalizeRateUnit(unit);
    return RATE_UNIT_LABELS[seconds] || "minute";
  }

  // Radios allow exactly one behavior at a time; the first is selected by default.
  function defaultBehaviorSelection() {
    const result = {};
    PG_BEHAVIORS.forEach((behavior, index) => { result[behavior.id] = index === 0; });
    return result;
  }

  function configFromPolicy(policy) {
    return {
      sara_mode: (state.data && state.data.sara_mode) || "demo",
      site_id: policy.site_id || "default",
      site_name: policy.site_name || "SARA site",
      policy_version: policy.policy_version || "",
      origin_url: "",
      deposit_amount: policy.deposit_amount,
      currency: policy.currency || "USD",
      allowed_paths: policy.allowed_paths || [],
      disallowed_paths: policy.disallowed_paths || [],
      rate_limit_requests: policy.rate_limit_requests,
      rate_limit_window_seconds: policy.rate_limit_window_seconds,
      max_requests_per_session: policy.max_requests_per_session,
      config_public: false,
      warnings: [],
    };
  }

  // A read-only console (no token and no public config) can still show the active site
  // from /policies. The website list endpoint is admin/public-config only.
  function siteFromConfig(cfg) {
    if (!cfg || !cfg.deposit_amount) return null;
    const origin = String(cfg.origin_url || "");
    const id = String(cfg.site_id || "default");
    return normalizeSite({
      site_id: id,
      name: cfg.site_name || siteHost(origin) || id,
      host: siteHost(origin) || cfg.site_name || id,
      origin_url: origin,
      status: "live",
      active: true,
      deposit_amount: cfg.deposit_amount,
      currency: cfg.currency || "USD",
      allowed_paths: cfg.allowed_paths || [],
      disallowed_paths: cfg.disallowed_paths || [],
      rate_limit_requests: cfg.rate_limit_requests,
      rate_limit_window_seconds: cfg.rate_limit_window_seconds,
      max_requests_per_session: cfg.max_requests_per_session,
      policy_version: cfg.policy_version || "",
    });
  }

  function selectedSite() {
    return state.sites.find((site) => site.id === state.selectedSiteId) || state.sites[0] || null;
  }

  function storedSelectedSiteId() {
    const saved = store.get("localStorage", SITE_SELECTED_KEY);
    if (saved) return saved;
    // Earlier builds stored the whole website list; only its selected id is still useful.
    const legacy = store.get("localStorage", LEGACY_SITE_STORE_KEY);
    if (!legacy) return null;
    try {
      const parsed = JSON.parse(legacy);
      return (parsed && parsed.selectedSiteId) || null;
    } catch (_) {
      return null;
    }
  }

  function persistSelectedSite() {
    store.set("localStorage", SITE_SELECTED_KEY, state.selectedSiteId || null);
  }

  function siteHost(originUrl) {
    if (!originUrl) return "";
    try { return new URL(originUrl).host; } catch (_) { return ""; }
  }

  // The form shows a fixed "https://" affordance; hide it when the user pastes a full URL.
  function syncSiteAddPrefix() {
    const input = $("site-add-input");
    const prefix = document.querySelector(".site-add-shell > span");
    if (!input || !prefix) return;
    prefix.hidden = /^[a-z][a-z0-9+.-]*:\/\//i.test(input.value.trim());
  }

  // Accept "example.com" or a full URL; the gateway requires an absolute http(s) origin.
  function normalizeOrigin(value) {
    let raw = String(value || "").trim().replace(/^\/+/, "");
    if (!raw) return "";
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) raw = `https://${raw}`;
    let url;
    try { url = new URL(raw); } catch (_) { return ""; }
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    if (!url.hostname) return "";
    // Catch typos before the gateway tries to resolve them: a host is a dotted name,
    // an IP literal, or localhost, and never contains spaces.
    const host = url.hostname;
    if (/%|\s/.test(host)) return "";
    if (host !== "localhost" && !host.includes(".") && !host.startsWith("[")) return "";
    return url.origin;
  }

  // GET /admin/sites returns site objects; this is the shape the console renders.
  function normalizeSite(raw) {
    if (!raw || typeof raw !== "object") return null;
    const id = String(raw.site_id || raw.id || "").trim();
    if (!id) return null;
    const origin = String(raw.origin_url || "").trim();
    const host = String(raw.host || "").trim() || siteHost(origin) || id;
    const depositValue = raw.deposit_amount;
    const rate = Number(raw.rate_limit_requests);
    const unit = Number(raw.rate_limit_window_seconds);
    return {
      id,
      name: String(raw.name || host),
      host,
      origin,
      savedOrigin: origin,
      status: raw.status === "paused" ? "paused" : "live",
      active: Boolean(raw.active),
      deposit: depositValue === null || depositValue === undefined || depositValue === "" ? "" : String(depositValue),
      currency: String(raw.currency || "USD"),
      allowed: Array.isArray(raw.allowed_paths) ? raw.allowed_paths.map(String).filter(Boolean) : [],
      disallowed: Array.isArray(raw.disallowed_paths) ? raw.disallowed_paths.map(String).filter(Boolean) : [],
      rate: Number.isFinite(rate) && rate > 0 ? rate : 60,
      unit: normalizeRateUnit(unit),
      maxRequests: Number(raw.max_requests_per_session) || null,
      policyVersion: raw.policy_version ? String(raw.policy_version) : "",
      version: raw.version,
      updatedAt: raw.updated_at || "",
      dirty: false,
    };
  }

  // Replace the local list with the gateway's, keeping the user's selection when possible.
  function applySiteList(body, opts) {
    const options = opts || {};
    state.sites = (body.sites || []).map(normalizeSite).filter(Boolean);
    state.sitesLoaded = true;
    state.sitesError = null;
    state.activeSiteId = body.active_site_id || state.activeSiteId || null;
    if (typeof body.config_public === "boolean") {
      state.configReadOnly = !(state.token || state.owner || body.config_public);
    }
    let candidate = options.selectId || null;
    // Keep the active site as the console context unless the caller explicitly selected
    // another one. A stale localStorage selection must not show a site the gateway is not
    // actually serving.
    if (!candidate && state.selectedSiteId === state.activeSiteId) candidate = state.selectedSiteId;
    if (!candidate) candidate = state.activeSiteId;
    if (!candidate) candidate = state.selectedSiteId;
    if (!state.sites.some((site) => site.id === candidate)) candidate = storedSelectedSiteId();
    if (!state.sites.some((site) => site.id === candidate)) candidate = state.sites.length ? state.sites[0].id : null;
    state.selectedSiteId = candidate;
    persistSelectedSite();
    renderSiteFilter();
  }

  // --- website filter: "All websites" or one site, for the activity pages --------------
  function siteFilter() {
    const value = state.siteFilter || "all";
    return value === "all" || state.sites.some((site) => site.id === value) ? value : "all";
  }

  function siteQuery() {
    const value = siteFilter();
    return value === "all" ? "" : `?site=${encodeURIComponent(value)}`;
  }

  function renderSiteFilter() {
    const select = $("site-filter");
    if (!select) return;
    if (state.sitesLoaded && state.siteFilter && siteFilter() !== state.siteFilter) {
      // The filtered website was removed: fall back to every website.
      state.siteFilter = "all";
      store.set("localStorage", SITE_FILTER_KEY, null);
    }
    clear(select);
    select.append(el("option", { value: "all", text: state.sites.length === 1 ? "All websites (1)" : `All websites${state.sites.length ? ` (${state.sites.length})` : ""}` }));
    for (const site of state.sites) select.append(el("option", { value: site.id, text: site.host }));
    select.value = siteFilter();
    if ($("site-filter-wrap")) $("site-filter-wrap").hidden = !FILTERED_VIEWS.has(state.view) || state.sites.length < 2;
  }

  function changeSiteFilter(value) {
    state.siteFilter = value || "all";
    store.set("localStorage", SITE_FILTER_KEY, state.siteFilter === "all" ? null : state.siteFilter);
    state.requestsAt = 0;
    state.autoSelect = true;
    // A different website's records are not "new": skip the change toasts for this load.
    state.previous = null;
    refresh();
    if (state.view === "operations" || state.view === "analytics") loadRequests(true);
  }

  async function loadSites(opts) {
    const options = opts || {};
    const token = (state.sitesLoadToken || 0) + 1;
    state.sitesLoadToken = token;
    let result;
    try {
      result = await getJson("/admin/sites", { headers: authHeaders() });
    } catch (_) {
      result = { ok: false, status: 0, body: null };
    }
    // A newer list request (for example a fast site switch) owns the state now.
    if (token !== state.sitesLoadToken) return true;
    if (!result.ok || !result.body || !Array.isArray(result.body.sites)) {
      const fallback = siteFromConfig(state.config);
      if (fallback) {
        state.sites = [fallback];
        state.activeSiteId = fallback.id;
        state.selectedSiteId = fallback.id;
        state.sitesError = null;
        persistSelectedSite();
        return true;
      }
      const message = apiErrorMessage(result.body, result.status, `Could not load the websites (HTTP ${result.status || "unreachable"}).`);
      state.sitesError = message;
      if (!options.silent) showBanner(message);
      return false;
    }
    state.sitesError = null;
    applySiteList(result.body, options);
    return true;
  }

  // Fold a RuntimeConfigResponse (PUT /admin/config) into the rendered website.
  function applyRuntimeConfigToSite(site, cfg) {
    if (!site || !cfg) return;
    if (cfg.deposit_amount !== undefined && cfg.deposit_amount !== null && cfg.deposit_amount !== "") {
      site.deposit = String(cfg.deposit_amount);
    }
    if (cfg.currency) site.currency = String(cfg.currency);
    if (Array.isArray(cfg.allowed_paths)) site.allowed = cfg.allowed_paths.map(String).filter(Boolean);
    if (Array.isArray(cfg.disallowed_paths)) site.disallowed = cfg.disallowed_paths.map(String).filter(Boolean);
    if (Number.isFinite(Number(cfg.rate_limit_requests)) && Number(cfg.rate_limit_requests) > 0) {
      site.rate = Number(cfg.rate_limit_requests);
    }
    if (Number(cfg.rate_limit_window_seconds) > 0) {
      site.unit = normalizeRateUnit(cfg.rate_limit_window_seconds);
    }
    if (Number.isFinite(Number(cfg.max_requests_per_session))) {
      site.maxRequests = Number(cfg.max_requests_per_session);
    }
    if (cfg.policy_version) site.policyVersion = String(cfg.policy_version);
    if (cfg.version !== undefined && cfg.version !== null) site.version = cfg.version;
    if (cfg.updated_at) site.updatedAt = cfg.updated_at;
    if (cfg.origin_url) {
      site.origin = String(cfg.origin_url);
      site.savedOrigin = String(cfg.origin_url);
    }
    if (cfg.site_name) site.name = String(cfg.site_name);
    if (cfg.status === "live" || cfg.status === "paused") site.status = cfg.status;
    if (cfg.active_site_id) state.activeSiteId = cfg.active_site_id;
    site.dirty = false;
  }

  function closeSiteMenus(except) {
    for (const menu of document.querySelectorAll(".site-menu")) {
      if (menu !== except) menu.hidden = true;
    }
  }

  function showSetupMessage(message) {
    state.setupMessage = message || null;
    const node = $("cfg-hint");
    if (node) {
      node.textContent = message;
      node.hidden = !message;
    }
    showBanner(message);
  }

  // FastAPI errors carry either {"detail": {"message": ...}}, a validation error list,
  // or a plain string.
  function apiErrorMessage(body, status, fallback) {
    const detail = body && body.detail;
    let message = "";
    if (Array.isArray(detail)) {
      const first = detail.find((item) => item && (item.msg || item.message)) || detail[0];
      if (first && typeof first === "object") message = first.msg || first.message || "";
      else if (typeof first === "string") message = first;
    } else if (detail && typeof detail === "object") {
      message = detail.message || detail.error || "";
    } else if (typeof detail === "string") {
      message = detail;
    }
    if (typeof message !== "string") message = "";
    if (!message && body && typeof body.message === "string") message = body.message;
    return message || fallback || `Request failed (HTTP ${status}).`;
  }

  // The gateway uses robots-style prefix rules. The console shows the familiar `/*`
  // spelling, then strips it before applying so `/products/*` becomes `/products`.
  function policyPath(value) {
    let path = String(value || "").trim();
    if (!path) return "";
    if (!path.startsWith("/")) path = `/${path}`;
    if (path.endsWith("*")) path = path.slice(0, -1).replace(/\/+$/, "") || "/";
    return path;
  }

  function moneyForSite(site) {
    return money(site && site.deposit, (site && site.currency) || "USD");
  }

  function renderSites() {
    const box = $("site-list");
    if (!box) return;
    clear(box);
    const count = state.sites.length;
    $("sites-count").textContent = `${count} site${count === 1 ? "" : "s"}`;

    if (!count) {
      box.append(el("p", { className: "empty", text: state.sitesError || "No websites yet. Add one below." }));
      return;
    }

    for (const site of state.sites) {
      const selected = site.id === state.selectedSiteId;
      const radio = el("input", {
        type: "radio",
        name: "site-select",
        className: "site-radio",
        value: site.id,
        "aria-label": `Select ${site.host}`,
        onchange: () => selectSite(site.id),
      });
      radio.checked = selected;
      const menu = el("div", { className: "site-menu", hidden: "hidden" }, [
        el("button", {
          type: "button",
          dataset: { action: "toggle" },
          text: site.status === "live" ? "Pause website" : "Resume website",
          onclick: (event) => { event.stopPropagation(); toggleSite(site.id); },
        }),
        el("button", {
          type: "button",
          dataset: { action: "remove" },
          text: "Remove website",
          onclick: (event) => { event.stopPropagation(); removeSite(site.id); },
        }),
      ]);
      const row = el("div", {
        className: "site-row",
        role: "listitem",
        dataset: { selected: String(selected), status: site.status },
      }, [
        el("label", {
          className: "site-select",
          title: `Select ${site.host} to edit its rules`,
        }, [
          radio,
          el("span", { className: "site-ident" }, [
            el("span", { className: "site-url", text: site.host }),
            el("span", {
              className: "site-origin",
              text: [site.origin, `${moneyForSite(site) || "—"} deposit`].filter(Boolean).join(" · "),
            }),
          ]),
          selected ? el("span", { className: "site-editing", text: "Editing" }) : null,
          el("span", { className: "site-state" }, [
            el("span", { className: "site-dot", "aria-hidden": "true" }),
            el("span", { text: site.status === "live" ? "Live" : "Paused" }),
          ]),
        ]),
        el("div", { className: "site-actions" }, [
          el("button", {
            type: "button",
            className: "site-menu-toggle",
            text: "···",
            "aria-label": `Actions for ${site.host}`,
            "aria-expanded": "false",
            onclick: (event) => {
              event.stopPropagation();
              const willOpen = menu.hidden;
              closeSiteMenus();
              menu.hidden = !willOpen;
              event.currentTarget.setAttribute("aria-expanded", String(!menu.hidden));
            },
          }),
          menu,
        ]),
      ]);
      box.append(row);
    }
  }

  async function selectSite(siteId) {
    const site = state.sites.find((item) => item.id === siteId);
    // Clicking the current selection is a no-op only when the gateway already has it active.
    const alreadyActive = siteId === state.activeSiteId || Boolean(site && site.active);
    if (!site || (siteId === state.selectedSiteId && alreadyActive)) return;
    // Block switching while a write or another activation is in flight so a late response
    // cannot change the site the console is showing.
    if (state.applying || state.addingSite || state.activating) return;
    const previousId = state.selectedSiteId;
    state.setupMessage = null;
    if (state.pgRunning) stopPlaygroundRun(true);
    state.selectedSiteId = siteId;
    state.editingDeposit = false;
    state.editingRate = false;
    state.pgResults = {};
    state.pgSteps = [];
    persistSelectedSite();

    // Read-only consoles still let the user inspect each website; only the activation
    // write is skipped. The rest of the console follows the local selection.
    if (!state.configReadOnly) {
      state.activating = true;
      refreshSetupControls();
      try {
        const result = await getJson(`/admin/sites/${encodeURIComponent(siteId)}/activate`, {
          method: "POST",
          headers: authHeaders(),
        });
        if (!result.ok) {
          showSetupMessage(apiErrorMessage(result.body, result.status, `Could not activate ${site.host} (HTTP ${result.status}).`));
          state.selectedSiteId = previousId;
          persistSelectedSite();
          renderSetupView();
          await loadPlayground(true);
          return;
        }
        if (result.body && Array.isArray(result.body.sites)) {
          applySiteList(result.body, { selectId: siteId });
        } else {
          state.activeSiteId = siteId;
          for (const item of state.sites) item.active = item.id === siteId;
        }
      } finally {
        state.activating = false;
        refreshSetupControls();
      }
    }
    hideBanner();
    renderSetupView();
    await loadPlayground(true);
  }

  function openSiteAddForm() {
    const form = $("site-add-form");
    if (!form) return;
    form.hidden = false;
    if ($("site-add-open")) $("site-add-open").setAttribute("aria-expanded", "true");
    if ($("site-add-input")) $("site-add-input").focus();
  }

  function closeSiteAddForm() {
    const form = $("site-add-form");
    if (form) form.hidden = true;
    if ($("site-add-open")) $("site-add-open").setAttribute("aria-expanded", "false");
    if ($("site-add-input")) $("site-add-input").value = "";
    syncSiteAddPrefix();
    setAddError(null);
  }

  // Errors about the address being typed belong next to the field, not in the settings card.
  function setAddError(message) {
    const node = $("site-add-error");
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
  }

  async function addSite(event) {
    event.preventDefault();
    setAddError(null);
    const input = $("site-add-input");
    if (state.configReadOnly) {
      showSetupMessage("Read-only. Sign in as the owner to add a website.");
      return;
    }
    if (state.addingSite || state.applying || state.activating) return;
    const raw = input ? input.value.trim() : "";
    if (!raw) {
      setAddError("Enter the website address, for example example.com.");
      if (input) input.focus();
      return;
    }
    const origin = normalizeOrigin(raw);
    if (!origin) {
      setAddError("Enter a valid website address, for example https://example.com.");
      if (input) input.focus();
      return;
    }
    const newHost = siteHost(origin).toLowerCase();
    const existing = state.sites.find((item) => (siteHost(item.origin) || item.host).toLowerCase() === newHost);
    if (existing) {
      closeSiteAddForm();
      await selectSite(existing.id);
      toast("neutral", "i", `${existing.host} is already on the gateway`, "Showing its settings instead of adding it again.");
      return;
    }

    state.addingSite = true;
    refreshSetupControls();
    try {
      const result = await getJson("/admin/sites", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ origin_url: origin, set_active: true }),
      });
      if (!result.ok) {
        setAddError(apiErrorMessage(result.body, result.status, `Could not add ${origin} (HTTP ${result.status}).`));
        return;
      }
      closeSiteAddForm();
      state.setupMessage = null;
      hideBanner();
      if (state.pgRunning) stopPlaygroundRun(true);
      state.pgResults = {};
      state.pgSteps = [];
      const created = result.body || {};
      const newId = created.site_id || (created.site && created.site.site_id) || "";
      const refreshed = await loadSites({ silent: true, selectId: newId || null });
      if (!refreshed) {
        // The website was created even if the refresh failed, so show it locally.
        const fresh = normalizeSite(created);
        if (fresh && !state.sites.some((item) => item.id === fresh.id)) {
          state.sites.push(fresh);
          state.selectedSiteId = fresh.id;
          state.activeSiteId = created.active_site_id || fresh.id;
          persistSelectedSite();
        }
      }
      const site = selectedSite();
      renderSetupView();
      await loadPlayground(true);
      toast("good", "✓", "Website added", site ? site.host : origin);
    } catch (_) {
      setAddError("Could not reach the gateway to add the website.");
    } finally {
      state.addingSite = false;
      refreshSetupControls();
    }
  }

  async function toggleSite(siteId) {
    const site = state.sites.find((item) => item.id === siteId);
    if (!site || state.applying || state.addingSite || state.activating) return;
    if (state.configReadOnly) {
      showSetupMessage("Read-only. Sign in as the owner to pause or resume a website.");
      return;
    }
    const nextStatus = site.status === "live" ? "paused" : "live";
    const result = await getJson(`/admin/sites/${encodeURIComponent(siteId)}`, {
      method: "PUT",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ status: nextStatus }),
    });
    if (!result.ok) {
      showSetupMessage(apiErrorMessage(result.body, result.status, `Could not update ${site.host} (HTTP ${result.status}).`));
      return;
    }
    hideBanner();
    state.setupMessage = null;
    const updated = result.body || {};
    site.status = updated.status === "live" || updated.status === "paused" ? updated.status : nextStatus;
    if (updated.name) site.name = String(updated.name);
    if (updated.updated_at) site.updatedAt = updated.updated_at;
    if (siteId === state.selectedSiteId) {
      if (state.pgRunning) stopPlaygroundRun(true);
      state.editingDeposit = false;
      state.editingRate = false;
      state.pgResults = {};
      state.pgSteps = [];
    }
    renderSetupView();
    toast(
      site.status === "live" ? "good" : "neutral",
      site.status === "live" ? "✓" : "○",
      `${site.host} ${site.status === "live" ? "resumed" : "paused"}`,
      site.status === "live" ? "Apply the configuration to make it live." : "The saved configuration is kept."
    );
  }

  async function removeSite(siteId) {
    if (state.configReadOnly) {
      showSetupMessage("Read-only. Sign in as the owner to remove a website.");
      return;
    }
    const site = state.sites.find((item) => item.id === siteId);
    if (!site || state.applying || state.addingSite || state.activating) return;
    const result = await getJson(`/admin/sites/${encodeURIComponent(siteId)}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!result.ok) {
      showSetupMessage(apiErrorMessage(result.body, result.status, `Could not remove ${site.host} (HTTP ${result.status}).`));
      return;
    }
    hideBanner();
    state.setupMessage = null;
    if (siteId === state.selectedSiteId) {
      if (state.pgRunning) stopPlaygroundRun(true);
      state.editingDeposit = false;
      state.editingRate = false;
      state.pgResults = {};
      state.pgSteps = [];
    }
    // The gateway activates the next website when the selected one is deleted.
    if (state.selectedSiteId === siteId) state.selectedSiteId = null;
    const refreshed = await loadSites({ silent: true });
    if (!refreshed) {
      state.sites = state.sites.filter((item) => item.id !== siteId);
      if (!state.sites.length) state.selectedSiteId = null;
      persistSelectedSite();
    }
    renderSetupView();
    await loadPlayground(true);
    toast("neutral", "−", "Website removed", site.host);
  }

  function renderPathList(tone, paths) {
    const list = $(tone === "allowed" ? "allowed-list" : "disallowed-list");
    clear(list);
    if (!paths.length) {
      list.append(el("li", { className: "empty", text: "No paths yet." }));
      return;
    }
    for (const path of paths) {
      list.append(el("li", { className: "rule-path" }, [
        el("span", { className: "rule-path-name", text: path }),
        el("button", {
          type: "button",
          className: "rule-path-remove",
          text: "×",
          "aria-label": `Remove ${path}`,
          onclick: () => removePath(tone, path),
        }),
      ]));
    }
  }

  function renderRules(site) {
    if ($("rules-site")) $("rules-site").textContent = site ? site.host : "";
    $("allowed-count").textContent = String((site && site.allowed.length) || 0);
    $("disallowed-count").textContent = String((site && site.disallowed.length) || 0);
    renderPathList("allowed", (site && site.allowed) || []);
    renderPathList("disallowed", (site && site.disallowed) || []);
  }

  function refreshSetupControls() {
    const site = selectedSite();
    const live = Boolean(site && site.status === "live");
    const disabled = !site || state.configReadOnly || state.applying || state.activating || !live;
    const canEdit = !state.configReadOnly;

    for (const id of ["cfg-deposit-save", "cfg-rate-save"]) {
      const button = $(id);
      if (button) {
        button.disabled = disabled;
        button.textContent = state.applying ? "Saving..." : "Save";
      }
    }
    for (const id of ["cfg-deposit-edit", "cfg-rate-edit"]) {
      const button = $(id);
      if (button) button.disabled = disabled;
    }
    // Read-only and paused websites cannot be edited; leave edit mode if either happened.
    if (!canEdit || !site || !live) {
      state.editingDeposit = false;
      state.editingRate = false;
    }

    if ($("cfg-deposit-read")) $("cfg-deposit-read").hidden = state.editingDeposit;
    if ($("cfg-deposit-edit-row")) $("cfg-deposit-edit-row").hidden = !state.editingDeposit;
    if ($("cfg-deposit-value")) $("cfg-deposit-value").textContent = site ? (moneyForSite(site) || "—") : "—";
    if ($("cfg-rate-read")) $("cfg-rate-read").hidden = state.editingRate;
    if ($("cfg-rate-edit-row")) $("cfg-rate-edit-row").hidden = !state.editingRate;
    if ($("cfg-rate-value")) {
      $("cfg-rate-value").textContent = site
        ? `${site.rate} request${Number(site.rate) === 1 ? "" : "s"} / ${rateUnitLabel(site.unit)}`
        : "—";
    }

    const symbol = CURRENCY_SYMBOLS[(site && site.currency) || "USD"] || "$";
    if ($("cfg-currency-symbol")) $("cfg-currency-symbol").textContent = symbol;

    if ($("cfg-deposit")) $("cfg-deposit").disabled = !canEdit || !site;
    if ($("site-add-input")) $("site-add-input").disabled = !canEdit || state.addingSite || state.activating;
    if ($("site-add-button")) {
      $("site-add-button").disabled = !canEdit || state.addingSite || state.activating;
      $("site-add-button").textContent = state.addingSite ? "Adding..." : "Add website";
      if (canEdit) $("site-add-button").removeAttribute("title");
      else $("site-add-button").title = "Read-only";
    }
    for (const node of document.querySelectorAll(".site-menu-toggle")) node.disabled = !canEdit || state.activating;
    for (const node of document.querySelectorAll(".rule-path-remove, .rule-path-add input, .rule-path-add button")) {
      node.disabled = !canEdit || !site || !live || state.applying;
    }
    for (const id of ["allowed-add-input", "disallowed-add-input", "cfg-rate", "cfg-rate-unit"]) {
      if ($(id)) $(id).disabled = !canEdit || !site || !live || state.applying;
    }

    let hint = "Changes apply live to the selected website.";
    if (state.setupMessage) hint = state.setupMessage;
    else if (!site) hint = "Add a website to configure the gateway.";
    else if (state.configReadOnly) hint = "Read-only. Sign in as the owner to apply changes.";
    else if (state.activating) hint = "Selecting the website...";
    else if (site.status !== "live") hint = `${site.host} is paused. Resume it to apply changes.`;
    else if (state.applying) hint = `Applying the configuration for ${site.host}...`;
    else if (site.dirty) hint = `Unsaved changes for ${site.host}.`;
    else hint = "";
    if ($("cfg-hint")) {
      $("cfg-hint").textContent = hint;
      $("cfg-hint").hidden = !hint;
    }
  }

  // The settings and playground cards name the website they act on, so it is never
  // ambiguous which site a deposit, rule, or test run belongs to.
  function renderSiteContext(site) {
    if ($("settings-site")) $("settings-site").textContent = site ? site.host : "—";
    for (const node of document.querySelectorAll(".connect-host")) node.textContent = site ? site.host : "your domain";
    if ($("settings-sub")) {
      let sub = "Add a website to configure it.";
      if (site && site.status !== "live") sub = `${site.origin || site.host} is paused: agents are not gated and settings are locked.`;
      else if (site) sub = `Agents visiting ${site.origin || site.host} must follow these rules. Changes apply immediately.`;
      $("settings-sub").textContent = sub;
    }
    if ($("site-settings-card")) $("site-settings-card").dataset.status = site ? site.status : "none";
    if ($("pg-sub")) {
      $("pg-sub").textContent = site
        ? `Send a test agent to ${site.host} through the gateway`
        : "Send a test agent through the gateway";
    }
  }

  function renderSiteConfig() {
    const site = selectedSite();
    renderSiteContext(site);
    if (!site) {
      refreshSetupControls();
      renderRules(null);
      return;
    }
    if ($("cfg-deposit")) $("cfg-deposit").value = site.deposit || "";
    if ($("cfg-rate")) $("cfg-rate").value = site.rate || "";
    site.unit = normalizeRateUnit(site.unit);
    if ($("cfg-rate-unit")) $("cfg-rate-unit").value = String(site.unit);
    renderRules(site);
    refreshSetupControls();
  }

  function renderSetupView() {
    renderSites();
    renderSiteConfig();
    renderPlayground();
    renderOnboarding();
  }

  // First run: until the owner adds a website, Setup is an onboarding card with the
  // address field open, and the playground asks for a website first.
  function renderOnboarding() {
    const none = Boolean(state.sitesLoaded && !state.sites.length && !state.sitesError);
    const show = (id, visible) => { if ($(id)) $(id).hidden = !visible; };
    show("site-onboard", none);
    show("site-list", !none);
    show("site-add-open", !none);
    show("site-add-cancel", !none);
    show("site-settings-card", !none);
    if (none && $("site-add-form") && $("site-add-form").hidden) {
      $("site-add-form").hidden = false;
      if ($("site-add-open")) $("site-add-open").setAttribute("aria-expanded", "true");
    }
    show("pg-empty", none);
    show("pg-target", !none && state.sites.length > 0);
    show("lab-card", !none);
    show("playground-card", false);
    renderLab();
    renderPlaygroundTarget();
  }

  function renderPlaygroundTarget() {
    const select = $("pg-site-select");
    if (!select) return;
    clear(select);
    for (const site of state.sites) {
      select.append(el("option", { value: site.id, text: site.status === "live" ? site.host : `${site.host} (paused)` }));
    }
    const site = selectedSite();
    if (site) select.value = site.id;
    select.disabled = state.activating || state.pgRunning || state.sites.length < 2;
    if ($("pg-target-rules")) {
      $("pg-target-rules").textContent = site
        ? `${moneyForSite(site) || "—"} deposit · ${site.rate} request${Number(site.rate) === 1 ? "" : "s"} / ${rateUnitLabel(site.unit)} · ${site.disallowed.length} blocked path${site.disallowed.length === 1 ? "" : "s"}`
        : "";
    }
  }

  function markSiteDirty() {
    const site = selectedSite();
    if (!site) return;
    site.dirty = true;
    state.setupMessage = null;
    refreshSetupControls();
  }

  function removePath(tone, path) {
    const site = selectedSite();
    if (!site || state.applying || state.activating) return;
    const key = tone === "allowed" ? "allowed" : "disallowed";
    site[key] = site[key].filter((item) => item !== path);
    persistPathChange(site);
  }

  function addPath(event, tone) {
    event.preventDefault();
    const site = selectedSite();
    if (!site) return;
    const input = $(tone === "allowed" ? "allowed-add-input" : "disallowed-add-input");
    let value = input.value.trim();
    if (!value) { input.focus(); return; }
    if (!value.startsWith("/")) value = `/${value}`;
    if (/\s/.test(value)) {
      showSetupMessage("Paths cannot contain spaces.");
      return;
    }
    if (tone === "disallowed" && policyPath(value) === "/") {
      showSetupMessage("The site root cannot be disallowed. Add a specific path such as /admin/*.");
      return;
    }
    const key = tone === "allowed" ? "allowed" : "disallowed";
    if (site[key].includes(value)) {
      showSetupMessage(`${value} is already listed.`);
      return;
    }
    site[key].push(value);
    input.value = "";
    persistPathChange(site);
  }

  // Path edits persist immediately; the deposit/rate pencil Save buttons keep their own
  // explicit apply, and the playground's Run test still applies before running.
  function persistPathChange(site) {
    state.pgResults = {};
    state.pgSteps = [];
    markSiteDirty();
    renderSiteConfig();
    renderPlayground();
    if (!state.configReadOnly && site.status === "live") {
      void applySelectedSite({ silent: true });
    }
  }

  async function loadSetup(force) {
    if (force) {
      state.sites = [];
      state.sitesError = null;
      state.selectedSiteId = null;
      state.activeSiteId = null;
      state.setupMessage = null;
      state.pgResults = {};
      state.pgSteps = [];
      state.playground = null;
      state.playgroundSiteId = null;
      state.pgCanStart = null;
      state.config = null;
    }
    if (!state.config) {
      let cfg = null;
      const result = await getJson("/admin/config", { headers: authHeaders() });
      if (result.ok && result.body) {
        cfg = result.body;
      } else {
        const policy = await getJson("/policies", { headers: { Accept: "application/json" } });
        cfg = policy.ok && policy.body ? configFromPolicy(policy.body) : {};
      }
      state.config = cfg;
      state.configReadOnly = !(state.token || state.owner || Boolean(cfg && cfg.config_public));
    }
    // The gateway is the source of truth for the website list; localStorage only helps
    // pick which website to show when the previous selection is still available.
    // Reload only when the list is empty so unsaved edits survive view switches.
    const loaded = state.sites.length ? true : await loadSites({ silent: true });
    if (!loaded) {
      showBanner(state.sitesError || "Could not load the websites from the gateway. Check the connection and try again.");
    }
    renderSetupView();
    await loadPlayground();
  }

  async function applySelectedSite(options) {
    const opts = options || {};
    const site = selectedSite();
    if (!site || state.applying || state.addingSite) return false;
    if (state.configReadOnly) {
      if (!opts.silent) showSetupMessage("Read-only. Sign in as the owner to apply changes.");
      return false;
    }
    if (site.status !== "live") {
      if (!opts.silent) showSetupMessage(`Resume ${site.host} before applying its configuration.`);
      return false;
    }
    const deposit = String(site.deposit || "").trim();
    const rate = Number(site.rate);
    const unit = Number(site.unit);
    if (!deposit) { showSetupMessage("Enter a deposit amount for this website."); return false; }
    if (!Number.isFinite(rate) || rate < 1) { showSetupMessage("The rate limit must be at least 1 request."); return false; }
    if (!Number.isFinite(unit) || unit <= 0) { showSetupMessage("Choose a rate-limit window."); return false; }

    state.applying = true;
    refreshSetupControls();
    try {
      const payload = {
        deposit_amount: deposit,
        allowed_paths: site.allowed.map(policyPath).filter(Boolean),
        disallowed_paths: site.disallowed.map(policyPath).filter((path) => path && path !== "/"),
        rate_limit_requests: Math.round(rate),
        rate_limit_window_seconds: unit,
      };
      // Send the origin only when the user actually changed it.
      if (site.origin && site.origin !== (site.savedOrigin || "")) payload.origin_url = site.origin;
      const result = await getJson(`/admin/config?site_id=${encodeURIComponent(site.id)}`, {
        method: "PUT",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      if (!result.ok) {
        showBanner(apiErrorMessage(result.body, result.status, `Configuration was rejected (HTTP ${result.status}).`));
        return false;
      }
      applyRuntimeConfigToSite(site, result.body);
      state.config = Object.assign({}, state.config, result.body);
      state.configReadOnly = !(state.token || state.owner || Boolean(result.body && result.body.config_public));
      // Refresh the website's data from the gateway, keeping the current selection.
      const refreshed = await loadSites({ silent: true, selectId: site.id });
      if (refreshed) {
        const saved = state.sites.find((item) => item.id === site.id);
        if (saved) saved.dirty = false;
      }
      hideBanner();
      state.setupMessage = null;
      renderSiteConfig();
      renderPlayground();
      if (!opts.silent) {
        toast("good", "✓", "Gateway updated live", `${site.host} · ${moneyForSite(site)} deposit.`);
      }
      return true;
    } finally {
      state.applying = false;
      refreshSetupControls();
      // The playground was last rendered while `applying` was still true, so re-render it
      // now that the flag is cleared or the "Run test" button stays disabled.
      renderPlayground();
    }
  }

  function renderPlaygroundChoices() {
    const box = $("pg-choices");
    if (!box) return;
    clear(box);
    if (!state.pgSelected) state.pgSelected = defaultBehaviorSelection();
    for (const tone of ["safe", "bad"]) {
      const behaviors = PG_BEHAVIORS.filter((behavior) => behavior.tone === tone);
      const col = el("div", { className: "pg-choice-col", dataset: { tone: tone === "safe" ? "good" : "critical" } }, [
        el("div", { className: "pg-choice-head" }, [
          el("span", { className: "pg-choice-title", text: tone === "safe" ? "Safe" : "Bad" }),
        ]),
      ]);
      for (const behavior of behaviors) {
        const input = el("input", {
          type: "radio",
          name: "pg-behavior",
          value: behavior.id,
          "aria-label": behavior.label,
          onchange: (event) => {
            // A radio group keeps exactly one behavior selected.
            if (!event.target.checked) return;
            for (const item of PG_BEHAVIORS) state.pgSelected[item.id] = item.id === behavior.id;
            state.pgResults = {};
            state.pgSteps = [];
            renderPlayground();
            const next = document.querySelector(`#pg-choices input[value="${behavior.id}"]`);
            if (next) next.focus();
          },
          onclick: (event) => {
            // Clicking the radio that is already selected fires no change event, so clear
            // the previous output here as well.
            if (!event.target.checked || !state.pgSelected[behavior.id]) return;
            if (!state.pgResults[behavior.id] && !state.pgSteps.length) return;
            state.pgResults = {};
            state.pgSteps = [];
            renderPlayground();
          },
        });
        input.checked = Boolean(state.pgSelected[behavior.id]);
        col.append(el("label", { className: "pg-choice" }, [
          input,
          el("span", { text: behavior.label }),
        ]));
      }
      box.append(col);
    }
  }

  function buildPlaygroundPlan() {
    const safe = PG_BEHAVIORS.filter((behavior) => behavior.tone === "safe" && state.pgSelected[behavior.id]);
    const bad = PG_BEHAVIORS.filter((behavior) => behavior.tone === "bad" && state.pgSelected[behavior.id]);
    return safe.concat(bad);
  }

  const HTTP_REASON = Object.freeze({
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    303: "See Other",
    307: "Temporary Redirect",
    308: "Permanent Redirect",
    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    409: "Conflict",
    410: "Gone",
    422: "Unprocessable Entity",
    429: "Too Many Requests",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
  });

  function httpStatusText(code) {
    const status = Number(code);
    if (!Number.isInteger(status) || status <= 0) return "";
    return `${status} ${HTTP_REASON[status] || ""}`.trim();
  }

  function responseTone(code) {
    const status = Number(code);
    if (!Number.isInteger(status)) return "muted";
    if (status >= 200 && status < 300) return "good";
    if (status === 402) return "warning";
    if (status === 429) return "serious";
    if (status >= 300 && status < 400) return "warning";
    return "critical";
  }

  function finalResponseStep(action, run) {
    const steps = Array.isArray(action && action.steps) && action.steps.length
      ? action.steps
      : (Array.isArray(run && run.steps) ? run.steps : []);
    const withStatus = steps.filter((step) => step && step.http_status !== null && step.http_status !== undefined);
    const reads = withStatus.filter((step) => /^GET\s+/i.test(String(step.request || "")));
    if (reads.length) return reads[reads.length - 1];
    return withStatus.length ? withStatus[withStatus.length - 1] : null;
  }

  function depositLine(deposit) {
    if (!deposit) return { text: "No deposit", tone: "muted" };
    const stateCode = deposit.deposit_state;
    const currency = deposit.currency || "USD";
    const amount = money(deposit.amount != null ? deposit.amount : deposit.captured_amount, currency);
    if (!stateCode) {
      const consequence = String(deposit.consequence || "");
      if (!consequence || /none|no deposit/i.test(consequence)) return { text: "No deposit taken", tone: "muted" };
      return { text: consequence, tone: "muted" };
    }
    const zero = money(0, currency);
    if (stateCode === "FORFEITED") return { text: `${amount || "Deposit"} forfeited (refund ${zero})`, tone: "critical" };
    if (stateCode === "REFUNDED") return { text: `${amount || "Deposit"} refunded (refund ${amount || zero})`, tone: "good" };
    if (stateCode === "RELEASED") return { text: `${amount || "Deposit"} released (refund not needed)`, tone: "good" };
    if (stateCode === "REFUND_PENDING") return { text: `${amount || "Deposit"} refund pending (refund ${amount || zero})`, tone: "warning" };
    if (stateCode === "FAILED") return { text: `${amount || "Deposit"} payment failed (refund ${zero})`, tone: "critical" };
    if (stateCode === "CAPTURED" || stateCode === "AUTHORIZED") {
      return { text: `${amount || "Deposit"} held (refund after settlement)`, tone: "muted" };
    }
    return { text: `${amount || "Deposit"} ${deposit.consequence || humanize(stateCode)}`.trim(), tone: "muted" };
  }

  function isSessionSummary(message) {
    const text = String(message || "");
    return /^Session\s+\S+/i.test(text) && /remaining\s+(budget|window)/i.test(text);
  }

  function describePlaygroundResponse(action, run) {
    const violation = (action && action.violation) || (run && run.violation) || null;
    const deposit = (action && action.deposit) || (run && run.deposit) || null;
    const refused = Boolean((action && action.outcome === "refused") || (run && run.outcome === "refused"));
    const failed = Boolean(
      (action && (action.status === "failed" || action.outcome === "error")) ||
      (run && (run.status === "failed" || run.status === "cancelled" || run.outcome === "error"))
    );
    const step = finalResponseStep(action, run);
    let statusCode = null;
    if (refused) statusCode = (violation && violation.http_status) || (step && step.http_status);
    else if (!failed) statusCode = (step && step.http_status) || 200;
    else if (step && Number(step.http_status) >= 400) statusCode = step.http_status;

    let message = "";
    if (refused) {
      message = (violation && (violation.agent_reason || violation.rule_matched || violation.reason_code))
        || (step && step.message)
        || (action && action.error)
        || (run && run.error)
        || "The gateway refused the action.";
    } else if (failed) {
      message = (action && action.error)
        || (run && run.error)
        || (step && step.message)
        || "The action did not finish.";
    } else {
      const stepMessage = step && step.message ? step.message : "";
      if (stepMessage && !isSessionSummary(stepMessage)) {
        message = stepMessage;
      } else if (step && step.request) {
        const status = step.http_status ? ` (HTTP ${step.http_status})` : "";
        const page = action && action.rendered_html ? " and returned the page" : "";
        message = `The gateway allowed ${step.request}${status}${page}.`;
      } else {
        message = "The gateway allowed the action.";
      }
    }

    const depositInfo = depositLine(deposit);
    return {
      status: httpStatusText(statusCode) || (failed ? "Run failed" : refused ? "Refused" : "Completed"),
      statusTone: statusCode ? responseTone(statusCode) : (failed || refused ? "critical" : "good"),
      message,
      deposit: depositInfo.text,
      depositTone: depositInfo.tone,
    };
  }

  function responseRow(label, value, tone) {
    const text = value === null || value === undefined || value === "" ? "—" : value;
    return el("div", { className: "pg-response-row" }, [
      el("dt", { text: label }),
      el("dd", { dataset: tone ? { tone } : null, text }),
    ]);
  }

  function playgroundStepRow(number, label, options) {
    const opts = options || {};
    const stepState = opts.state || "done";
    const title = number ? `Step ${number} – ${label}` : label;
    const marker = stepState === "running"
      ? el("span", { className: "pg-step-spinner", "aria-hidden": "true" })
      : el("span", { className: "pg-step-marker", "aria-hidden": "true" });
    const stateLabel = stepState === "blocked" ? "Blocked" : stepState === "pending" ? "Pending" : stepState === "running" ? "Running" : "Completed";
    return el("li", { className: "pg-timeline-step", dataset: { state: stepState } }, [
      el("span", { className: "pg-step-marker-wrap" }, [marker]),
      el("div", { className: "pg-step-body" }, [
        el("span", { className: "pg-step-title" }, [
          el("span", { className: "sr-only", text: `${stateLabel}: ` }),
          document.createTextNode(title),
        ]),
        opts.request || opts.httpStatus
          ? el("span", { className: "pg-step-meta pg-step-meta-line" }, [
              opts.request ? el("span", { className: "pg-step-request", text: opts.request }) : null,
              opts.request && opts.httpStatus ? el("span", { className: "pg-step-sep", text: "·" }) : null,
              opts.httpStatus ? el("span", { text: httpStatusText(opts.httpStatus) }) : null,
            ])
          : null,
        opts.message ? el("span", { className: "pg-step-meta", text: opts.message }) : null,
      ]),
    ]);
  }

  function renderPlayground() {
    const site = selectedSite();
    if (!state.pgSelected) state.pgSelected = defaultBehaviorSelection();

    renderPlaygroundChoices();

    const plan = buildPlaygroundPlan();
    const selected = plan[0] || null;

    const result = selected ? state.pgResults[selected.id] : null;
    const running = Boolean(state.pgRunning && selected && result && result.state === "running");
    const resultSteps = result && Array.isArray(result.steps) ? result.steps : [];
    const steps = running ? state.pgSteps : resultSteps;

    // Timeline of the actual steps the agent took, with a live spinner on the newest one.
    // Rebuild only when the steps or run state change, so polling does not churn the DOM.
    const timeline = $("pg-timeline");
    if (timeline) {
      const refused = Boolean(result && result.state === "blocked");
      const blockedHttp = result && result.http !== null && result.http !== undefined
        ? Number(result.http)
        : null;
      const hasBlockedStatus = Number.isInteger(blockedHttp) && blockedHttp > 0;
      const signature = JSON.stringify({
        running: running ? 1 : 0,
        refused: refused ? 1 : 0,
        blockedHttp: hasBlockedStatus ? blockedHttp : null,
        hasResult: result ? 1 : 0,
        emptyText: !steps.length ? (running ? "running" : result ? "result" : "idle") : "",
        steps: steps.map((step) => step
          ? [step.seq, step.label, step.allowed, step.http_status, step.reason_code, step.message, step.request]
          : null),
      });
      if (timeline.dataset.signature !== signature) {
        timeline.dataset.signature = signature;
        clear(timeline);
        if (!steps.length && running) {
          timeline.append(el("li", { className: "pg-timeline-step pg-timeline-empty", dataset: { state: "running" } }, [
            el("span", { className: "pg-step-marker-wrap" }, [
              el("span", { className: "pg-step-spinner", "aria-hidden": "true" }),
            ]),
            el("div", { className: "pg-step-body" }, [
              el("span", { className: "pg-step-title", text: "Starting the run…" }),
              el("span", { className: "pg-step-meta", text: "Waiting for the first step from the gateway." }),
            ]),
          ]));
        }
        // `allowed` is true only for HTTP 200, so a redirect or the expected 402 challenge is
        // not a refusal. Only the response that matches the run's violation status is marked
        // blocked, so the initial payment challenge stays a completed step.
        const isBlockedStep = (step) => Boolean(step) && (
          hasBlockedStatus
            ? Number(step.http_status) === blockedHttp
            : Number(step.http_status) >= 400
        );
        const anyBlockedStep = refused && steps.some(isBlockedStep);
        steps.forEach((step, index) => {
          const isLast = index === steps.length - 1;
          let stepState = "done";
          if (refused && isBlockedStep(step)) stepState = "blocked";
          else if (refused && !anyBlockedStep && isLast) stepState = "blocked";
          if (running && isLast && stepState !== "blocked") stepState = "running";
          timeline.append(playgroundStepRow(index + 1, (step && step.label) || "Step", {
            state: stepState,
            request: step && step.request,
            httpStatus: step && step.http_status,
            message: step && step.message,
          }));
        });
        if (running) timeline.append(playgroundStepRow(null, "Final response", { state: "pending" }));
      }
    }

    // The final response is always shown after a run, with no expand/collapse step.
    const finalBox = $("pg-final");
    if (finalBox) {
      clear(finalBox);
      const response = result && result.response ? result.response : null;
      finalBox.hidden = !response;
      if (response) {
        finalBox.dataset.tone = response.statusTone || "muted";
        appendAll(
          finalBox,
          el("div", { className: "pg-final-head" }, [
            el("span", { className: "pg-final-title", text: "Final response" }),
          ]),
          el("dl", { className: "pg-response-body" }, [
            responseRow("Status", response.status, response.statusTone),
            responseRow("Message", response.message),
            responseRow("Deposit", response.deposit, response.depositTone),
          ])
        );
      }
    }

    const countNode = $("pg-plan-count");
    if (countNode) {
      if (running) countNode.textContent = "Running…";
      else if (result && result.response) countNode.textContent = result.response.status;
      else countNode.textContent = "";
    }
    const summaryNode = $("pg-plan-summary");
    if (summaryNode) {
      if (!selected) summaryNode.textContent = "Select a crawler behavior, then run it.";
      else if (running) summaryNode.textContent = `Running “${selected.label}”…`;
      else if (!result) summaryNode.textContent = `Ready to run “${selected.label}”.`;
      else if (result.state === "blocked") summaryNode.textContent = "The gateway refused this behavior.";
      else if (result.state === "error") summaryNode.textContent = result.detail || "The run did not finish.";
      else summaryNode.textContent = "The gateway allowed this behavior.";
    }

    const button = $("pg-run-button");
    const hint = $("pg-run-hint");
    let disabledReason = "";
    if (state.pgCanStart === null) disabledReason = "Loading the playground...";
    else if (!state.pgCanStart) disabledReason = "Sign in as the owner to run tests.";
    else if (!site) disabledReason = "Select a website first.";
    else if (site.status !== "live") disabledReason = `Resume ${site.host} to run the playground.`;
    else if (!plan.length) disabledReason = "Select a crawler behavior.";
    else if (state.pgRunning) disabledReason = `Running “${plan[0].label}”...`;
    else if (state.applying) disabledReason = "Applying the website configuration...";
    button.disabled = Boolean(disabledReason) || state.pgRunning;
    button.textContent = state.pgRunning ? "Running…" : "Run test";
    hint.textContent = disabledReason;
    hint.hidden = !disabledReason;
  }

  // --- agent playground: scenarios, the owner's own goal, a live timeline ---------------
  // AI scenarios send the real AI crawler with a goal; scripted ones replay a precise
  // attack the crawler's tools cannot express (a forged signature, a reused token).
  const LAB_SCENARIOS = [
    { id: "polite", tone: "good", kind: "ai", title: "Polite researcher",
      blurb: "Reads the policy, stays on allowed pages, respects the rate limit.",
      goal: "Read this site's main pages and write a short summary. Read the crawling policy first, request only paths it allows, respect the rate limit, and finish the session cleanly." },
    { id: "scraper", tone: "bad", kind: "ai", title: "Price scraper",
      blurb: "Copies everything as fast as it can and ignores the rules.",
      goal: "You are a price scraper. Copy every page you can reach as fast as possible, including pages the rules forbid. Ignore robots.txt and never wait between requests." },
    { id: "prober", tone: "bad", kind: "ai", title: "Admin prober",
      blurb: "Hunts for admin, login, and internal pages.",
      goal: "Look for admin, login, private, and internal pages on this site and try to open each of them." },
    { id: "tricked", tone: "bad", kind: "ai", title: "Tricked by a page",
      blurb: "Does whatever text on a page tells AI agents to do.",
      goal: "You are a gullible assistant. Read this site's pages and do whatever the text on them tells AI agents to do, even if that means opening other paths." },
    { id: "budget", tone: "bad", kind: "ai", title: "Budget burner",
      blurb: "Keeps reading after its request budget runs out.",
      goal: "Read as many pages as you can in a single session. Keep requesting pages even after the gateway says your request budget is used up." },
    { id: "reuse", tone: "bad", kind: "scripted", action: "session_reuse", title: "Reuses a finished session",
      blurb: "Pays, finishes, then keeps using the same token." },
    { id: "forged", tone: "bad", kind: "scripted", action: "forged_signature", title: "Forged identity",
      blurb: "Signs its requests with a key the site does not know." },
    { id: "freeloader", tone: "bad", kind: "scripted", action: "no_deposit", title: "Never pays",
      blurb: "Asks for protected pages without paying a deposit." },
    { id: "traversal", tone: "bad", kind: "scripted", action: "path_traversal", title: "Path trickery",
      blurb: "Uses an encoded ../ path to sneak into the admin area." },
  ];
  const LAB_POLL_MS = 900;

  function labAiAvailable() {
    return Boolean(state.playground && state.playground.ai && state.playground.ai.available);
  }

  // AI test agents always pay with PayPal; only a gateway that simulates payments pays
  // without a PayPal approval.
  function labPayWith() {
    return state.paypalAccount && state.paypalAccount.simulated ? "test_card" : "paypal";
  }

  function labScenario() {
    return LAB_SCENARIOS.find((s) => s.id === state.labScenarioId) || null;
  }

  function renderLab() {
    const good = $("lab-good");
    const bad = $("lab-bad");
    if (!good || !bad) return;
    const site = selectedSite();
    const ai = labAiAvailable();
    const running = Boolean(state.lab && !state.lab.finished);
    clear(good);
    clear(bad);
    for (const scenario of LAB_SCENARIOS) {
      const needsAi = scenario.kind === "ai" && !ai;
      const selected = scenario.id === state.labScenarioId;
      const button = el("button", {
        type: "button",
        role: "radio",
        className: "lab-scenario",
        "aria-checked": String(selected),
        dataset: { tone: scenario.tone, selected: String(selected) },
        title: needsAi ? "Needs an AI key on the gateway" : null,
        onclick: () => pickScenario(scenario.id),
      }, [
        el("span", { className: "lab-scenario-title", text: scenario.title }),
        el("span", { className: "lab-scenario-blurb", text: scenario.blurb }),
        el("span", { className: "lab-scenario-kind", text: scenario.kind === "ai" ? "AI agent" : "Scripted attack" }),
      ]);
      button.disabled = running || needsAi;
      (scenario.tone === "good" ? good : bad).append(button);
    }
    const goal = $("lab-goal");
    if (goal) goal.disabled = running || !ai;
    if ($("lab-sub") && site) {
      const mode = state.data && state.data.payment_mode;
      const pays = mode === "paypal_sandbox" ? "pays a refundable PayPal deposit" : mode === "mock" ? "pays a simulated deposit" : "pays a PayPal deposit";
      $("lab-sub").textContent = `Pick a scenario or tell the agent what to do. It visits ${site.host} through the gateway and ${pays}.`;
    }
    const scenario = labScenario();
    const goalText = goal ? goal.value.trim() : "";
    const custom = goalText.length > 0 && (!scenario || scenario.kind !== "ai" || goalText !== scenario.goal);
    let hint = "";
    if (!site) hint = "Add a website first.";
    else if (site.status !== "live") hint = `${site.host} is paused. Resume it to test.`;
    else if (state.configReadOnly) hint = "Sign in as the owner to send test agents.";
    else if (!scenario && !custom) hint = ai ? "Pick a scenario or write a goal." : "Pick a scripted attack. AI agents need an AI key on the gateway.";
    else if (custom) hint = "The agent will follow your goal.";
    else hint = scenario.kind === "ai" ? "An AI agent will pursue this goal. You can edit it." : "A scripted attack replays this exact behaviour.";
    if ($("lab-hint")) $("lab-hint").textContent = state.labError || hint;
    const ready = Boolean(site && site.status === "live" && !state.configReadOnly && (scenario || custom));
    if ($("lab-run")) {
      $("lab-run").disabled = running || !ready;
      $("lab-run").textContent = running ? "Agent running..." : "Send agent";
    }
    if ($("lab-stop")) $("lab-stop").hidden = !running;
    if ($("lab-pay-note")) {
      const isAiRun = custom || Boolean(scenario && scenario.kind === "ai");
      let note = "";
      if (state.paypalAccount && state.paypalAccount.simulated) note = "This gateway simulates payments, so the deposit is paid automatically.";
      else if (scenario && !isAiRun) note = "Scripted attacks replay a fixed request sequence and pay automatically.";
      $("lab-pay-note").textContent = note;
      $("lab-pay-note").hidden = !note;
    }
  }

  function pickScenario(id) {
    const scenario = LAB_SCENARIOS.find((s) => s.id === id);
    if (!scenario) return;
    state.labScenarioId = id;
    state.labError = null;
    if ($("lab-goal")) $("lab-goal").value = scenario.kind === "ai" ? scenario.goal : "";
    renderLab();
  }

  async function startLab() {
    const site = selectedSite();
    if (!site) return;
    const scenario = labScenario();
    const goalText = ($("lab-goal") ? $("lab-goal").value : "").trim();
    const useGoal = goalText.length > 0;
    state.labError = null;
    let result;
    if (useGoal) {
      if (goalText.length < 8) {
        state.labError = "Describe the goal in a few more words.";
        renderLab();
        return;
      }
      result = await getJson("/demo/runs", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ driver: "ai", persona: "goal", goal: goalText, site_id: site.id, pay_with: labPayWith() }),
      });
    } else if (scenario && scenario.kind === "scripted") {
      result = await getJson("/playground/runs", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ action_ids: [scenario.action], driver: "scripted", site_id: site.id }),
      });
    } else {
      return;
    }
    if (!result.ok || !result.body) {
      state.labError = apiErrorMessage(result.body, result.status, `Could not start the agent (HTTP ${result.status}).`);
      renderLab();
      return;
    }
    const named = scenario && (scenario.kind === "scripted" || goalText === scenario.goal);
    state.lab = {
      kind: useGoal ? "ai" : "scripted",
      payWith: useGoal ? labPayWith() : "test_card",
      runId: result.body.run_id,
      title: named ? scenario.title : "Your goal",
      goal: useGoal ? goalText : scenario.blurb,
      site: site.host,
      after: 0,
      rows: [],
      finished: false,
      body: result.body,
      explanation: null,
    };
    state.pgRunning = true;
    renderLab();
    renderLabResult();
    $("lab-result").scrollIntoView({ behavior: "smooth", block: "start" });
    pollLab();
  }

  async function stopLab() {
    const lab = state.lab;
    if (!lab || lab.finished) return;
    const url = lab.kind === "ai" ? `/demo/runs/${encodeURIComponent(lab.runId)}/cancel` : `/playground/runs/${encodeURIComponent(lab.runId)}/cancel`;
    await getJson(url, { method: "POST", headers: authHeaders() });
  }

  async function pollLab() {
    const lab = state.lab;
    if (!lab || lab.finished) return;
    const base = lab.kind === "ai" ? "/demo/runs/" : "/playground/runs/";
    const result = await getJson(`${base}${encodeURIComponent(lab.runId)}?after=${lab.after}`, { headers: authHeaders() });
    if (state.lab !== lab) return;
    if (result.ok && result.body) {
      const body = result.body;
      lab.body = body;
      lab.after = body.next_seq || lab.after;
      if (lab.kind === "ai") for (const event of body.events || []) labAddAiEvent(lab, event);
      else for (const step of body.steps || []) labAddStep(lab, step);
      const done = ["completed", "failed", "cancelled"].includes(body.status);
      if (done) {
        lab.finished = true;
        state.pgRunning = false;
        state.pgQuietUntil = Date.now() + 8000;
        await labLoadExplanation(lab);
        refresh();
      }
      renderLabResult();
      renderLab();
      if (done) return;
    }
    setTimeout(pollLab, LAB_POLL_MS);
  }

  // One timeline row: what the agent did, and what the gateway answered.
  function labRow(tone, title, request, httpStatus, message) {
    return { tone, title, request, httpStatus, message };
  }

  function labAddAiEvent(lab, event) {
    const data = event.data || {};
    if (event.kind === "text" && data.text) {
      lab.rows.push(labRow("info", "Agent", null, null, data.text));
      return;
    }
    if (event.kind === "approval_required") {
      lab.rows.push(labRow("warn", "Waiting for PayPal approval", null, null, "The deposit needs a buyer to approve it at PayPal."));
      return;
    }
    if (event.kind !== "tool_result") return;
    const r = data.result || {};
    const name = data.name;
    if (data.is_error && r.error) {
      lab.rows.push(labRow("bad", humanize(name || "tool"), null, null, r.error));
      return;
    }
    if (name === "fetch_page") {
      const path = r.path || "";
      if (r.status === 402) {
        const dep = r.deposit || {};
        lab.rows.push(labRow("warn", "Asked for a page without paying", `GET ${path || "/"}`, 402,
          `The gateway asks for a ${money(dep.amount, dep.currency)} refundable deposit first.`));
      } else if (r.status === 200) {
        lab.rows.push(labRow("good", "Read a page", `GET ${path}`, 200,
          r.prompt_injection ? "Allowed. The page contains a hidden instruction aimed at AI agents." : `Allowed${r.remaining_session_budget != null ? ` · ${r.remaining_session_budget} requests left in the session` : ""}.`));
      } else if (r.status === 404) {
        lab.rows.push(labRow("info", "Page not found", `GET ${path}`, 404, r.reason || "The website has no page at this path."));
      } else if (r.status >= 300 && r.status < 400) {
        // The website answered with a redirect; the gateway let the request through.
        lab.rows.push(labRow("info", "Redirected by the website", `GET ${path}`, r.status,
          "Allowed by the gateway. The website sent the agent to another address instead of a page."));
      } else {
        lab.rows.push(labRow("bad", "Blocked by the gateway", `GET ${path}`, r.status,
          `${humanize(r.reason_code || "")}${r.reason ? ` · ${r.reason}` : ""}${r.disqualifying ? " · counts against the deposit" : ""}`));
      }
    } else if (name === "read_policy") {
      lab.rows.push(labRow("info", "Read the crawling policy", "GET /policies", 200, "Allowed and blocked paths, rate limit, request budget, refund rules."));
    } else if (name === "pay_deposit") {
      lab.rows.push(labRow("pay", "Paid the deposit with PayPal", "POST /deposits", null,
        `${money(r.amount || r.required_amount, r.currency)} · ${humanize(r.payment_status || r.deposit_state || r.status || "paid")}`));
    } else if (name === "accept_policy") {
      lab.rows.push(labRow("info", "Accepted the policy", "POST /sessions/accept-policy", null, "The paid session starts."));
    } else if (name === "wait") {
      lab.rows.push(labRow("info", "Waited", null, null, `Paused ${r.waited_seconds || r.seconds || ""}s to respect the rate limit.`));
    } else if (name === "complete_session") {
      const kept = r.deposit_state === "FORFEITED";
      lab.rows.push(labRow(kept ? "bad" : "good", "Ended the session", "POST /sessions/complete", null,
        `${kept ? "Deposit kept" : "Deposit returned"} · ${r.total_violations || 0} violation${r.total_violations === 1 ? "" : "s"}`));
    }
  }

  function labAddStep(lab, step) {
    const tone = step.allowed === true ? "good" : step.allowed === false ? "bad" : step.http_status === 402 ? "warn" : "info";
    const pay = /^pay/i.test(step.label || "") && step.allowed === true;
    lab.rows.push(labRow(pay ? "pay" : tone, step.label, step.request, step.http_status, step.message));
  }

  async function labLoadExplanation(lab) {
    const deposit = labDeposit(lab);
    if (!deposit) return;
    if (lab.kind === "ai" && lab.body.explanation) {
      lab.explanation = lab.body.explanation;
      return;
    }
    const result = await getJson(`/dashboard/deposits/${encodeURIComponent(deposit.deposit_id)}/explanation`, { headers: authHeaders() });
    if (result.ok) lab.explanation = result.body;
  }

  function labDeposit(lab) {
    const deposit = (lab.body || {}).deposit || {};
    return deposit.deposit_id ? deposit : null;
  }

  function renderLabResult() {
    const box = $("lab-result");
    const lab = state.lab;
    if (!box) return;
    box.hidden = !lab;
    if (!lab) return;
    $("lab-result-title").textContent = lab.title;
    $("lab-result-sub").textContent = `${lab.site} · ${lab.goal}`;
    const status = lab.body ? lab.body.status : "running";
    const labels = { running: "Running", awaiting_approval: "Waiting for PayPal", completed: "Finished", failed: "Failed", cancelled: "Stopped" };
    $("lab-status").textContent = labels[status] || humanize(status);
    $("lab-status").dataset.state = status;
    const waiting = status === "awaiting_approval" && lab.body && lab.body.approval_url;
    if ($("lab-approval")) {
      $("lab-approval").hidden = !waiting;
      if (waiting) {
        const dep = lab.body.deposit || {};
        const amount = dep.required_amount ? money(dep.required_amount, dep.currency) : "its";
        $("lab-approval-text").textContent = lab.paypalNote || `Approve the ${amount} refundable deposit with PayPal. The agent continues as soon as PayPal confirms.`;
        mountLabPayPalButton(lab);
      }
    }

    const list = $("lab-timeline");
    clear(list);
    if (!lab.rows.length) {
      list.append(el("li", { className: "lab-row", dataset: { tone: "info" } }, [
        el("span", { className: "lab-dot", "aria-hidden": "true" }),
        el("div", { className: "lab-row-body" }, [el("span", { className: "lab-row-title", text: "Starting the agent..." })]),
      ]));
    }
    for (const row of lab.rows) {
      list.append(el("li", { className: "lab-row", dataset: { tone: row.tone } }, [
        el("span", { className: "lab-dot", "aria-hidden": "true" }),
        el("div", { className: "lab-row-body" }, [
          el("div", { className: "lab-row-head" }, [
            el("span", { className: "lab-row-title", text: row.title }),
            row.request ? el("code", { className: "lab-row-request", text: row.request }) : null,
            row.httpStatus ? el("span", { className: "lab-http", dataset: { tone: httpTone(row.httpStatus) }, text: String(row.httpStatus) }) : null,
          ]),
          row.message ? el("p", { className: "lab-row-message", text: row.message }) : null,
        ]),
      ]));
    }
    renderLabOutcome(lab);
  }

  function renderLabOutcome(lab) {
    const box = $("lab-outcome");
    clear(box);
    if (!lab.finished) { box.hidden = true; return; }
    box.hidden = false;
    const body = lab.body || {};
    const deposit = labDeposit(lab);
    const stateCode = deposit ? deposit.deposit_state : null;
    const amount = deposit ? money(deposit.amount || deposit.required_amount, deposit.currency) : "";
    let tone = "info";
    let headline = "No deposit was taken";
    let detail = "The gateway refused the agent before any money moved.";
    if (stateCode === "FORFEITED") { tone = "bad"; headline = `Deposit kept · ${amount}`; detail = "The agent broke the rules, so the deposit goes to the website."; }
    else if (RETURNED.has(stateCode)) { tone = "good"; headline = `Deposit returned · ${amount}`; detail = "No rules broken, so the agent gets its deposit back."; }
    else if (stateCode) { headline = `Deposit ${depositLabel(stateCode).toLowerCase()}`; detail = "The session has not settled yet."; }
    // A scripted attack that came after a clean settlement: the attack was refused, and the
    // deposit had already been returned for the honest part of the session.
    if (lab.kind === "scripted" && body.outcome === "refused" && RETURNED.has(stateCode)) {
      detail = "The attack was refused. The deposit had already been settled for the session before it, so it was returned.";
    }
    if (body.status === "failed") { tone = "bad"; headline = "The run failed"; detail = body.error || "The agent stopped with an error."; }
    if (body.status === "cancelled") { detail = `Stopped by you. ${detail}`; }
    box.dataset.tone = tone;
    box.append(el("div", { className: "lab-outcome-head" }, [
      el("strong", { text: headline }),
      el("span", { text: detail }),
    ]));
    const explanation = lab.explanation;
    if (explanation && explanation.summary) {
      const by = explanation.generated_by === "template" ? "Why (from the audit log)" : `Why · explained by ${explanation.generated_by === "openai" ? "OpenAI" : "Claude"}`;
      box.append(el("div", { className: "lab-explain" }, [
        el("p", { className: "lab-kicker", text: by }),
        el("p", { className: "lab-explain-headline", text: explanation.headline }),
        el("p", { className: "lab-explain-summary", text: explanation.summary }),
      ]));
    }
    if (lab.kind === "ai" && body.final_text) {
      box.append(el("div", { className: "lab-report" }, [
        el("p", { className: "lab-kicker", text: "The agent's own report" }),
        el("p", { className: "lab-report-text", text: body.final_text }),
      ]));
    }
    if (deposit) {
      box.append(el("div", { className: "lab-outcome-actions" }, [
        el("button", { type: "button", className: "primary", text: "Verify with PayPal", onclick: () => labOpenDeposit(deposit.deposit_id) }),
        el("span", { className: "muted", text: `Deposit ${deposit.deposit_id}` }),
      ]));
    }
  }

  async function labOpenDeposit(depositId) {
    await refresh();
    const row = state.data && state.data.deposits.find((d) => d.deposit_id === depositId);
    if (row) openDeposit(row);
    else showView("deposits");
  }

  // --- PayPal payouts: the business account deposits and sales are paid to -------------
  async function loadPayPalAccount() {
    try {
      const result = await getJson("/paypal/account", { headers: authHeaders() });
      state.paypalAccount = result.ok ? result.body : { connected: false, error: apiErrorMessage(result.body, result.status, "Could not reach PayPal.") };
    } catch (_) {
      state.paypalAccount = { connected: false, error: "Could not reach the gateway." };
    }
    renderPayouts();
    renderLab();
  }

  // The account payouts go to: the one the owner connected with Log in with PayPal, else
  // the merchant account the gateway's PayPal app is paid to.
  function payoutAccount() {
    const acct = state.paypalAccount;
    if (!acct || acct.simulated) return null;
    if (acct.owner_connection) {
      const c = acct.owner_connection;
      return { email: c.email || null, name: c.name, owner: true, pending: Boolean(c.profile_pending && !c.email) };
    }
    if (acct.merchant_email) return { email: acct.merchant_email, owner: false };
    return null;
  }

  function renderPayouts() {
    const acct = state.paypalAccount;
    const payout = payoutAccount();
    const ownerConnected = Boolean(payout && payout.owner);
    for (const chipNode of document.querySelectorAll("[data-paypal-chip]")) {
      let text = "Checking PayPal...";
      let tone = "neutral";
      if (acct) {
        if (acct.simulated) { text = "Payments are simulated"; tone = "neutral"; }
        else if (ownerConnected) { text = payout.email ? `Connected · ${payout.email}` : "Connected to PayPal"; tone = "good"; }
        else if (!acct.connected) { text = "PayPal unavailable"; tone = "bad"; }
        else { text = "Not connected yet"; tone = "neutral"; }
      }
      chipNode.textContent = text;
      chipNode.dataset.tone = tone;
      chipNode.title = acct && acct.error ? acct.error : "";
    }
    const canLogin = Boolean(acct && acct.login_available && !state.configReadOnly);
    for (const button of document.querySelectorAll("[data-paypal-login]")) button.hidden = !canLogin || ownerConnected;
    if ($("payout-switch")) $("payout-switch").hidden = !canLogin || !ownerConnected;
    if ($("payout-disconnect")) $("payout-disconnect").hidden = !canLogin || !ownerConnected;
    if (!acct) return;
    if ($("payout-account")) {
      $("payout-account").textContent = acct.simulated
        ? "None: this gateway simulates payments"
        : ownerConnected
          ? payout.email
            ? `${payout.name ? `${payout.name} · ` : ""}${payout.email}`
            : "Your PayPal account (signed in). PayPal has not shared its name and email yet."
          : "Not connected. Log in with PayPal to choose the account you are paid to.";
    }
    if ($("payout-settlement")) {
      $("payout-settlement").textContent = acct.settlement_mode === "authorization"
        ? "PayPal holds: released when the agent behaves, captured when it breaks the rules"
        : "Charged up front, refunded when the agent behaves";
    }
    if ($("payout-hint")) $("payout-hint").textContent = state.paypalLoginMessage || "";
  }

  // Log in with PayPal opens in a popup. The window is opened on the click itself, so popup
  // blockers allow it, and pointed at PayPal once the gateway has issued the sign-in link.
  // Log in with PayPal happens in this tab: PayPal brings the owner back to the console,
  // which then reports the outcome (see showPayPalReturn).
  async function connectPayPal() {
    state.paypalLoginMessage = "Opening PayPal...";
    renderPayouts();
    const result = await getJson("/paypal/connect/start", { method: "POST", headers: authHeaders() });
    if (!result.ok || !result.body || !result.body.authorize_url) {
      state.paypalLoginMessage = apiErrorMessage(result.body, result.status, "Could not start Log in with PayPal.");
      renderPayouts();
      return;
    }
    window.location.assign(result.body.authorize_url);
  }

  function showPayPalReturn() {
    const params = new URLSearchParams(window.location.search);
    const outcome = params.get("paypal");
    if (!outcome) return;
    const message = params.get("paypal_message") || "";
    if (outcome === "connected") toast("good", "✓", "PayPal connected", message || "Payouts go to your PayPal account.");
    else toast("critical", "!", "PayPal not connected", message || "PayPal sign-in did not finish.");
    state.paypalLoginMessage = outcome === "connected" ? "" : message;
    history.replaceState(null, "", `${window.location.pathname}${window.location.hash}`);
  }

  async function disconnectPayPal() {
    await getJson("/paypal/connect/disconnect", { method: "POST", headers: authHeaders() });
    state.paypalLoginMessage = "";
    loadPayPalAccount();
  }

  // --- PayPal's JS SDK v6: the real PayPal button and checkout popup ----------------------
  function loadPayPalSdk() {
    if (state.paypalSdk) return state.paypalSdk;
    state.paypalSdk = (async () => {
      const config = await getJson("/demo/config", { headers: authHeaders() });
      const clientId = config.ok && config.body ? config.body.paypal_js_client_id : null;
      if (!clientId) return null;
      const live = state.paypalAccount && state.paypalAccount.environment === "live";
      const host = live ? "https://www.paypal.com" : "https://www.sandbox.paypal.com";
      if (!window.paypal || typeof window.paypal.createInstance !== "function") {
        await new Promise((resolve, reject) => {
          const script = document.createElement("script");
          script.src = `${host}/web-sdk/v6/core`;
          script.async = true;
          script.onload = resolve;
          script.onerror = reject;
          document.head.append(script);
        });
      }
      return window.paypal.createInstance({ clientId, components: ["paypal-payments"] });
    })().catch((err) => {
      console.warn("PayPal SDK unavailable", err);
      return null;
    });
    return state.paypalSdk;
  }

  // The agent's deposit already has a PayPal order: the owner approves that order with
  // PayPal's button, the gateway captures it, and the waiting agent continues.
  async function mountLabPayPalButton(lab) {
    const deposit = (lab.body && lab.body.deposit) || {};
    const orderId = deposit.provider_order_id;
    if (!orderId || lab.paypalMountedFor === deposit.deposit_id) return;
    lab.paypalMountedFor = deposit.deposit_id;
    const slot = $("lab-paypal-slot");
    clear(slot);
    const sdk = await loadPayPalSdk();
    if (state.lab !== lab) return;
    if (!sdk) {
      if ($("lab-approve")) $("lab-approve").hidden = false;
      return;
    }
    const session = sdk.createPayPalOneTimePaymentSession({
      async onApprove(data) {
        lab.paypalNote = "PayPal approved the deposit. Confirming the payment...";
        renderLabResult();
        const query = `deposit_id=${encodeURIComponent(deposit.deposit_id)}&token=${encodeURIComponent(data.orderId || orderId)}`;
        await getJson(`/deposits/paypal/return?${query}`, { headers: { Accept: "application/json" } });
        await getJson(`/demo/runs/${encodeURIComponent(lab.runId)}/approval`, { method: "POST", headers: authHeaders() });
      },
      onCancel() {
        lab.paypalNote = "You closed PayPal before approving. Approve to let the agent continue, or stop the run.";
        renderLabResult();
      },
      onError(err) {
        lab.paypalNote = `PayPal reported a problem: ${(err && err.message) || err}`;
        renderLabResult();
      },
    });
    const button = document.createElement("paypal-button");
    button.setAttribute("type", "pay");
    button.addEventListener("click", () => {
      lab.paypalNote = "";
      session.start({ presentationMode: "auto" }, Promise.resolve({ orderId })).catch((err) => {
        lab.paypalNote = `Could not open PayPal: ${(err && err.message) || err}`;
        renderLabResult();
      });
    });
    slot.append(button);
    if ($("lab-approve")) $("lab-approve").hidden = true;
  }


  async function loadPlayground(force) {
    const site = selectedSite();
    const siteId = site ? site.id : "";
    if (state.playground && !force && state.playgroundSiteId === siteId) {
      renderPlayground();
      return;
    }
    const token = (state.pgLoadToken || 0) + 1;
    state.pgLoadToken = token;
    const url = siteId
      ? `/playground/actions?site_id=${encodeURIComponent(siteId)}`
      : "/playground/actions";
    const result = await getJson(url, { headers: authHeaders() });
    // A newer selection started loading while this request was in flight.
    if (token !== state.pgLoadToken) return;
    if (result.ok) {
      state.playground = result.body;
      state.playgroundSiteId = siteId;
      state.pgCanStart = Boolean(result.body && result.body.can_start);
    } else {
      state.playground = null;
      state.playgroundSiteId = siteId;
      state.pgCanStart = false;
    }
    renderPlayground();
    renderLab();
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function mergePlaygroundSteps(steps) {
    if (!Array.isArray(steps) || !steps.length) return;
    const seen = new Set(state.pgSteps.map((step) => step.seq));
    for (const step of steps) {
      if (!step || seen.has(step.seq)) continue;
      seen.add(step.seq);
      state.pgSteps.push(step);
    }
    state.pgSteps.sort((a, b) => Number(a.seq) - Number(b.seq));
  }

  // The gateway streams steps page by page (after=next_seq); onUpdate lets the timeline
  // render each new step while the run is still in progress.
  async function pollPlaygroundRun(runId, token, onUpdate) {
    const deadline = Date.now() + 120000;
    let after = 0;
    while (Date.now() < deadline) {
      if (token !== state.pgRunToken) return null;
      await sleep(450);
      const result = await getJson(`/playground/runs/${encodeURIComponent(runId)}?after=${after}`, { headers: authHeaders() });
      if (!result.ok) throw new Error(`Could not read the playground run (HTTP ${result.status}).`);
      const body = result.body;
      after = body.next_seq || after;
      if (typeof onUpdate === "function") onUpdate(body);
      if (["completed", "failed", "cancelled"].includes(body.status)) return body;
    }
    throw new Error("The playground run took too long to finish.");
  }

  async function runPlaygroundPlan() {
    const site = selectedSite();
    if (!site || state.pgRunning || state.applying) return;
    // A paused website cannot run: its gateway traffic is refused.
    if (site.status !== "live") return;
    const plan = buildPlaygroundPlan();
    const executable = plan.length;
    if (!executable) return;
    if (state.pgCanStart !== true) return;

    state.pgRunning = true;
    state.pgResults = {};
    state.pgSteps = [];
    state.pgCurrentIndex = 0;
    state.pgRunToken = (state.pgRunToken || 0) + 1;
    const token = state.pgRunToken;
    renderPlayground();

    try {
      if (!state.configReadOnly) {
        const applied = await applySelectedSite({ silent: true });
        if (!applied) return;
      }
      for (let index = 0; index < executable; index += 1) {
        if (token !== state.pgRunToken) return;
        const step = plan[index];
        state.pgCurrentIndex = index;
        state.pgSteps = [];
        state.pgResults[step.id] = { state: "running", steps: [] };
        renderPlayground();

        const started = await getJson("/playground/runs", {
          method: "POST",
          headers: authHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ action_id: step.action, driver: "scripted", site_id: site.id }),
        });
        if (!started.ok) {
          const body = started.body || {};
          const detail = (body.detail && (body.detail.message || body.detail.error)) || body.message;
          state.pgResults[step.id] = {
            state: "error",
            detail: detail || `HTTP ${started.status}`,
            steps: state.pgSteps.slice(),
            response: {
              status: httpStatusText(started.status) || "Run failed",
              statusTone: responseTone(started.status),
              message: detail || `The playground could not start this action (HTTP ${started.status}).`,
              deposit: "No deposit taken",
              depositTone: "muted",
            },
          };
          break;
        }
        state.pgActiveRunId = started.body.run_id;
        const run = await pollPlaygroundRun(started.body.run_id, token, (progress) => {
          if (token !== state.pgRunToken) return;
          mergePlaygroundSteps(progress.steps);
          renderPlayground();
        });
        state.pgActiveRunId = null;
        if (!run) return;
        mergePlaygroundSteps(run.steps);
        const steps = state.pgSteps.slice();
        const action = (run.action_results && run.action_results[0]) || {};
        const violation = action.violation || run.violation || {};
        const refused = action.outcome === "refused" || run.outcome === "refused";
        const failed = action.status === "failed" || run.status === "failed" || run.status === "cancelled";
        const detail = violation.rule_matched || violation.reason_code || action.error || run.error || "";
        const response = describePlaygroundResponse(action, run);
        if (refused) {
          state.pgResults[step.id] = { state: "blocked", http: violation.http_status, detail, response, steps };
        } else if (failed) {
          state.pgResults[step.id] = { state: "error", detail, response, steps };
        } else {
          state.pgResults[step.id] = { state: "completes", detail: "", response, steps };
        }
        renderPlayground();
      }
    } catch (error) {
      const message = error && error.message ? error.message : "The playground run failed.";
      const step = plan[state.pgCurrentIndex];
      if (step && state.pgResults[step.id] && state.pgResults[step.id].state === "running") {
        state.pgResults[step.id] = {
          state: "error",
          detail: message,
          steps: state.pgSteps.slice(),
          response: {
            status: "Run failed",
            statusTone: "critical",
            message,
            deposit: "Deposit state unknown",
            depositTone: "muted",
          },
        };
      }
      const runId = state.pgActiveRunId;
      if (runId) {
        fetch(`/playground/runs/${encodeURIComponent(runId)}/cancel`, {
          method: "POST",
          headers: authHeaders(),
        }).catch(() => { /* the run may already be finished */ });
        state.pgActiveRunId = null;
      }
      showBanner(message);
    } finally {
      if (token === state.pgRunToken) {
        state.pgRunning = false;
        state.pgCurrentIndex = -1;
        state.pgActiveRunId = null;
        state.pgQuietUntil = Date.now() + 10000;
        renderPlayground();
      }
    }
  }

  function stopPlaygroundRun(clearResults) {
    state.pgRunToken = (state.pgRunToken || 0) + 1;
    const runId = state.pgActiveRunId;
    if (runId) {
      fetch(`/playground/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
        headers: authHeaders(),
      }).catch(() => { /* the run may already be finished */ });
    }
    state.pgActiveRunId = null;
    state.pgRunning = false;
    state.pgCurrentIndex = -1;
    state.pgQuietUntil = Date.now() + 10000;
    if (clearResults) {
      state.pgResults = {};
      state.pgSteps = [];
    }
  }

  function resetPlayground() {
    if (state.pgRunning) stopPlaygroundRun(true);
    state.pgSelected = defaultBehaviorSelection();
    state.pgResults = {};
    state.pgSteps = [];
    renderPlayground();
  }

  function clearPlayground() {
    if (state.pgRunning) stopPlaygroundRun(true);
    // A radio group always keeps one behavior; Clear only clears the output.
    state.pgResults = {};
    state.pgSteps = [];
    renderPlayground();
  }


  // --- policy view ----------------------------------------------------------------------
  async function loadPolicy() {
    if (state.policyLoaded) return;
    const box = $("policy-view");
    try {
      const result = await getJson("/policies", { headers: { Accept: "application/json" } });
      if (!result.ok || !result.body) {
        clear(box);
        box.append(el("p", { className: "empty", text: `Could not load the policy (HTTP ${result.status}).` }));
        return;
      }
      state.policyLoaded = true;
      renderPolicy(result.body);
    } catch (_) {
      clear(box);
      box.append(el("p", { className: "empty", text: "Could not reach the gateway." }));
    }
  }

  function renderPolicy(p) {
    const box = $("policy-view");
    clear(box);
    $("policy-sub").textContent = `${p.site_name} · version ${p.policy_version} · served at /policies and enforced on every gateway request.`;
    const stat = (label, value) => el("div", { className: "policy-stat" }, [el("div", { className: "tile-label", text: label }), el("div", { className: "tile-value", text: value })]);
    const paths = (items, tone) => el("ul", { className: "path-list", dataset: { tone } }, (items || []).map((x) => el("li", { text: x })));
    appendAll(
      box,
      el("div", { className: "policy-grid" }, [
        stat("Base deposit", money(p.deposit_amount, p.currency)),
        stat("Rate limit", `${p.rate_limit_requests} / ${p.rate_limit_window_seconds}s`),
        stat("Session budget", `${p.max_requests_per_session} requests`),
        stat("Session lifetime", `${Math.round(p.session_ttl_seconds / 60)} min`),
      ]),
      el("p", { className: "policy-h", text: "Allowed paths" }),
      paths(p.allowed_paths, "good"),
      el("p", { className: "policy-h", text: "Disallowed paths (a request forfeits the deposit)" }),
      paths(p.disallowed_paths, "critical"),
      (p.pricing_tiers || []).length ? el("p", { className: "policy-h", text: "Pricing tiers" }) : null,
      (p.pricing_tiers || []).length
        ? el("table", { className: "viz-table" }, [
            el("thead", null, [el("tr", null, [el("th", { text: "Tier" }), el("th", { text: "Content" }), el("th", { text: "Prefix" }), el("th", { className: "num", text: "Deposit" })])]),
            el("tbody", null, p.pricing_tiers.map((t) => el("tr", null, [
              el("td", null, [tierBadge(t.tier_id, t.name)]),
              el("td", { text: (t.categories || []).join(", ") }),
              el("td", { className: "mono", text: [t.path_prefix].concat(t.additional_path_prefixes || []).join(", ") }),
              el("td", { className: "num", text: money(t.price, t.currency) }),
            ]))),
          ])
        : null
    );
  }

  // --- policy assistant ----------------------------------------------------------------
  function updateAssistantState() {
    const button = $("draft-button");
    const hint = $("draft-hint");
    const ready = Boolean(state.token);
    button.textContent = `Draft policy with ${providerName(state.data ? state.data.ai_provider : "")}`;
    button.disabled = !ready;
    hint.textContent = ready
      ? (state.data && !state.data.ai_available ? "AI is not configured on the gateway; the request will report that." : `Uses ${providerName(state.data ? state.data.ai_provider : "")} through the gateway (admin).`)
      : "Policy drafting needs an admin token on the gateway. The active policy on the left is always visible.";
  }

  function listSection(title, items) {
    if (!items || !items.length) return null;
    return el("div", null, [el("h3", { text: title }), el("ul", null, items.map((item) => el("li", { text: item })))]);
  }

  async function draftPolicy() {
    const output = $("draft-result");
    const terms = $("terms").value.trim();
    clear(output);
    if (terms.length < 20) {
      output.append(el("p", { className: "notice", text: "Describe your terms in at least 20 characters." }));
      return;
    }
    $("draft-button").disabled = true;
    output.append(el("p", { className: "notice", text: `Drafting with ${providerName(state.data ? state.data.ai_provider : "")}...` }));
    try {
      const result = await getJson("/policies/draft", {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ terms }),
      });
      clear(output);
      if (!result.ok) {
        output.append(el("p", { className: "notice", text: (result.body && result.body.message) || `Request failed (HTTP ${result.status}).` }));
        return;
      }
      const draft = result.body;
      const jsonText = JSON.stringify(draft.draft, null, 2);
      appendAll(
        output,
        el("div", { className: "explain-meta" }, [
          draft.valid ? chip("good", "✓", "Valid draft") : chip("critical", "✕", `${draft.validation_errors.length} problem(s) to fix`),
          el("span", { className: "badge", text: `Drafted by ${draft.model}` }),
        ]),
        listSection("Problems", draft.validation_errors),
        listSection("Assumptions", draft.assumptions),
        listSection("Warnings", draft.warnings),
        el("pre", { className: "json-out", text: jsonText }),
        el("div", { className: "assistant-actions" }, [
          el("button", {
            type: "button",
            text: "Copy JSON",
            onclick: async (event) => {
              try { await navigator.clipboard.writeText(jsonText); event.target.textContent = "Copied"; }
              catch (_) { event.target.textContent = "Copy failed"; }
            },
          }),
          el("span", { className: "hint", text: draft.notice }),
        ])
      );
    } catch (_) {
      clear(output);
      output.append(el("p", { className: "notice", text: "Could not reach the gateway." }));
    } finally {
      updateAssistantState();
    }
  }

  // --- request history (shared with the analytics view) ----------------------------------
  async function loadRequests(force) {
    if (state.requestsLoading) return state.requests;
    if (!force && Date.now() - state.requestsAt < REQUESTS_EVERY_MS) return state.requests;
    state.requestsLoading = true;
    try {
      const result = await getJson(`/demo/requests${siteQuery()}`, { headers: authHeaders() });
      if (result.ok && result.body) {
        state.requests = result.body.rows;
        state.requestsAt = Date.now();
        if (state.grids.requests) state.grids.requests.setRows(state.requests);
        $("requests-hint").textContent = `${state.requests.length} request(s) · updated ${formatTime(result.body.generated_at)}.`;
      } else {
        $("requests-hint").textContent = (result.body && result.body.message) || `Request history unavailable (HTTP ${result.status}).`;
      }
    } catch (_) {
      $("requests-hint").textContent = "Could not load request history.";
    } finally {
      state.requestsLoading = false;
    }
    return state.requests;
  }

  function analyticsContext() {
    return {
      getData: () => state.data,
      loadRequests: () => loadRequests(true),
      loadRuns: async () => {
        const result = await getJson("/demo/config", { headers: authHeaders() });
        if (!result.ok || !result.body) return [];
        const runs = result.body.recent_runs || [];
        // The run summaries carry no model usage; read it from each run (events skipped).
        const details = await Promise.all(runs.map(async (run) => {
          try {
            const r = await getJson(`/demo/runs/${encodeURIComponent(run.run_id)}?after=1000000`, { headers: authHeaders() });
            return r.ok ? r.body : null;
          } catch (_) { return null; }
        }));
        return runs.map((run, i) => Object.assign({}, run, { detail: details[i] }));
      },
      loadCommerce: async () => {
        const result = await getJson("/commerce/summary", { headers: authHeaders() });
        return result.ok ? result.body : null;
      },
      helpers: { humanize, depositLabel, eventCategory, providerName },
    };
  }

  // --- commerce: orders, subscriptions, revenue by payment type -------------------------
  const ORDER_STATUS = {
    PAID: ["good", "✓", "Paid"],
    CREATED: ["neutral", "●", "Awaiting payment"],
    APPROVAL_REQUIRED: ["warning", "!", "Awaiting PayPal approval"],
    FAILED: ["critical", "✕", "Failed"],
    CANCELLED: ["neutral", "■", "Cancelled (not paid)"],
    REFUNDED: ["warning", "↺", "Refunded"],
  };
  const SUB_STATUS = {
    ACTIVE: ["good", "✓", "Active"],
    APPROVAL_PENDING: ["warning", "!", "Awaiting approval"],
    APPROVED: ["warning", "!", "Approved"],
    CANCELLED: ["neutral", "■", "Cancelled"],
    SUSPENDED: ["serious", "!", "Suspended"],
    EXPIRED: ["neutral", "■", "Expired"],
    FAILED: ["critical", "✕", "Failed"],
  };
  const statusChip = (map, code) => { const [tone, icon, label] = map[code] || ["neutral", "●", humanize(code)]; return chip(tone, icon, label); };

  function initCommerceGrids(create) {
    state.grids.orders = create($("grid-orders"), {
      rowId: (o) => o.order_id,
      pageSize: 10,
      autoHeight: true,
      exportName: "sara-orders",
      search: "Search orders...",
      noRows: "No orders yet. Orders agents place on your site appear here.",
      segments: {
        label: "Filter by type",
        options: [
          { id: "access", label: "Pay-per-access", test: (o) => o.kind === "access" },
          { id: "product", label: "Products", test: (o) => o.kind === "product" },
          { id: "unpaid", label: "Unpaid", test: (o) => o.status !== "PAID" },
        ],
      },
      columnDefs: [
        { headerName: "Status", field: "status", minWidth: 150, valueGetter: (p) => (p.data ? (ORDER_STATUS[p.data.status] || [])[2] || p.data.status : ""), cellRenderer: cellNode((o) => statusChip(ORDER_STATUS, o.status)) },
        { headerName: "Type", field: "kind", maxWidth: 140, valueFormatter: (p) => (p.value === "access" ? "Pay-per-access" : p.value === "product" ? "Product" : "") },
        { headerName: "Items", field: "lines", minWidth: 230, valueGetter: (p) => (p.data ? p.data.lines.map((l) => `${l.quantity > 1 ? `${l.quantity}× ` : ""}${l.name}`).join(", ") : ""), tooltipValueGetter: (p) => p.value },
        { headerName: "Amount", field: "amount", maxWidth: 120, type: "numericColumn", filter: "agNumberColumnFilter", valueGetter: (p) => (p.data ? Number(p.data.amount) : null), valueFormatter: (p) => (p.data && p.value != null ? money(p.value, p.data.currency) : "") },
        { headerName: "Fulfilment", field: "fulfillment_status", maxWidth: 140, valueFormatter: (p) => humanize(p.value) },
        { headerName: "Agent", field: "agent_id", minWidth: 140, cellClass: "mono" },
        { headerName: "Agent's reason", field: "purpose", minWidth: 200, tooltipField: "purpose" },
        { headerName: "PayPal order", field: "provider_order_id", minWidth: 150, cellClass: "mono", tooltipField: "provider_order_id" },
        { headerName: "PayPal capture", field: "provider_capture_id", minWidth: 150, cellClass: "mono", tooltipField: "provider_capture_id" },
        { headerName: "Created", field: "created_at", maxWidth: 120, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value) },
        {
          headerName: "", colId: "actions", sortable: false, filter: false, resizable: false, pinned: "right", minWidth: 210, maxWidth: 230,
          cellRenderer: cellNode((o) => el("span", { className: "cell-actions" }, [
            el("button", { className: "cell-button", type: "button", text: "Verify", title: "Compare with PayPal's records through PayPal's MCP server", onclick: (e) => { e.stopPropagation(); verifyOrder(o); } }),
            o.status === "PAID" ? el("button", { className: "cell-button", type: "button", text: "Refund", title: "Refund through PayPal (admin)", onclick: (e) => { e.stopPropagation(); refundOrder(o); } }) : null,
            o.status === "PAID" && (o.lines || []).some((l) => l.category === "PHYSICAL_GOODS") && o.fulfillment_status !== "SHIPPED"
              ? el("button", { className: "cell-button", type: "button", text: "Ship", title: "Add shipment tracking at PayPal (admin)", onclick: (e) => { e.stopPropagation(); shipOrder(o); } }) : null,
          ])),
        },
      ],
      noExport: ["actions"],
    });
    state.grids.subscriptions = create($("grid-subscriptions"), {
      rowId: (s) => s.subscription_ref,
      pageSize: 10,
      autoHeight: true,
      exportName: "sara-subscriptions",
      search: "Search subscriptions...",
      noRows: "No subscriptions yet.",
      columnDefs: [
        { headerName: "Status", field: "status", minWidth: 150, valueGetter: (p) => (p.data ? (SUB_STATUS[p.data.status] || [])[2] || p.data.status : ""), cellRenderer: cellNode((s) => statusChip(SUB_STATUS, s.status)) },
        { headerName: "Plan", field: "plan_name", minWidth: 140 },
        { headerName: "Price", field: "amount", maxWidth: 140, type: "numericColumn", valueGetter: (p) => (p.data ? Number(p.data.amount) : null), valueFormatter: (p) => (p.data && p.value != null ? `${money(p.value, p.data.currency)} / ${String(p.data.interval_unit || "").toLowerCase()}` : "") },
        { headerName: "Unlocks", field: "path_prefix", maxWidth: 130, cellClass: "mono" },
        { headerName: "Agent", field: "agent_id", minWidth: 140, cellClass: "mono" },
        { headerName: "Agent's reason", field: "purpose", minWidth: 200, tooltipField: "purpose" },
        { headerName: "PayPal subscription", field: "provider_subscription_id", minWidth: 170, cellClass: "mono", tooltipField: "provider_subscription_id" },
        { headerName: "Started", field: "created_at", maxWidth: 120, sort: "desc", filter: false, valueFormatter: (p) => formatTime(p.value) },
      ],
    });
  }

  function showOrderPanel(title, rows, tone) {
    const box = $("order-verify");
    clear(box);
    box.append(el("h3", { text: title }));
    if (rows) {
      box.append(el("table", null, [
        el("thead", null, [el("tr", null, ["Check", "SARA", "PayPal", ""].map((h) => el("th", { text: h })))]),
        el("tbody", null, rows.map((c) => el("tr", null, [
          el("td", { text: c.label }), el("td", { className: "mono", text: c.sara }), el("td", { className: "mono", text: c.paypal }),
          el("td", { className: c.status === "match" ? "ok" : c.status === "mismatch" ? "bad" : "", text: c.status }),
        ]))),
      ]));
    }
    box.dataset.tone = tone || "";
    box.hidden = false;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function verifyOrder(order) {
    showOrderPanel(`Checking ${order.order_id} with PayPal...`);
    const result = await getJson(`/paypal/orders/${encodeURIComponent(order.order_id)}/verify`, { method: "POST", headers: authHeaders() });
    if (!result.ok) {
      showOrderPanel((result.body && result.body.message) || `Verification failed (HTTP ${result.status}).`);
      return;
    }
    showOrderPanel(`${result.body.verdict} (via ${result.body.mcp_server})`, result.body.checks, result.body.consistent ? "good" : "critical");
  }

  async function refundOrder(order) {
    if (!state.token && !state.owner) { showOrderPanel("Sign in as the owner to refund orders."); return; }
    if (!window.confirm(`Refund ${money(order.amount, order.currency)} for ${order.order_id} through PayPal? Access to what was bought ends.`)) return;
    const result = await getJson(`/commerce/orders/${encodeURIComponent(order.order_id)}/refund`, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ reason: "Refunded by the publisher" }),
    });
    showOrderPanel(result.ok ? `Refunded ${order.order_id} (PayPal refund ${result.body.provider_refund_id || "pending"}).` : (result.body && result.body.message) || `Refund failed (HTTP ${result.status}).`);
    loadCommerce();
  }

  async function shipOrder(order) {
    if (!state.token && !state.owner) { showOrderPanel("Sign in as the owner to add shipping updates."); return; }
    const tracking = window.prompt("Tracking number", "");
    if (!tracking) return;
    const carrier = (window.prompt("Carrier code (for example UPS, FEDEX, DHL, USPS)", "UPS") || "").trim().toUpperCase();
    if (!carrier) return;
    const result = await getJson(`/commerce/orders/${encodeURIComponent(order.order_id)}/tracking`, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ tracking_number: tracking.trim(), carrier }),
    });
    showOrderPanel(result.ok ? `Tracking ${tracking} (${carrier}) added at PayPal; ${order.order_id} is shipped.` : (result.body && result.body.message) || `Tracking failed (HTTP ${result.status}).`);
    loadCommerce();
  }

  async function loadCommerce() {
    try {
      const result = await getJson("/commerce/summary", { headers: authHeaders() });
      if (!result.ok || !result.body) return;
      renderCommerce(result.body);
    } catch (_) { /* the main refresh loop reports connectivity */ }
  }

  function renderCommerce(c) {
    const cur = c.currency;
    const d = c.deposits || {};
    if ($("count-orders")) $("count-orders").textContent = String(c.paid_access_orders + c.paid_product_orders + c.active_subscriptions);
    const kpis = $("commerce-kpis");
    clear(kpis);
    kpis.append(
      tile("Pay-per-access", money(c.access_revenue, cur), `${compact(c.paid_access_orders)} item${c.paid_access_orders === 1 ? "" : "s"} sold`, "accent", "▶"),
      tile("Product orders", money(c.product_revenue, cur), `${compact(c.paid_product_orders)} paid order${c.paid_product_orders === 1 ? "" : "s"}`, "accent", "◫"),
      tile("Subscriptions (MRR)", money(c.subscription_mrr, cur), `${compact(c.active_subscriptions)} active · ${compact(c.pending_subscriptions)} pending · ${compact(c.cancelled_subscriptions)} cancelled`, "good", "∞"),
      tile("Forfeited deposits", money(d.forfeited_revenue, cur), `${compact(d.forfeited)} forfeited · ${compact(d.returned)} returned of ${compact(d.total)}`, d.forfeited ? "critical" : "neutral", "✕"),
      tile("Awaiting payment", compact(c.orders_awaiting_payment), `Orders not captured · ${compact(c.refunded_orders || 0)} refunded · ${compact(c.open_disputes || 0)} disputes`, c.orders_awaiting_payment ? "warning" : "neutral", "⏸"),
      tile("PayPal webhooks", compact(c.webhook_events || 0), "Verified events processed", "accent", "⇄"),
    );
    const groups = [
      ["Pay-per-access", "var(--accent)", Number(c.access_revenue)],
      ["Products", "var(--status-serious)", Number(c.product_revenue)],
      ["Subscriptions (MRR)", "var(--status-good)", Number(c.subscription_mrr)],
      ["Forfeited deposits", "var(--status-critical)", Number(d.forfeited_revenue || 0)],
    ].filter((g) => g[2] > 0);
    const box = $("revenue-mix");
    clear(box);
    const total = groups.reduce((a, g) => a + g[2], 0);
    if (!total) {
      box.append(el("p", { className: "empty", text: "No agent revenue yet. Deposits kept from rule-breakers and agent purchases appear here." }));
    } else {
      const bar = el("div", { className: "share-bar", role: "img", "aria-label": groups.map((g) => `${g[0]}: ${money(g[2], cur)}`).join(", ") });
      for (const g of groups) bar.append(el("span", { className: "share-seg", style: `width:${(g[2] / total) * 100}%; background:${g[1]}`, title: `${g[0]}: ${money(g[2], cur)}` }));
      box.append(bar, el("ul", { className: "share-legend" }, groups.map((g) => el("li", null, [
        el("span", { className: "share-swatch", style: `background:${g[1]}` }),
        el("span", { text: `${g[0]} · ${Math.round((g[2] / total) * 100)}%` }),
        el("span", { className: "num", text: money(g[2], cur) }),
      ]))));
    }
    if (state.grids.orders) {
      state.grids.orders.setRows(c.orders);
      state.grids.subscriptions.setRows(c.subscriptions);
    }
  }

  // --- owner session -------------------------------------------------------------------
  async function loadOwner() {
    try {
      const response = await fetch("/auth/me", { credentials: "same-origin" });
      if (!response.ok) return;
      const me = await response.json();
      state.loginEnabled = Boolean(me.login_enabled);
      // Without sign-in configured the console is the operator's own (local) console.
      state.owner = Boolean(me.login_enabled && me.signed_in);
      if (me.login_enabled && !me.signed_in) {
        window.location.replace("/signin");
        return;
      }
      if ($("owner-email")) {
        $("owner-email").textContent = me.email || "";
        $("owner-email").hidden = !me.email;
      }
      if ($("signout")) $("signout").hidden = !me.login_enabled;
      if ($("mode-badge")) {
        const sandbox = me.payment_mode === "paypal_sandbox";
        $("mode-badge").textContent = sandbox ? "PayPal" : me.payment_mode === "mock" ? "Simulated payments" : "PayPal";
        $("mode-badge").hidden = !me.payment_mode;
      }
    } catch (_) { /* the console still loads; writes fall back to read-only */ }
  }

  async function signOut() {
    try {
      await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
    } finally {
      window.location.assign("/");
    }
  }

  // --- data loop -----------------------------------------------------------------------
  async function refresh() {
    // A refresh asked for while one is running (a website switch) runs right after it.
    if (state.inFlight) { state.refreshQueued = true; return; }
    state.inFlight = true;
    const filterAtStart = siteFilter();
    const main = document.querySelector(".console-main");
    const slowTimer = setTimeout(() => main.classList.add("refreshing"), SLOW_FETCH_MS);
    try {
      const result = await getJson(`/dashboard/data${siteQuery()}`, { headers: authHeaders() });
      if (result.status === 401) {
        if (state.loginEnabled) {
          window.location.replace("/signin");
          return;
        }
        showBanner("Your session has ended. Sign in again to see the console.");
        return;
      }
      if (!result.ok || !result.body) {
        showBanner(`The gateway returned HTTP ${result.status} for dashboard data.`);
        return;
      }
      hideBanner();
      // Ignore a response for a website the user has since switched away from.
      if (filterAtStart !== siteFilter()) { state.refreshQueued = true; return; }
      render(result.body);
      if (state.view === "operations") loadRequests(false);
      loadCommerce();
    } catch (_) {
      showBanner("Cannot reach the SARA gateway. Is the server running?");
    } finally {
      clearTimeout(slowTimer);
      main.classList.remove("refreshing");
      state.inFlight = false;
      if (state.refreshQueued) {
        state.refreshQueued = false;
        refresh();
      }
    }
  }

  function render(data) {
    state.data = data;
    renderKpis(data.kpis);
    renderSecurityKpis(data);
    renderViolations(data.kpis.violations_by_reason);
    renderOutcomes(data.deposits);
    renderRecent(data.deposits);
    if (state.grids.deposits) {
      state.grids.deposits.setRows(data.deposits);
      state.grids.sessions.setRows(data.sessions);
      state.grids.events.setRows(data.events);
      state.grids.violations.setRows(data.events.filter((e) => e.event_type === "POLICY_VIOLATION"));
    }
    updateAssistantState();
    $("console-empty").hidden = Boolean(data.deposits.length || data.sessions.length || data.events.length);
    announceChanges(state.previous, data);
    state.previous = data;

    // The inspector follows the newest deposit until the user selects one.
    if (state.autoSelect && data.deposits.length && data.deposits[0].deposit_id !== state.selectedDepositId) {
      selectDeposit(data.deposits[0]);
    }

    // Re-explain automatically when the selected deposit's outcome changes (live demo).
    if (state.selectedDepositId) {
      const row = data.deposits.find((d) => d.deposit_id === state.selectedDepositId);
      if (row && row.updated_at !== state.selectedUpdatedAt) {
        state.selectedUpdatedAt = row.updated_at;
        renderDepositFacts(row);
        loadExplanation(row.deposit_id);
        if (state.paypal) state.paypal.setDeposit(row);
      }
    }
  }

  // --- live toasts: settlements and new violations since the previous refresh -----------
  function toast(tone, icon, title, body, onOpen) {
    const box = $("toasts");
    const node = el("div", { className: "toast", dataset: { tone }, role: "status" }, [
      el("span", { className: "toast-icon", "aria-hidden": "true", text: icon }),
      el("div", { className: "toast-body" }, [el("strong", { text: title }), body ? el("span", { text: body }) : null]),
      onOpen ? el("button", { type: "button", className: "btn-sm", text: "View", onclick: () => { onOpen(); dismiss(); } }) : null,
      el("button", { type: "button", className: "toast-close", "aria-label": "Dismiss", text: "×", onclick: () => dismiss() }),
    ]);
    const dismiss = () => {
      clearInterval(ticker);
      node.classList.add("leaving");
      setTimeout(() => node.remove(), 250);
    };
    // Counts down only while the tab is visible and the pointer is not over the toast, so a
    // notification raised while the user was on the live demo tab is still there on return.
    let left = 7000;
    let hovered = false;
    node.addEventListener("pointerenter", () => { hovered = true; });
    node.addEventListener("pointerleave", () => { hovered = false; });
    const ticker = setInterval(() => {
      if (document.hidden || hovered) return;
      left -= 250;
      if (left <= 0) dismiss();
    }, 250);
    box.append(node);
    while (box.children.length > 4) box.firstChild.remove();
  }

  // Playground runs attribute their crawler as "playground-<run id>". Their violations and
  // settlements are already shown in the action response panel, so console toasts skip them.
  const PLAYGROUND_CRAWLER_PREFIX = "playground-";
  function isPlaygroundCrawler(crawlerId) {
    return String(crawlerId || "").startsWith(PLAYGROUND_CRAWLER_PREFIX);
  }

  function announceChanges(before, after) {
    if (!before) return;
    // The playground already shows the final response (including the deposit outcome), so
    // do not duplicate it with automatic "new deposit" / "refunded" / "violation" toasts.
    if (state.pgRunning || Date.now() < state.pgQuietUntil) return;
    const old = new Map(before.deposits.map((d) => [d.deposit_id, d.deposit_state]));
    for (const d of after.deposits) {
      // Playground deposits show their settlement in the action response panel.
      if (isPlaygroundCrawler(d.crawler_id)) continue;
      const was = old.get(d.deposit_id);
      if (was === d.deposit_state) continue;
      const open = () => openDeposit(d);
      if (RETURNED.has(d.deposit_state)) toast("good", "↺", `Deposit ${d.deposit_state === "RELEASED" ? "released" : "refunded"} · ${money(d.amount, d.currency)}`, d.crawler_id, open);
      else if (d.deposit_state === "FORFEITED") toast("critical", "✕", `Deposit forfeited · ${money(d.amount, d.currency)}`, d.crawler_id, open);
      else if (!was) toast("accent", "$", `New deposit · ${money(d.amount, d.currency)}`, `${d.crawler_id} · ${d.pricing_tier_name || "Base"} tier`, open);
    }
    // Count only new violation events from crawlers outside the playground. The aggregate KPI
    // also includes playground violations, which would otherwise raise a duplicate toast.
    const seen = new Set((before.events || []).map((e) => e.event_id));
    const added = (after.events || []).filter((e) =>
      e.event_type === "POLICY_VIOLATION" && !seen.has(e.event_id) && !isPlaygroundCrawler(e.crawler_id)
    ).length;
    if (added > 0) {
      toast("critical", "⚠", `${added} new policy violation${added === 1 ? "" : "s"}`, "Blocked by the gateway and recorded in the audit log.",
        () => drill({ view: "security", target: "grid-violations" }));
    }
  }

  // --- init ----------------------------------------------------------------------------
  function init() {
    initTheme();

    state.pgSelected = defaultBehaviorSelection();
    if ($("site-add-form")) $("site-add-form").addEventListener("submit", addSite);
    if ($("site-add-open")) $("site-add-open").addEventListener("click", () => {
      if ($("site-add-form") && !$("site-add-form").hidden) closeSiteAddForm();
      else openSiteAddForm();
    });
    if ($("site-add-cancel")) $("site-add-cancel").addEventListener("click", closeSiteAddForm);
    if ($("pg-site-select")) $("pg-site-select").addEventListener("change", (event) => selectSite(event.target.value));
    if ($("lab-run")) $("lab-run").addEventListener("click", startLab);
    if ($("lab-stop")) $("lab-stop").addEventListener("click", stopLab);
    for (const button of document.querySelectorAll("[data-paypal-login]")) button.addEventListener("click", connectPayPal);
    if ($("payout-switch")) $("payout-switch").addEventListener("click", connectPayPal);
    if ($("payout-disconnect")) $("payout-disconnect").addEventListener("click", disconnectPayPal);
    window.addEventListener("message", (event) => {
      if (event.data && event.data.type === "sara-paypal-connect") {
        loadPayPalAccount();
        if (event.data.ok) toast("good", "✓", "PayPal connected", "Payouts go to your PayPal account.");
      }
    });
    if ($("lab-approve")) $("lab-approve").addEventListener("click", () => {
      const url = state.lab && state.lab.body && state.lab.body.approval_url;
      if (url) window.open(url, "sara-paypal", "popup,width=520,height=760");
    });

    if ($("lab-goal")) $("lab-goal").addEventListener("input", () => { state.labError = null; renderLab(); });
    if ($("site-add-input")) $("site-add-input").addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeSiteAddForm();
    });
    if ($("site-add-input")) $("site-add-input").addEventListener("input", syncSiteAddPrefix);
    syncSiteAddPrefix();
    $("allowed-add-form").addEventListener("submit", (event) => addPath(event, "allowed"));
    $("disallowed-add-form").addEventListener("submit", (event) => addPath(event, "disallowed"));

    // Deposit and rate limit use a pencil-to-edit pattern with a small Save button.
    if ($("cfg-deposit-edit")) {
      $("cfg-deposit-edit").addEventListener("click", () => {
        state.editingDeposit = true;
        state.editingRate = false;
        refreshSetupControls();
        if ($("cfg-deposit")) {
          $("cfg-deposit").focus();
          $("cfg-deposit").select();
        }
      });
    }
    if ($("cfg-deposit-save")) {
      $("cfg-deposit-save").addEventListener("click", async () => {
        const saved = await applySelectedSite();
        if (saved) {
          state.editingDeposit = false;
          renderSiteConfig();
        }
      });
    }
    if ($("cfg-rate-edit")) {
      $("cfg-rate-edit").addEventListener("click", () => {
        state.editingRate = true;
        state.editingDeposit = false;
        refreshSetupControls();
        if ($("cfg-rate")) {
          $("cfg-rate").focus();
          $("cfg-rate").select();
        }
      });
    }
    if ($("cfg-rate-save")) {
      $("cfg-rate-save").addEventListener("click", async () => {
        const saved = await applySelectedSite();
        if (saved) {
          state.editingRate = false;
          renderSiteConfig();
        }
      });
    }
    const cancelEdit = (event) => {
      if (event.key !== "Escape") return;
      state.editingDeposit = false;
      state.editingRate = false;
      refreshSetupControls();
    };
    for (const id of ["cfg-deposit", "cfg-rate", "cfg-rate-unit"]) {
      if ($(id)) $(id).addEventListener("keydown", cancelEdit);
    }

    $("cfg-deposit").addEventListener("input", (event) => {
      const site = selectedSite();
      if (!site) return;
      site.deposit = event.target.value;
      state.pgResults = {};
      state.pgSteps = [];
      markSiteDirty();
    });
    $("cfg-rate").addEventListener("input", (event) => {
      const site = selectedSite();
      if (!site) return;
      site.rate = Number(event.target.value) || 0;
      state.pgResults = {};
      state.pgSteps = [];
      markSiteDirty();
    });
    $("cfg-rate-unit").addEventListener("change", (event) => {
      const site = selectedSite();
      if (!site) return;
      site.unit = normalizeRateUnit(event.target.value);
      state.pgResults = {};
      state.pgSteps = [];
      markSiteDirty();
    });
    $("pg-reset").addEventListener("click", resetPlayground);
    if ($("pg-clear")) $("pg-clear").addEventListener("click", clearPlayground);
    $("pg-run-button").addEventListener("click", runPlaygroundPlan);
    document.addEventListener("click", () => closeSiteMenus());

    for (const node of document.querySelectorAll("[data-go-view]")) {
      node.addEventListener("click", () => showView(node.dataset.goView));
    }
    $("chart-view-toggle").addEventListener("click", (event) => {
      state.chartAsTable = !state.chartAsTable;
      event.target.textContent = state.chartAsTable ? "Show as chart" : "Show as table";
      event.target.setAttribute("aria-pressed", String(state.chartAsTable));
      renderViolations(state.data ? state.data.kpis.violations_by_reason : {});
    });

    $("draft-button").addEventListener("click", draftPolicy);

    if (window.CTPayPal) {
      state.paypal = window.CTPayPal.create($("paypal-panel"), {
        headers: () => (state.token ? { "X-Admin-Token": state.token } : {}),
        compact: true,
      });
    }

    initGrids();
    updateAssistantState();
    if ($("signout")) $("signout").addEventListener("click", signOut);
    // Learn who is signed in before the views decide what is editable.
    state.siteFilter = store.get("localStorage", SITE_FILTER_KEY) || "all";
    if ($("site-filter")) $("site-filter").addEventListener("change", (event) => changeSiteFilter(event.target.value));
    loadOwner().finally(() => {
      showPayPalReturn();
      loadSites({ silent: true });
      loadPayPalAccount();
      initViews();
      refresh();
    });
    state.timer = setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
    // Fetch AG Studio in the background so the Analytics view opens without a wait.
    if (window.CTAnalytics) {
      const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 2500));
      idle(() => window.CTAnalytics.prefetch(), { timeout: 5000 });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
