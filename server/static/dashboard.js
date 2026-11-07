/* AgentTrust publisher console.
   All values that originate from crawlers (IDs, paths, reasons) are inserted with
   textContent, never innerHTML. Every number shown comes from the gateway's own endpoints:
   /dashboard/data, /demo/requests, /demo/config, and /policies. */
(() => {
  "use strict";

  const REFRESH_MS = 3000;
  const SLOW_FETCH_MS = 400;
  const REQUESTS_EVERY_MS = 6000;
  const VIEWS = {
    overview: "Overview",
    commerce: "Commerce",
    operations: "Operations",
    security: "Security & audit",
    analytics: "Analytics",
    policy: "Policy",
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
    view: "overview",
    policyLoaded: false,
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
    for (const link of document.querySelectorAll(".sidenav-link")) {
      if (link.dataset.view === view) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    $("page-title").textContent = VIEWS[view];
    if (!fromHash) history.replaceState(null, "", view === "overview" ? window.location.pathname : `#${view}`);
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
          ["Resource", r.resource ? `GET /gateway${r.resource === "/" ? "" : r.resource}` : "", true],
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
      exportName: "agenttrust-deposits",
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
      exportName: "agenttrust-sessions",
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
      exportName: "agenttrust-requests",
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
      exportName: "agenttrust-violations",
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
      exportName: "agenttrust-audit",
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

    $("quick-filter").addEventListener("input", (event) => {
      for (const grid of Object.values(state.grids)) if (grid) grid.setQuickFilter(event.target.value);
    });
    return true;
  }

  // --- render: header, KPIs, charts ----------------------------------------------------
  function renderBadges(data) {
    const badges = $("badges");
    clear(badges);
    const add = (tone, text) =>
      badges.append(el("span", { className: "badge" }, [el("span", { className: "badge-dot", dataset: { tone } }), el("span", { text })]));
    add(data.payment_mode === "mock" ? "neutral" : "accent", data.payment_mode === "mock" ? "Mock payments (simulated)" : "PayPal Sandbox");
    add("accent", data.settlement_mode === "authorization" ? "Settlement: authorization hold" : "Settlement: capture + refund");
    add(data.ai_available ? "good" : "neutral", data.ai_available ? `AI: ${providerName(data.ai_provider)} (${data.ai_model})` : "AI: rule-based explanations only");
    add("neutral", data.access === "admin" ? "Admin view" : "Read-only public view");
    $("site-line").textContent = `${data.site_name} · policy ${data.policy_version}`;
  }

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
      : "Enter the admin token in the top bar to draft a policy with AI. The active policy on the left is always visible.";
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
      exportName: "agenttrust-orders",
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
      ],
    });
    state.grids.subscriptions = create($("grid-subscriptions"), {
      rowId: (s) => s.subscription_ref,
      pageSize: 10,
      autoHeight: true,
      exportName: "agenttrust-subscriptions",
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
    $("count-orders").textContent = String(c.paid_access_orders + c.paid_product_orders + c.active_subscriptions);
    const kpis = $("commerce-kpis");
    clear(kpis);
    kpis.append(
      tile("Pay-per-access", money(c.access_revenue, cur), `${compact(c.paid_access_orders)} item${c.paid_access_orders === 1 ? "" : "s"} sold`, "accent", "▶"),
      tile("Product orders", money(c.product_revenue, cur), `${compact(c.paid_product_orders)} paid order${c.paid_product_orders === 1 ? "" : "s"}`, "accent", "◫"),
      tile("Subscriptions (MRR)", money(c.subscription_mrr, cur), `${compact(c.active_subscriptions)} active · ${compact(c.pending_subscriptions)} pending · ${compact(c.cancelled_subscriptions)} cancelled`, "good", "∞"),
      tile("Forfeited deposits", money(d.forfeited_revenue, cur), `${compact(d.forfeited)} forfeited · ${compact(d.returned)} returned of ${compact(d.total)}`, d.forfeited ? "critical" : "neutral", "✕"),
      tile("Awaiting payment", compact(c.orders_awaiting_payment), "Orders created but not captured", c.orders_awaiting_payment ? "warning" : "neutral", "⏸"),
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
        showBanner(state.token
          ? "The admin token was rejected. Check ADMIN_API_TOKEN on the gateway."
          : "Enter the admin token to view the console, or run the gateway with DASHBOARD_PUBLIC=true for a read-only demo.");
        $("updated").textContent = "Not authorized";
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
      showBanner("Cannot reach the AgentTrust gateway. Is the server running?");
    } finally {
      clearTimeout(slowTimer);
      main.classList.remove("refreshing");
      state.inFlight = false;
    }
  }

  function render(data) {
    state.data = data;
    renderBadges(data);
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
    $("updated").textContent = `Updated ${formatTime(data.generated_at)}`;
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

  function announceChanges(before, after) {
    if (!before) return;
    const old = new Map(before.deposits.map((d) => [d.deposit_id, d.deposit_state]));
    for (const d of after.deposits) {
      const was = old.get(d.deposit_id);
      if (was === d.deposit_state) continue;
      const open = () => { showView("overview"); selectDeposit(d, true); $("explain-card").scrollIntoView({ behavior: "smooth", block: "start" }); };
      if (RETURNED.has(d.deposit_state)) toast("good", "↺", `Deposit ${d.deposit_state === "RELEASED" ? "released" : "refunded"} · ${money(d.amount, d.currency)}`, d.crawler_id, open);
      else if (d.deposit_state === "FORFEITED") toast("critical", "✕", `Deposit forfeited · ${money(d.amount, d.currency)}`, d.crawler_id, open);
      else if (!was) toast("accent", "$", `New deposit · ${money(d.amount, d.currency)}`, `${d.crawler_id} · ${d.pricing_tier_name || "Base"} tier`, open);
    }
    const added = after.kpis.policy_violations - before.kpis.policy_violations;
    if (added > 0) {
      toast("critical", "⚠", `${added} new policy violation${added === 1 ? "" : "s"}`, "Blocked by the gateway and recorded in the audit log.",
        () => drill({ view: "security", target: "grid-violations" }));
    }
  }

  // --- admin token popover -----------------------------------------------------------------
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

  function setAutoRefresh(enabled) {
    clearInterval(state.timer);
    state.timer = enabled ? setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS) : null;
    store.set("localStorage", "ct-auto-refresh", enabled ? null : "off");
  }

  // --- init ----------------------------------------------------------------------------
  function init() {
    initTheme();
    state.token = store.get("sessionStorage", "ct-admin-token");
    if (state.token) $("admin-token").value = state.token;

    $("token-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const value = $("admin-token").value.trim();
      state.token = value || null;
      store.set("sessionStorage", "ct-admin-token", state.token);
      refresh();
      if (state.paypal) state.paypal.refresh();
    });

    $("chart-view-toggle").addEventListener("click", (event) => {
      state.chartAsTable = !state.chartAsTable;
      event.target.textContent = state.chartAsTable ? "Show as chart" : "Show as table";
      event.target.setAttribute("aria-pressed", String(state.chartAsTable));
      renderViolations(state.data ? state.data.kpis.violations_by_reason : {});
    });

    $("draft-button").addEventListener("click", draftPolicy);

    const auto = $("auto-refresh");
    auto.checked = store.get("localStorage", "ct-auto-refresh") !== "off";
    auto.addEventListener("change", () => setAutoRefresh(auto.checked));

    if (window.CTPayPal) {
      state.paypal = window.CTPayPal.create($("paypal-panel"), {
        headers: () => (state.token ? { "X-Admin-Token": state.token } : {}),
        compact: true,
      });
    }

    initGrids();
    initAdminMenu();
    updateAssistantState();
    initViews();
    refresh();
    setAutoRefresh(auto.checked);
    // Fetch AG Studio in the background so the Analytics view opens without a wait.
    if (window.CTAnalytics) {
      const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 2500));
      idle(() => window.CTAnalytics.prefetch(), { timeout: 5000 });
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
