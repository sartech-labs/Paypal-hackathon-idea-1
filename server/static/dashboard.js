/* SARA publisher console.
   All values that originate from crawlers (IDs, paths, reasons) are inserted with
   textContent, never innerHTML. Every number shown comes from the gateway's own endpoints:
   /dashboard/data, /demo/requests, /demo/config, and /policies. */
(() => {
  "use strict";

  const REFRESH_MS = 3000;
  const SLOW_FETCH_MS = 400;
  const REQUESTS_EVERY_MS = 6000;
  const VIEWS = {
    setup: "Console",
    overview: "Overview",
    commerce: "Commerce",
    operations: "Audit log",
    security: "Policy violations",
    analytics: "Analytics",
    policy: "Policy",
  };

  const state = {
    token: null,
    consoleAuth: null,
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

  // --- sign out (shown only when this deployment enables auth) -------------------------
  function initSignOut() {
    fetch("/_sara/session")
      .then((response) => (response.ok ? response.json() : null))
      .then((session) => {
        if (!session) return;
        state.consoleAuth = session;
        const form = $("signout-form");
        if (form && session.authenticated === true) form.hidden = false;
      })
      .catch(() => {});
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
    if (!VIEWS[view]) view = "setup";
    state.view = view;
    for (const section of document.querySelectorAll(".view")) section.hidden = section.dataset.view !== view;
    for (const link of document.querySelectorAll(".sidenav-link")) {
      if (link.dataset.view === view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    $("page-title").textContent = VIEWS[view];
    if ($("page-head")) $("page-head").hidden = view === "setup";
    if (!fromHash) history.replaceState(null, "", view === "setup" ? window.location.pathname : `#${view}`);
    if (view === "setup") loadSetup();
    if (view === "operations" || view === "analytics") loadRequests(true);
    if (view === "policy") loadPolicy();
    if (view === "commerce") loadCommerce();
    if (view === "analytics" && window.CTAnalytics) window.CTAnalytics.open(analyticsContext());
  }

  function initViews() {
    for (const link of document.querySelectorAll(".sidenav-link")) {
      link.addEventListener("click", () => showView(link.dataset.view));
    }
    window.addEventListener("hashchange", () => showView(window.location.hash.slice(1), true));
    showView(window.location.hash.slice(1) || "setup", true);
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
          ["Rail", d.payment_mode === "mock" ? "Mock (simulated)" : "PayPal Sandbox"],
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
      noRows: "No crawler sessions yet.",
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
    const compliance = settled ? `${Math.round((k.deposits_returned / settled) * 100)}% of settled deposits returned` : "No settled deposits yet";
    kpis.append(
      tile("Active sessions", compact(k.sessions_active), `${compact(k.sessions_total)} total crawler sessions`, "accent", "◉",
        { view: "operations", grid: "sessions", segment: "active", target: "grid-sessions", hint: "Show active sessions" }),
      tile("Deposits held", compact(k.deposits_held), k.deposits_needing_attention ? `${k.deposits_needing_attention} need attention` : "During live sessions", k.deposits_needing_attention ? "warning" : "neutral", "⏸",
        { view: "overview", grid: "deposits", segment: "held", target: "grid-deposits", hint: "Show held deposits" }),
      tile("Deposits returned", compact(k.deposits_returned), `${money(k.amount_returned, k.currency)} refunded or released`, "good", "↺",
        { view: "overview", grid: "deposits", segment: "returned", target: "grid-deposits", hint: "Show returned deposits" }),
      tile("Deposits forfeited", compact(k.deposits_forfeited), `${money(k.amount_forfeited, k.currency)} kept by the publisher`, "critical", "✕",
        { view: "overview", grid: "deposits", segment: "forfeited", target: "grid-deposits", hint: "Show forfeited deposits" }),
      tile("Policy violations", compact(k.policy_violations), compliance, k.policy_violations ? "critical" : "good", "⚠",
        { view: "security", target: "grid-violations", hint: "Open the violations table" }),
      tile("Requests allowed", compact(k.gateway_requests_allowed), `${compact(k.gateway_requests_rejected)} blocked by the gateway`, "accent", "→",
        { view: "operations", grid: "requests", segment: "granted", target: "grid-requests", hint: "Show granted requests" })
    );
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
    $("count-sessions").textContent = compact(data.sessions.length);
    $("count-sessions").title = `${data.sessions.length} crawler sessions, ${k.sessions_active} active`;
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
        ["Rail", d.payment_mode === "mock" ? "Mock (simulated)" : "PayPal Sandbox"],
        ["PayPal order", d.provider_order_id, true],
        ["Refund", d.provider_refund_id, true],
      ]),
      el("hr", { className: "divider" })
    );
  }

  async function loadExplanation(depositId) {
    const panel = $("explanation");
    $("explain-sub").textContent = `Deposit ${depositId}`;
    panel.classList.add("refreshing");
    try {
      const result = await getJson(`/dashboard/deposits/${encodeURIComponent(depositId)}/explanation`, { headers: authHeaders() });
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
    state.sitesError = null;
    state.activeSiteId = body.active_site_id || state.activeSiteId || null;
    if (typeof body.config_public === "boolean") {
      state.configReadOnly = !(state.token || body.config_public);
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
          el("span", { className: "site-url", text: site.host }),
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

  async function addSite(event) {
    event.preventDefault();
    const input = $("site-add-input");
    if (state.configReadOnly) {
      showSetupMessage("Read-only. Enter the admin token in the top bar to add a website.");
      return;
    }
    if (state.addingSite || state.applying || state.activating) return;
    const raw = input ? input.value.trim() : "";
    if (!raw) {
      showSetupMessage("Enter the website address, for example example.com.");
      if (input) input.focus();
      return;
    }
    const origin = normalizeOrigin(raw);
    if (!origin) {
      showSetupMessage("Enter a valid website address, for example https://example.com.");
      if (input) input.focus();
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
        showSetupMessage(apiErrorMessage(result.body, result.status, `Could not add ${origin} (HTTP ${result.status}).`));
        return;
      }
      if (input) input.value = "";
      syncSiteAddPrefix();
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
      showSetupMessage("Could not reach the gateway to add the website.");
    } finally {
      state.addingSite = false;
      refreshSetupControls();
    }
  }

  async function toggleSite(siteId) {
    const site = state.sites.find((item) => item.id === siteId);
    if (!site || state.applying || state.addingSite || state.activating) return;
    if (state.configReadOnly) {
      showSetupMessage("Read-only. Enter the admin token in the top bar to pause or resume a website.");
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
    if (state.sites.length <= 1) {
      showSetupMessage("Keep at least one website in the console.");
      return;
    }
    if (state.configReadOnly) {
      showSetupMessage("Read-only. Enter the admin token in the top bar to remove a website.");
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
    else if (state.configReadOnly) hint = "Read-only. Enter the admin token in the top bar to apply changes.";
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

  function renderSiteConfig() {
    const site = selectedSite();
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
      state.configReadOnly = !(state.token || Boolean(cfg && cfg.config_public));
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
      if (!opts.silent) showSetupMessage("Read-only. Enter the admin token in the top bar to apply changes.");
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
      state.configReadOnly = !(state.token || Boolean(result.body && result.body.config_public));
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
    else if (!state.pgCanStart) disabledReason = "Starting runs needs the admin token, or DEMO_PUBLIC=true on a demo deployment.";
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
      const result = await getJson("/demo/requests", { headers: authHeaders() });
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
      noRows: "No orders yet. Run the personal agent from the agent studio.",
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
    if (!state.token) { showOrderPanel("Refunds need the admin token (Admin menu, top right)."); return; }
    if (!window.confirm(`Refund ${money(order.amount, order.currency)} for ${order.order_id} through PayPal? Access to what was bought ends.`)) return;
    const result = await getJson(`/commerce/orders/${encodeURIComponent(order.order_id)}/refund`, {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ reason: "Refunded by the publisher" }),
    });
    showOrderPanel(result.ok ? `Refunded ${order.order_id} (PayPal refund ${result.body.provider_refund_id || "pending"}).` : (result.body && result.body.message) || `Refund failed (HTTP ${result.status}).`);
    loadCommerce();
  }

  async function shipOrder(order) {
    if (!state.token) { showOrderPanel("Shipping updates need the admin token (Admin menu, top right)."); return; }
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
      box.append(el("p", { className: "empty", text: "No agent revenue yet. Run the personal agent from the agent studio." }));
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

  // --- data loop -----------------------------------------------------------------------
  async function refresh() {
    if (state.inFlight) return;
    state.inFlight = true;
    const main = document.querySelector(".console-main");
    const slowTimer = setTimeout(() => main.classList.add("refreshing"), SLOW_FETCH_MS);
    try {
      const result = await getJson("/dashboard/data", { headers: authHeaders() });
      if (result.status === 401) {
        // With sign-in enabled, an expired session belongs at the login page rather than
        // behind a permanent "no access" banner.
        if (state.consoleAuth && state.consoleAuth.enabled) {
          location.assign(`/_sara/login?next=${encodeURIComponent(location.pathname)}`);
          return;
        }
        showBanner("The dashboard needs public access or an admin token on the gateway.");
        return;
      }
      if (!result.ok || !result.body) {
        showBanner(`The gateway returned HTTP ${result.status} for dashboard data.`);
        return;
      }
      hideBanner();
      render(result.body);
      if (state.view === "operations") loadRequests(false);
      loadCommerce();
    } catch (_) {
      showBanner("Cannot reach the SARA gateway. Is the server running?");
    } finally {
      clearTimeout(slowTimer);
      main.classList.remove("refreshing");
      state.inFlight = false;
    }
  }

  function render(data) {
    state.data = data;
    renderKpis(data.kpis);
    renderSecurityKpis(data);
    renderViolations(data.kpis.violations_by_reason);
    renderOutcomes(data.deposits);
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
      const open = () => { showView("overview"); selectDeposit(d, true); $("explain-card").scrollIntoView({ behavior: "smooth", block: "start" }); };
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
    initSignOut();

    state.pgSelected = defaultBehaviorSelection();
    if ($("site-add-form")) $("site-add-form").addEventListener("submit", addSite);
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
    initViews();
    refresh();
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
