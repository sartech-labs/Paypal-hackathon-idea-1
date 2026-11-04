/* CrawlerTrust publisher console.
   All values that originate from crawlers (IDs, paths, reasons) are inserted with
   textContent, never innerHTML. */
(() => {
  "use strict";

  const REFRESH_MS = 3000;
  const SLOW_FETCH_MS = 400;

  const state = {
    token: null,
    data: null,
    selectedDepositId: null,
    selectedUpdatedAt: null,
    grids: {},
    chartAsTable: false,
    timer: null,
    inFlight: false,
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
      if (child !== null && child !== undefined) node.append(child);
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
    };
    if (known[code]) return known[code];
    const lower = code.toLowerCase().replace(/_/g, " ");
    return lower.charAt(0).toUpperCase() + lower.slice(1);
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

  function chip(tone, icon, label) {
    return el("span", { className: "chip" }, [
      el("span", { className: "chip-icon", dataset: { tone }, "aria-hidden": "true", text: icon }),
      el("span", { text: label }),
    ]);
  }

  function depositChip(stateCode) {
    const [tone, icon, label] = DEPOSIT_STATUS[stateCode] || ["neutral", "●", humanize(stateCode)];
    return chip(tone, icon, label);
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
    return el("span", { text: "" });
  }

  // --- theme ---------------------------------------------------------------------------
  const THEMES = ["system", "light", "dark"];

  function applyTheme(pref) {
    const root = document.documentElement;
    if (pref === "light" || pref === "dark") root.dataset.theme = pref;
    else delete root.dataset.theme;
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

  // --- grids ---------------------------------------------------------------------------
  function gridTheme() {
    return window.agGrid.themeQuartz.withParams({
      backgroundColor: "var(--surface-1)",
      foregroundColor: "var(--text-primary)",
      chromeBackgroundColor: "var(--surface-2)",
      headerBackgroundColor: "var(--surface-2)",
      headerTextColor: "var(--text-secondary)",
      borderColor: "var(--grid)",
      oddRowBackgroundColor: "var(--surface-1)",
      rowHoverColor: "var(--surface-2)",
      selectedRowBackgroundColor: "var(--surface-2)",
      accentColor: "var(--accent)",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      fontSize: 13,
      headerFontWeight: 600,
      spacing: 6,
      wrapperBorderRadius: 8,
      browserColorScheme: "inherit",
    });
  }

  function cellNode(render) {
    return (params) => (params.data ? render(params.data) : "");
  }

  function initGrids() {
    if (!window.agGrid || typeof window.agGrid.createGrid !== "function") {
      for (const id of ["grid-deposits", "grid-sessions", "grid-events"]) {
        const node = $(id);
        clear(node);
        node.append(el("p", { className: "empty", text: "The table library (AG Grid, cdn.jsdelivr.net) could not be loaded. Data is still available from /dashboard/data." }));
      }
      return false;
    }
    if (window.agGrid.ModuleRegistry && window.agGrid.AllCommunityModule) {
      window.agGrid.ModuleRegistry.registerModules([window.agGrid.AllCommunityModule]);
    }
    const theme = gridTheme();
    const defaultColDef = { sortable: true, resizable: true, minWidth: 90, flex: 1 };

    state.grids.deposits = window.agGrid.createGrid($("grid-deposits"), {
      theme,
      defaultColDef,
      rowData: [],
      getRowId: (p) => p.data.deposit_id,
      rowSelection: { mode: "singleRow", checkboxes: false, enableClickSelection: true },
      onRowClicked: (event) => selectDeposit(event.data),
      columnDefs: [
        { headerName: "Outcome", field: "deposit_state", minWidth: 150, cellRenderer: cellNode((d) => depositChip(d.deposit_state)) },
        { headerName: "Crawler", field: "crawler_id", minWidth: 150 },
        { headerName: "Deposit", field: "amount", maxWidth: 120, valueFormatter: (p) => (p.data ? `${p.data.amount} ${p.data.currency}` : "") },
        { headerName: "Settlement", field: "settlement_mode", maxWidth: 130, valueFormatter: (p) => (p.value === "authorization" ? "Hold" : "Refund") },
        { headerName: "Decision", field: "policy_decision", minWidth: 150, valueFormatter: (p) => humanize(p.value) },
        { headerName: "Created", field: "created_at", maxWidth: 120, sort: "desc", valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
        {
          headerName: "",
          colId: "explain",
          sortable: false,
          resizable: false,
          maxWidth: 104,
          minWidth: 96,
          cellRenderer: cellNode((d) =>
            el("button", {
              className: "cell-button",
              type: "button",
              text: "Explain",
              "aria-label": `Explain deposit ${d.deposit_id}`,
              onclick: (event) => { event.stopPropagation(); selectDeposit(d); },
            })
          ),
        },
      ],
    });

    state.grids.sessions = window.agGrid.createGrid($("grid-sessions"), {
      theme,
      defaultColDef,
      rowData: [],
      getRowId: (p) => p.data.session_id,
      columnDefs: [
        { headerName: "Status", field: "status", minWidth: 170, cellRenderer: cellNode(sessionChip) },
        { headerName: "Crawler", field: "crawler_id", minWidth: 150 },
        { headerName: "Allowed", field: "allowed_requests", maxWidth: 110, type: "numericColumn" },
        { headerName: "Blocked", field: "rejected_requests", maxWidth: 110, type: "numericColumn" },
        { headerName: "Violations", field: "total_violations", maxWidth: 120, type: "numericColumn" },
        { headerName: "Started", field: "created_at", maxWidth: 120, sort: "desc", valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
      ],
    });

    state.grids.events = window.agGrid.createGrid($("grid-events"), {
      theme,
      defaultColDef,
      rowData: [],
      getRowId: (p) => p.data.event_id,
      columnDefs: [
        { headerName: "Time", field: "timestamp", maxWidth: 110, sort: "desc", valueFormatter: (p) => formatTime(p.value), tooltipValueGetter: (p) => formatDateTime(p.value) },
        { headerName: "Event", field: "event_type", minWidth: 200, cellClass: "mono" },
        { headerName: "Crawler", field: "crawler_id", minWidth: 140 },
        { headerName: "Path", field: "path", minWidth: 140, cellClass: "mono" },
        { headerName: "Reason", field: "reason_code", minWidth: 150, valueFormatter: (p) => humanize(p.value) },
        { headerName: "Result", field: "allowed", maxWidth: 120, cellRenderer: cellNode((e) => allowedChip(e.allowed)) },
      ],
    });

    $("quick-filter").addEventListener("input", (event) => {
      for (const grid of Object.values(state.grids)) grid.setGridOption("quickFilterText", event.target.value);
    });
    return true;
  }

  // --- render: header, KPIs, chart -----------------------------------------------------
  function renderBadges(data) {
    const badges = $("badges");
    clear(badges);
    const add = (tone, text) =>
      badges.append(el("span", { className: "badge" }, [el("span", { className: "badge-dot", dataset: { tone } }), el("span", { text })]));
    add(data.payment_mode === "mock" ? "neutral" : "accent", data.payment_mode === "mock" ? "Mock payments (simulated)" : "PayPal Sandbox");
    add("accent", data.settlement_mode === "authorization" ? "Settlement: authorization hold" : "Settlement: capture + refund");
    add(data.ai_available ? "good" : "neutral", data.ai_available ? `Claude AI: ${data.ai_model}` : "AI: rule-based explanations only");
    add("neutral", data.access === "admin" ? "Admin view" : "Read-only public view");
    $("site-line").textContent = `${data.site_name} · policy ${data.policy_version}`;
  }

  function tile(label, value, sub) {
    return el("div", { className: "tile" }, [
      el("div", { className: "tile-label", text: label }),
      el("div", { className: "tile-value", text: value }),
      sub ? el("div", { className: "tile-sub", text: sub }) : null,
    ]);
  }

  function renderKpis(k) {
    const kpis = $("kpis");
    clear(kpis);
    kpis.append(
      tile("Active sessions", compact(k.sessions_active), `${compact(k.sessions_total)} total`),
      tile("Deposits held", compact(k.deposits_held), k.deposits_needing_attention ? `${k.deposits_needing_attention} need attention` : "During live sessions"),
      tile("Deposits returned", compact(k.deposits_returned), `${k.amount_returned} ${k.currency} returned`),
      tile("Deposits forfeited", compact(k.deposits_forfeited), `${k.amount_forfeited} ${k.currency} kept`),
      tile("Policy violations", compact(k.policy_violations), "Disallowed paths, traversal, bursts"),
      tile("Requests allowed", compact(k.gateway_requests_allowed), `${compact(k.gateway_requests_rejected)} blocked`)
    );
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
          el("span", { className: "bar", style: `width: ${Math.max(2, (count / max) * 100 * 0.85)}%` }),
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

  // --- explanation panel ---------------------------------------------------------------
  function selectDeposit(row) {
    if (!row) return;
    state.selectedDepositId = row.deposit_id;
    state.selectedUpdatedAt = row.updated_at;
    loadExplanation(row.deposit_id);
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
        el("span", { className: "badge-dot", dataset: { tone: x.generated_by === "claude" ? "good" : "neutral" } }),
        el("span", { text: x.generated_by === "claude" ? `Explained by Claude${x.model ? ` (${x.model})` : ""}` : "Rule-based explanation" }),
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

  // --- policy assistant ----------------------------------------------------------------
  function updateAssistantState() {
    const button = $("draft-button");
    const hint = $("draft-hint");
    const ready = Boolean(state.token);
    button.disabled = !ready;
    hint.textContent = ready
      ? (state.data && !state.data.ai_available ? "AI is not configured on the gateway; the request will report that." : "Uses Claude through the gateway (admin).")
      : "Requires the admin token.";
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
    output.append(el("p", { className: "notice", text: "Drafting with Claude..." }));
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

  // --- data loop -----------------------------------------------------------------------
  async function refresh() {
    if (state.inFlight) return;
    state.inFlight = true;
    const slowTimer = setTimeout(() => document.querySelector(".layout").classList.add("refreshing"), SLOW_FETCH_MS);
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
    } catch (_) {
      showBanner("Cannot reach the CrawlerTrust gateway. Is the server running?");
    } finally {
      clearTimeout(slowTimer);
      document.querySelector(".layout").classList.remove("refreshing");
      state.inFlight = false;
    }
  }

  function render(data) {
    state.data = data;
    renderBadges(data);
    renderKpis(data.kpis);
    renderViolations(data.kpis.violations_by_reason);
    if (state.grids.deposits) {
      state.grids.deposits.setGridOption("rowData", data.deposits);
      state.grids.sessions.setGridOption("rowData", data.sessions);
      state.grids.events.setGridOption("rowData", data.events);
    }
    updateAssistantState();
    $("updated").textContent = `Updated ${formatTime(data.generated_at)}`;

    // Re-explain automatically when the selected deposit's outcome changes (live demo).
    if (state.selectedDepositId) {
      const row = data.deposits.find((d) => d.deposit_id === state.selectedDepositId);
      if (row && row.updated_at !== state.selectedUpdatedAt) {
        state.selectedUpdatedAt = row.updated_at;
        loadExplanation(row.deposit_id);
      }
    }
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

    initGrids();
    updateAssistantState();
    refresh();
    setAutoRefresh(auto.checked);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
