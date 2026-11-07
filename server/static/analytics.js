/* SARA analytics: AG Studio dashboards over the gateway's own records.

   AG Studio (ag-studio 3.1.0, AG Grid's embedded-analytics component) is loaded on demand,
   from a pinned CDN URL with a Subresource Integrity hash, the first time the Analytics view
   opens. Its data engine runs in the browser; the rows it receives come only from
   /dashboard/data, /demo/requests, and /demo/config (plus each recent run's usage). The only
   transformations are labels (e.g. "REFUNDED" -> "Returned") and per-minute time buckets. */
(() => {
  "use strict";

  const STUDIO_URL = "https://cdn.jsdelivr.net/npm/ag-studio@3.1.0/dist/umd/ag-studio.min.js";
  const STUDIO_SRI = "sha384-AQulWK/PF30iLAso56dIH/1+Iv852aUbFC98K7N1ZpLYVvmamIEAU36dEbUaPym1";

  const PAGES = [
    ["overview", "Overview"],
    ["commerce", "Agent commerce"],
    ["finance", "Deposits & refunds"],
    ["security", "Security"],
    ["agents", "AI agents"],
  ];

  const state = { ctx: null, api: null, loading: null, mode: "view", page: "overview", built: false, busy: false };
  const $ = (id) => document.getElementById(id);

  function el(tag, props, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "text") node.textContent = String(value);
      else if (key === "className") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children || []) if (child) node.append(child);
    return node;
  }

  function status(text, spinning) {
    const box = $("studio-status");
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    if (text === null) { box.hidden = true; return; }
    box.hidden = false;
    if (spinning) box.append(el("span", { className: "spinner", "aria-hidden": "true" }));
    box.append(el("span", { text }));
  }

  function loadLibrary() {
    if (window.agStudio) return Promise.resolve(window.agStudio);
    if (!state.loading) {
      state.loading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = STUDIO_URL;
        script.integrity = STUDIO_SRI;
        script.crossOrigin = "anonymous";
        script.onload = () => (window.agStudio ? resolve(window.agStudio) : reject(new Error("AG Studio did not register its global.")));
        script.onerror = () => reject(new Error("AG Studio could not be loaded from cdn.jsdelivr.net."));
        document.head.append(script);
      });
      state.loading.catch(() => { state.loading = null; });
    }
    return state.loading;
  }

  // AG Charts draws on a canvas, so the theme needs concrete colors, not CSS variables.
  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function buildTheme(lib) {
    const isLight = cssVar("color-scheme", "dark").includes("light") || getComputedStyle(document.documentElement).colorScheme === "light";
    const c = {
      page: cssVar("--page", "#0a0e15"),
      s1: cssVar("--surface-1", "#10161f"),
      s2: cssVar("--surface-2", "#151c27"),
      s3: cssVar("--surface-3", "#1b2431"),
      text: cssVar("--text-primary", "#e7ecf3"),
      text2: cssVar("--text-secondary", "#a7b1c2"),
      muted: cssVar("--text-muted", "#6e7a8e"),
      border: cssVar("--border-strong", "rgba(255,255,255,0.14)"),
      grid: cssVar("--grid", "rgba(255,255,255,0.06)"),
      accent: cssVar("--accent", "#4c8dff"),
      good: cssVar("--status-good", "#22c55e"),
      bad: cssVar("--status-critical", "#ef4f4f"),
    };
    const font = "Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
    return lib.studioTheme.withParams({
      browserColorScheme: isLight ? "light" : "dark",
      backgroundColor: c.s1,
      foregroundColor: c.text,
      textColor: c.text,
      subtleTextColor: c.muted,
      accentColor: c.accent,
      borderColor: c.border,
      fontFamily: font,
      menuBackgroundColor: c.s2,
      studioWrapperBackgroundColor: c.page,
      studioCanvasBackgroundColor: c.page,
      studioCanvasGridLineColor: c.grid,
      studioPanelContainerBackgroundColor: c.s1,
      studioPopupBackgroundColor: c.s2,
      studioWidgetBackgroundColor: c.s1,
      studioWidgetBorderRadius: 12,
      studioWidgetTitleTextColor: c.text,
      studioWidgetTitleFontSize: 15,
      studioWidgetTitleFontWeight: 600,
      studioWidgetTitleFontFamily: font,
      studioCanvasFontFamily: font,
      studioWidgetSubtitleTextColor: c.text2,
      studioWidgetToolbarBackgroundColor: c.s2,
      studioToggleButtonActiveBackgroundColor: c.accent,
      studioToggleButtonActiveBorderColor: c.accent,
      studioToggleButtonActiveColor: c.accent,
      valueChangeUpColor: c.good,
      valueChangeDownColor: c.bad,
      chartFontFamily: font,
      chartTextColor: c.text,
      chartSubtleTextColor: c.muted,
      chartAxisLabelColor: c.text2,
      chartAxisTitleColor: c.text2,
      chartAxisLineColor: c.border,
      chartGridLineColor: c.grid,
      chartLegendLabelColor: c.text2,
      chartTooltipBackgroundColor: c.s3,
      chartTooltipTextColor: c.text,
      chartAccentColor: c.accent,
      gridFontFamily: font,
      gridDataBackgroundColor: c.s1,
      gridOddRowBackgroundColor: c.s1,
      gridHeaderBackgroundColor: c.s2,
      gridHeaderTextColor: c.text2,
      gridCellTextColor: c.text,
      gridRowHoverColor: c.s2,
      gridBrowserColorScheme: isLight ? "light" : "dark",
    });
  }

  // --- data sources (real records only) -------------------------------------------------
  const pad = (n) => String(n).padStart(2, "0");
  // Time buckets adapt to the data: a short demo session (under 10 minutes of activity) is
  // bucketed by 10 seconds, so its requests do not all collapse into one bar.
  let bucketSeconds = 60;
  function chooseBucket(isos) {
    const times = isos.map((x) => Date.parse(x)).filter((t) => !Number.isNaN(t));
    const span = times.length ? (Math.max(...times) - Math.min(...times)) / 1000 : 0;
    bucketSeconds = span < 600 ? 10 : 60;
    return bucketSeconds;
  }
  function minute(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    const base = `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return bucketSeconds < 60 ? `${base}:${pad(Math.floor(d.getSeconds() / bucketSeconds) * bucketSeconds)}` : base;
  }

  const RETURNED = new Set(["REFUNDED", "RELEASED"]);
  function outcome(stateCode) {
    if (RETURNED.has(stateCode)) return "Returned";
    if (stateCode === "FORFEITED") return "Forfeited";
    if (stateCode === "FAILED") return "Failed";
    if (stateCode === "REFUND_PENDING") return "Refund pending";
    return "Held / in progress";
  }

  const text = (id, name) => ({ id, name, format: "textFormat" });
  const cash = (id, name) => ({ id, name, format: "currencyFormat", formatOptions: { format: "$#,##0.00" } });
  const int = (id, name) => ({ id, name, format: "integerFormat" });

  function buildSources(data, requests, runs, commerce) {
    const h = state.ctx.helpers;
    chooseBucket([
      ...(requests || []).map((r) => r.timestamp),
      ...(data ? data.events.map((e) => e.timestamp) : []),
      ...(data ? data.deposits.map((d) => d.created_at) : []),
    ]);
    const deposits = (data ? data.deposits : []).map((d) => {
      const amount = Number(d.amount) || 0;
      const out = outcome(d.deposit_state);
      return {
        deposit_id: d.deposit_id,
        crawler_id: d.crawler_id,
        tier: d.pricing_tier_name || "Base",
        amount,
        currency: d.currency,
        outcome: out,
        state: h.depositLabel(d.deposit_state),
        settlement: d.settlement_mode === "authorization" ? "Authorization hold" : "Capture + refund",
        rail: d.payment_mode === "mock" ? "Mock (simulated)" : "PayPal Sandbox",
        returned_amount: out === "Returned" ? amount : 0,
        forfeited_amount: out === "Forfeited" ? amount : 0,
        created_minute: minute(d.created_at),
      };
    });
    const sessions = (data ? data.sessions : []).map((s) => ({
      session_id: s.session_id,
      crawler_id: s.crawler_id,
      deposit_id: s.deposit_id,
      compliance: s.disqualified ? "Violating" : s.status === "COMPLETED" ? "Compliant" : s.status === "ACTIVE" ? "Active" : h.humanize(s.status),
      allowed: s.allowed_requests,
      blocked: s.rejected_requests,
      violations: s.total_violations,
      total: s.total_requests,
    }));
    const DECISION = { granted: "Granted", payment_required: "HTTP 402 challenge", denied: "Denied" };
    const requestRows = (requests || []).map((r) => ({
      request_id: r.request_id,
      minute: minute(r.timestamp),
      crawler_id: r.crawler_id,
      resource: r.resource,
      tier: r.pricing_tier_name || "Base",
      http_status: r.http_status ? String(r.http_status) : "—",
      decision: DECISION[r.access_decision] || r.access_decision,
      reason: r.reason_code ? h.humanize(r.reason_code) : "Within policy",
      deposit_id: r.deposit_id,
    }));
    const events = (data ? data.events : []);
    const violations = events.filter((e) => e.event_type === "POLICY_VIOLATION").map((e) => ({
      event_id: e.event_id,
      minute: minute(e.timestamp),
      crawler_id: e.crawler_id,
      path: e.path,
      reason: h.humanize(e.reason_code) || "Violation",
      deposit_id: e.deposit_id,
    }));
    const audit = events.map((e) => ({
      event_id: e.event_id,
      minute: minute(e.timestamp),
      category: h.eventCategory(e.event_type),
      event_type: e.event_type,
      crawler_id: e.crawler_id,
      result: e.allowed === true ? "Allowed" : e.allowed === false ? "Blocked" : "—",
    }));
    const runRows = (runs || []).map((run) => {
      const d = run.detail || {};
      const u = d.usage || {};
      const input = Number(u.input_tokens) || 0;
      const output = Number(u.output_tokens) || 0;
      return {
        run_id: run.run_id,
        driver: run.driver === "ai" ? "AI agent" : "Scripted",
        persona: run.persona === "rogue" ? "Rogue scraper" : run.persona === "assistant" ? "Personal agent" : "Compliant crawler",
        status: h.humanize(run.status),
        outcome: run.deposit_state ? outcome(run.deposit_state) : "No deposit",
        tier: run.pricing_tier ? h.humanize(run.pricing_tier) : "Base",
        model: run.driver === "ai" ? `${h.providerName(d.provider)} ${d.model || ""}`.trim() : "—",
        turns: Number(u.turns) || 0,
        tokens: input + output,
        crawler_id: run.crawler_id,
        deposit_id: run.deposit_id,
        started_minute: minute(run.started_at),
      };
    });

    // Agent commerce: one row per payment, whatever its type, so revenue mixes cleanly.
    const ORDER_STATE = { PAID: "Paid", REFUNDED: "Refunded", CANCELLED: "Cancelled", CREATED: "Awaiting payment", APPROVAL_REQUIRED: "Awaiting payment", FAILED: "Failed" };
    const payments = [];
    for (const o of (commerce && commerce.orders) || []) {
      const amount = Number(o.amount) || 0;
      payments.push({
        payment_id: o.order_id,
        type: o.kind === "access" ? "Pay-per-access" : "Product order",
        item: (o.lines || []).map((l) => `${l.quantity > 1 ? `${l.quantity}x ` : ""}${l.name}`).join(", "),
        agent_id: o.agent_id,
        status: ORDER_STATE[o.status] || h.humanize(o.status),
        amount,
        revenue: o.status === "PAID" ? amount : 0,
        rail: o.simulated ? "Mock (simulated)" : "PayPal Sandbox",
        created_minute: minute(o.created_at),
      });
    }
    for (const s of (commerce && commerce.subscriptions) || []) {
      const amount = Number(s.amount) || 0;
      payments.push({
        payment_id: s.subscription_ref,
        type: "Subscription",
        item: `${s.plan_name} (per ${String(s.interval_unit || "").toLowerCase()})`,
        agent_id: s.agent_id,
        status: h.humanize(s.status),
        amount,
        revenue: s.status === "ACTIVE" ? amount : 0,
        rail: s.simulated ? "Mock (simulated)" : "PayPal Sandbox",
        created_minute: minute(s.created_at),
      });
    }
    for (const d of deposits) {
      payments.push({
        payment_id: d.deposit_id,
        type: "Crawl deposit",
        item: `${d.tier} tier deposit`,
        agent_id: d.crawler_id,
        status: d.outcome,
        amount: d.amount,
        revenue: d.forfeited_amount,
        rail: d.rail,
        created_minute: d.created_minute,
      });
    }

    return {
      sources: [
        { id: "payments", name: "Agent payments (all types)", primaryKeyField: "payment_id", data: payments, fields: [
          text("payment_id", "Payment ID"), text("type", "Payment type"), text("item", "Item"), text("agent_id", "Agent"),
          text("status", "Status"), cash("amount", "Amount"), cash("revenue", "Revenue kept"), text("rail", "Payment rail"),
          text("created_minute", "Created"),
        ] },
        { id: "deposits", name: "Deposits", primaryKeyField: "deposit_id", data: deposits, fields: [
          text("deposit_id", "Deposit ID"), text("crawler_id", "Crawler"), text("tier", "Pricing tier"), cash("amount", "Deposit amount"),
          text("currency", "Currency"), text("outcome", "Outcome"), text("state", "Deposit state"), text("settlement", "Settlement"),
          text("rail", "Payment rail"), cash("returned_amount", "Returned amount"), cash("forfeited_amount", "Forfeited amount"),
          text("created_minute", "Created"),
        ] },
        { id: "sessions", name: "Crawler sessions", primaryKeyField: "session_id", data: sessions, fields: [
          text("session_id", "Session ID"), text("crawler_id", "Crawler"), text("deposit_id", "Deposit ID"), text("compliance", "Compliance"),
          int("allowed", "Allowed requests"), int("blocked", "Blocked requests"), int("violations", "Violations"), int("total", "Total requests"),
        ] },
        { id: "requests", name: "Gateway requests", primaryKeyField: "request_id", data: requestRows, fields: [
          text("request_id", "Request ID"), text("minute", "Time"), text("crawler_id", "Crawler"), text("resource", "Resource"),
          text("tier", "Pricing tier"), text("http_status", "HTTP status"), text("decision", "Decision"), text("reason", "Reason"),
          text("deposit_id", "Deposit ID"),
        ] },
        { id: "violations", name: "Policy violations", primaryKeyField: "event_id", data: violations, fields: [
          text("event_id", "Event ID"), text("minute", "Time"), text("crawler_id", "Crawler"), text("path", "Path"),
          text("reason", "Violation"), text("deposit_id", "Deposit ID"),
        ] },
        { id: "audit", name: "Audit events", primaryKeyField: "event_id", data: audit, fields: [
          text("event_id", "Event ID"), text("minute", "Time"), text("category", "Category"), text("event_type", "Event type"),
          text("crawler_id", "Crawler"), text("result", "Result"),
        ] },
        { id: "runs", name: "Demo agent runs", primaryKeyField: "run_id", data: runRows, fields: [
          text("run_id", "Run ID"), text("driver", "Driver"), text("persona", "Persona"), text("status", "Run status"),
          text("outcome", "Deposit outcome"), text("tier", "Tier"), text("model", "Model"), int("turns", "Model turns"),
          int("tokens", "Tokens"), text("crawler_id", "Crawler"), text("deposit_id", "Deposit ID"), text("started_minute", "Started"),
        ] },
      ],
      relationships: [
        { id: "request-deposit", source: "requests.deposit_id", target: "deposits.deposit_id", type: "many-to-one" },
        { id: "session-deposit", source: "sessions.deposit_id", target: "deposits.deposit_id", type: "many-to-one" },
        { id: "violation-deposit", source: "violations.deposit_id", target: "deposits.deposit_id", type: "many-to-one" },
        { id: "run-deposit", source: "runs.deposit_id", target: "deposits.deposit_id", type: "many-to-one" },
      ],
      counts: { payments: payments.length, deposits: deposits.length, sessions: sessions.length, requests: requestRows.length, violations: violations.length, audit: audit.length, runs: runRows.length },
    };
  }

  // --- pre-built report ------------------------------------------------------------------
  const kpi = (id, aggregation, label) => ({ type: "value", dataMapping: { value: [{ id, aggregation }] }, format: { style: { label: { enabled: true, text: label } } } });
  const title = (t) => ({ title: { enabled: true, text: t } });
  // Multi-series charts show their legend under the plot.
  const titled = (t) => ({ title: { enabled: true, text: t }, style: { theme: { common: { legend: { enabled: true, position: "bottom" } } } } });
  const desc = (id, aggregation) => [{ field: { id, aggregation }, direction: "desc" }];
  const asc = (id) => [{ field: { id }, direction: "asc" }];

  function initialState() {
    return {
      selectedPageId: state.page,
      panels: { filters: { collapsed: true }, edit: { collapsed: true }, data: { collapsed: true } },
      pages: [
        {
          id: "overview",
          widgets: {
            "k-deposits": kpi("deposits.deposit_id", "countd", "Deposits"),
            "k-returned": kpi("deposits.returned_amount", "sum", "Returned to crawlers"),
            "k-forfeited": kpi("deposits.forfeited_amount", "sum", "Forfeited to publisher"),
            "k-requests": kpi("requests.request_id", "count", "Gateway requests"),
            "k-violations": kpi("sessions.violations", "sum", "Policy violations"),
            "outcomes": { type: "donut-chart", dataMapping: { categoryKey: [{ id: "deposits.outcome" }], valueKey: [{ id: "deposits.deposit_id", aggregation: "count" }] }, sort: asc("deposits.outcome"), format: titled("Deposit outcomes") },
            "activity": {
              type: "column-chart-stacked",
              dataMapping: { categoryKey: [{ id: "requests.minute" }], valueKey: [{ id: "requests.request_id", aggregation: "count" }], legendKey: [{ id: "requests.decision" }] },
              sort: asc("requests.minute"),
              format: titled("Crawler activity and compliance over time"),
            },
            "tier-filter": { type: "list-filter", dataMapping: { value: [{ id: "deposits.tier" }] }, format: title("Pricing tier") },
            "by-crawler": {
              type: "grid",
              dataMapping: { cols: [
                { id: "deposits.crawler_id" }, { id: "deposits.deposit_id", aggregation: "countd" }, { id: "deposits.amount", aggregation: "sum" },
                { id: "deposits.returned_amount", aggregation: "sum" }, { id: "deposits.forfeited_amount", aggregation: "sum" },
              ] },
              sort: desc("deposits.amount", "sum"),
              format: title("Crawlers by deposit value"),
            },
          },
          widgetLayout: {
            "k-deposits": { xTrack: 0, yTrack: 0, xSpan: 5, ySpan: 6 },
            "k-returned": { xTrack: 5, yTrack: 0, xSpan: 5, ySpan: 6 },
            "k-forfeited": { xTrack: 10, yTrack: 0, xSpan: 5, ySpan: 6 },
            "k-requests": { xTrack: 15, yTrack: 0, xSpan: 5, ySpan: 6 },
            "k-violations": { xTrack: 20, yTrack: 0, xSpan: 4, ySpan: 6 },
            "outcomes": { xTrack: 0, yTrack: 6, xSpan: 8, ySpan: 18 },
            "activity": { xTrack: 8, yTrack: 6, xSpan: 16, ySpan: 18 },
            "tier-filter": { xTrack: 0, yTrack: 24, xSpan: 6, ySpan: 16 },
            "by-crawler": { xTrack: 6, yTrack: 24, xSpan: 18, ySpan: 16 },
          },
        },
        {
          id: "commerce",
          widgets: {
            "c-revenue": kpi("payments.revenue", "sum", "Revenue kept"),
            "c-payments": kpi("payments.payment_id", "countd", "Agent payments"),
            "c-agents": kpi("payments.agent_id", "countd", "Paying agents"),
            "c-mix": { type: "donut-chart", dataMapping: { categoryKey: [{ id: "payments.type" }], valueKey: [{ id: "payments.revenue", aggregation: "sum" }] }, sort: asc("payments.type"), format: titled("Revenue by payment type") },
            "c-status": {
              type: "bar-chart-stacked",
              dataMapping: { categoryKey: [{ id: "payments.type" }], valueKey: [{ id: "payments.payment_id", aggregation: "count" }], legendKey: [{ id: "payments.status" }] },
              sort: asc("payments.type"),
              format: titled("Payments by type and status"),
            },
            "c-type-filter": { type: "button-filter", dataMapping: { value: [{ id: "payments.type" }] }, format: title("Payment type") },
            "c-grid": {
              type: "grid",
              dataMapping: { cols: [
                { id: "payments.created_minute" }, { id: "payments.type" }, { id: "payments.item" }, { id: "payments.agent_id" },
                { id: "payments.status" }, { id: "payments.amount" }, { id: "payments.revenue" }, { id: "payments.rail" },
              ] },
              sort: desc("payments.amount"),
              format: title("Every agent payment"),
            },
          },
          widgetLayout: {
            "c-revenue": { xTrack: 0, yTrack: 0, xSpan: 8, ySpan: 6 },
            "c-payments": { xTrack: 8, yTrack: 0, xSpan: 8, ySpan: 6 },
            "c-agents": { xTrack: 16, yTrack: 0, xSpan: 8, ySpan: 6 },
            "c-mix": { xTrack: 0, yTrack: 6, xSpan: 9, ySpan: 18 },
            "c-status": { xTrack: 9, yTrack: 6, xSpan: 15, ySpan: 18 },
            "c-type-filter": { xTrack: 0, yTrack: 24, xSpan: 24, ySpan: 5 },
            "c-grid": { xTrack: 0, yTrack: 29, xSpan: 24, ySpan: 16 },
          },
        },
        {
          id: "finance",
          widgets: {
            "f-total": kpi("deposits.amount", "sum", "Total deposited"),
            "f-avg": kpi("deposits.amount", "avg", "Average deposit"),
            "f-returned": kpi("deposits.returned_amount", "sum", "Refunded or released"),
            "f-forfeited": kpi("deposits.forfeited_amount", "sum", "Forfeited"),
            "f-tier": {
              type: "bar-chart-stacked",
              dataMapping: { categoryKey: [{ id: "deposits.tier" }], valueKey: [{ id: "deposits.amount", aggregation: "sum" }], legendKey: [{ id: "deposits.outcome" }] },
              sort: asc("deposits.outcome"),
              format: titled("Deposit value by content tier and outcome"),
            },
            "f-outcome": { type: "donut-chart", dataMapping: { categoryKey: [{ id: "deposits.outcome" }], valueKey: [{ id: "deposits.amount", aggregation: "sum" }] }, sort: asc("deposits.outcome"), format: titled("Value by outcome") },
            "f-rail": { type: "button-filter", dataMapping: { value: [{ id: "deposits.rail" }] }, format: title("Payment rail") },
            "f-grid": {
              type: "grid",
              dataMapping: { cols: [
                { id: "deposits.deposit_id" }, { id: "deposits.crawler_id" }, { id: "deposits.tier" }, { id: "deposits.amount" },
                { id: "deposits.outcome" }, { id: "deposits.settlement" }, { id: "deposits.rail" }, { id: "deposits.created_minute" },
              ] },
              format: title("Deposit ledger"),
            },
          },
          widgetLayout: {
            "f-total": { xTrack: 0, yTrack: 0, xSpan: 6, ySpan: 6 },
            "f-avg": { xTrack: 6, yTrack: 0, xSpan: 6, ySpan: 6 },
            "f-returned": { xTrack: 12, yTrack: 0, xSpan: 6, ySpan: 6 },
            "f-forfeited": { xTrack: 18, yTrack: 0, xSpan: 6, ySpan: 6 },
            "f-tier": { xTrack: 0, yTrack: 6, xSpan: 14, ySpan: 18 },
            "f-outcome": { xTrack: 14, yTrack: 6, xSpan: 10, ySpan: 18 },
            "f-rail": { xTrack: 0, yTrack: 24, xSpan: 24, ySpan: 7 },
            "f-grid": { xTrack: 0, yTrack: 31, xSpan: 24, ySpan: 18 },
          },
        },
        {
          id: "security",
          widgets: {
            "s-violations": kpi("violations.event_id", "count", "Violations (recent events)"),
            "s-offenders": kpi("violations.crawler_id", "countd", "Offending crawlers"),
            "s-blocked": kpi("sessions.blocked", "sum", "Blocked requests"),
            "s-reason": {
              type: "bar-chart-grouped",
              dataMapping: { categoryKey: [{ id: "violations.reason" }], valueKey: [{ id: "violations.event_id", aggregation: "count" }] },
              sort: desc("violations.event_id", "count"),
              format: title("Violations by type"),
            },
            "s-crawler": {
              type: "column-chart-grouped",
              dataMapping: { categoryKey: [{ id: "violations.crawler_id" }], valueKey: [{ id: "violations.event_id", aggregation: "count" }] },
              sort: desc("violations.event_id", "count"),
              format: title("Suspicious crawlers"),
            },
            "s-crawler-decision": {
              type: "bar-chart-stacked",
              dataMapping: { categoryKey: [{ id: "requests.crawler_id" }], valueKey: [{ id: "requests.request_id", aggregation: "count" }], legendKey: [{ id: "requests.decision" }] },
              sort: asc("requests.decision"),
              format: titled("Requests by crawler and gateway decision"),
            },
            "s-trend": {
              type: "line-chart",
              dataMapping: { categoryKey: [{ id: "requests.minute" }], valueKey: [{ id: "requests.request_id", aggregation: "count" }], legendKey: [{ id: "requests.decision" }] },
              sort: asc("requests.minute"),
              format: titled("Gateway decisions over time"),
            },
            "s-grid": {
              type: "grid",
              dataMapping: { cols: [{ id: "violations.minute" }, { id: "violations.reason" }, { id: "violations.crawler_id" }, { id: "violations.path" }, { id: "violations.deposit_id" }] },
              format: title("Violation log"),
            },
          },
          widgetLayout: {
            "s-violations": { xTrack: 0, yTrack: 0, xSpan: 8, ySpan: 6 },
            "s-offenders": { xTrack: 8, yTrack: 0, xSpan: 8, ySpan: 6 },
            "s-blocked": { xTrack: 16, yTrack: 0, xSpan: 8, ySpan: 6 },
            "s-reason": { xTrack: 0, yTrack: 6, xSpan: 12, ySpan: 18 },
            "s-crawler": { xTrack: 12, yTrack: 6, xSpan: 12, ySpan: 18 },
            "s-trend": { xTrack: 0, yTrack: 24, xSpan: 12, ySpan: 18 },
            "s-crawler-decision": { xTrack: 12, yTrack: 24, xSpan: 12, ySpan: 18 },
            "s-grid": { xTrack: 0, yTrack: 42, xSpan: 24, ySpan: 16 },
          },
        },
        {
          id: "agents",
          widgets: {
            "a-runs": kpi("runs.run_id", "count", "Recent demo runs"),
            "a-turns": kpi("runs.turns", "sum", "Model turns"),
            "a-tokens": kpi("runs.tokens", "sum", "Model tokens"),
            "a-outcome": {
              type: "bar-chart-stacked",
              dataMapping: { categoryKey: [{ id: "runs.persona" }], valueKey: [{ id: "runs.run_id", aggregation: "count" }], legendKey: [{ id: "runs.outcome" }] },
              sort: asc("runs.outcome"),
              format: titled("Outcome by crawler persona"),
            },
            "a-driver": { type: "donut-chart", dataMapping: { categoryKey: [{ id: "runs.driver" }], valueKey: [{ id: "runs.run_id", aggregation: "count" }] }, format: titled("Execution mode") },
            "a-grid": {
              type: "grid",
              dataMapping: { cols: [
                { id: "runs.run_id" }, { id: "runs.persona" }, { id: "runs.driver" }, { id: "runs.model" }, { id: "runs.status" },
                { id: "runs.outcome" }, { id: "runs.tier" }, { id: "runs.turns" }, { id: "runs.tokens" },
              ] },
              format: title("Agent execution log"),
            },
          },
          widgetLayout: {
            "a-runs": { xTrack: 0, yTrack: 0, xSpan: 8, ySpan: 6 },
            "a-turns": { xTrack: 8, yTrack: 0, xSpan: 8, ySpan: 6 },
            "a-tokens": { xTrack: 16, yTrack: 0, xSpan: 8, ySpan: 6 },
            "a-outcome": { xTrack: 0, yTrack: 6, xSpan: 14, ySpan: 18 },
            "a-driver": { xTrack: 14, yTrack: 6, xSpan: 10, ySpan: 18 },
            "a-grid": { xTrack: 0, yTrack: 24, xSpan: 24, ySpan: 16 },
          },
        },
      ],
    };
  }

  // --- lifecycle ---------------------------------------------------------------------------
  async function collect() {
    const [requests, runs, commerce] = await Promise.all([
      state.ctx.loadRequests().catch(() => null),
      state.ctx.loadRuns().catch(() => []),
      (state.ctx.loadCommerce ? state.ctx.loadCommerce() : Promise.resolve(null)).catch(() => null),
    ]);
    return buildSources(state.ctx.getData(), requests, runs, commerce);
  }

  function renderSourceStrip(counts) {
    const strip = $("studio-sources");
    while (strip.firstChild) strip.removeChild(strip.firstChild);
    const labels = { deposits: "Deposits", sessions: "Sessions", requests: "Requests", violations: "Violations", audit: "Audit events", runs: "Agent runs" };
    for (const [key, label] of Object.entries(labels)) {
      strip.append(el("span", { className: "badge" }, [el("span", { className: "badge-dot", "data-tone": counts[key] ? "good" : "neutral" }), el("span", { text: `${label}: ${counts[key]} rows` })]));
    }
  }

  function renderPageButtons() {
    const group = $("studio-pages");
    while (group.firstChild) group.removeChild(group.firstChild);
    for (const [id, label] of PAGES) {
      group.append(el("button", {
        type: "button",
        text: label,
        "aria-pressed": id === state.page ? "true" : "false",
        onclick: () => selectPage(id),
      }));
    }
  }

  function selectPage(id) {
    state.page = id;
    renderPageButtons();
    if (!state.api) return;
    const current = state.api.getState();
    state.api.setState(Object.assign({}, current, { selectedPageId: id }));
  }

  async function refreshData() {
    if (!state.api || state.busy) return;
    state.busy = true;
    $("studio-refresh").disabled = true;
    try {
      const data = await collect();
      renderSourceStrip(data.counts);
      state.api.setProperty("data", { sources: data.sources, relationships: data.relationships });
      $("studio-hint").textContent = `Data refreshed ${new Date().toLocaleTimeString()} from /dashboard/data, /demo/requests, and /demo/config (time buckets: ${bucketSeconds} s). Nothing is sampled or simulated.`;
    } finally {
      state.busy = false;
      $("studio-refresh").disabled = false;
    }
  }

  async function build() {
    if (state.built || state.busy) return;
    state.busy = true;
    status("Loading AG Studio...", true);
    try {
      const lib = await loadLibrary();
      if (!state.ctx.getData()) await new Promise((resolve) => setTimeout(resolve, 800));
      status("Preparing the gateway's records...", true);
      const data = await collect();
      renderSourceStrip(data.counts);
      const host = el("div", { className: "studio-mount", style: "position:absolute; inset:0;" });
      $("studio-host").append(host);
      let theme;
      try { theme = buildTheme(lib); } catch (_) { theme = undefined; }
      state.api = lib.createStudio(host, {
        mode: state.mode,
        theme,
        initialState: initialState(),
        data: { sources: data.sources, relationships: data.relationships },
        onStudioReady: () => status(null),
        onErrorRaised: (event) => {
          const msg = event && (event.message || (event.error && event.error.message));
          if (msg) $("studio-hint").textContent = `AG Studio: ${msg}`;
        },
      });
      state.built = true;
      $("studio-hint").textContent = `Loaded ${new Date().toLocaleTimeString()} from /dashboard/data, /demo/requests, and /demo/config (time buckets: ${bucketSeconds} s). Nothing is sampled or simulated.`;
      setTimeout(() => status(null), 4000);
    } catch (error) {
      status(`${error && error.message ? error.message : "AG Studio failed to start."} The same data is available in the Operations and Security views.`, false);
    } finally {
      state.busy = false;
    }
  }

  function toggleMode() {
    state.mode = state.mode === "edit" ? "view" : "edit";
    const button = $("studio-mode");
    button.textContent = state.mode === "edit" ? "Done editing" : "Edit dashboard";
    button.setAttribute("aria-pressed", String(state.mode === "edit"));
    if (state.api) state.api.setProperty("mode", state.mode);
  }

  function themeChanged() {
    if (!state.api || !window.agStudio) return;
    // Wait a frame so the new CSS variables are in effect before reading them.
    requestAnimationFrame(() => {
      try { state.api.setProperty("theme", buildTheme(window.agStudio)); } catch (_) { /* keep the current theme */ }
    });
  }

  let wired = false;
  function open(ctx) {
    state.ctx = ctx;
    if (!wired) {
      wired = true;
      renderPageButtons();
      $("studio-refresh").addEventListener("click", refreshData);
      $("studio-mode").addEventListener("click", toggleMode);
    }
    if (state.built) refreshData();
    else build();
  }

  // Download the library in the background (console idle time); errors surface on open.
  function prefetch() {
    loadLibrary().catch(() => { /* reported when the Analytics view opens */ });
  }

  window.CTAnalytics = { open, themeChanged, prefetch };
})();
