/* SARA grid kit: one AG Grid (Community) setup shared by the live demo and the console.

   Adds, on top of a plain AG Grid:
   - a toolbar: quick search, segment filters, a floating-filter toggle, a row counter, and CSV
     export (of the selected rows when any are selected, otherwise of the filtered rows);
   - expandable detail rows built from Community features: a full-width row inserted after its
     parent, kept next to it through sorting with postSortRows, and carrying the parent's field
     values so column filters and the quick filter treat parent and detail alike;
   - pagination, multi-row selection, a pinned totals row, and a flash on rows that arrive live.

   Everything rendered from row data uses textContent; nothing here touches innerHTML. */
(() => {
  "use strict";

  const DETAIL = "__detailOf";

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

  function available() {
    return Boolean(window.agGrid && typeof window.agGrid.createGrid === "function");
  }

  let registered = false;
  function register() {
    if (registered || !available()) return;
    if (window.agGrid.ModuleRegistry && window.agGrid.AllCommunityModule) {
      window.agGrid.ModuleRegistry.registerModules([window.agGrid.AllCommunityModule]);
    }
    registered = true;
  }

  // Colors come from the page's CSS variables, so the grid follows the theme toggle live.
  function theme() {
    return window.agGrid.themeQuartz.withParams({
      backgroundColor: "var(--surface-1)",
      foregroundColor: "var(--text-primary)",
      chromeBackgroundColor: "var(--surface-2)",
      headerBackgroundColor: "var(--surface-2)",
      headerTextColor: "var(--text-secondary)",
      borderColor: "var(--border)",
      oddRowBackgroundColor: "var(--surface-1)",
      rowHoverColor: "var(--surface-2)",
      selectedRowBackgroundColor: "var(--accent-soft)",
      accentColor: "var(--accent)",
      fontFamily: "var(--font)",
      fontSize: 13,
      headerFontSize: 12,
      headerFontWeight: 600,
      rowHeight: 40,
      headerHeight: 40,
      spacing: 6,
      wrapperBorderRadius: 10,
      browserColorScheme: "inherit",
    });
  }

  function unavailable(node, message) {
    while (node.firstChild) node.removeChild(node.firstChild);
    node.append(el("p", { className: "empty", text: message }));
  }

  /**
   * create(node, options) builds a toolbar above `node` and a grid inside it.
   *
   * options: {
   *   columnDefs, rowId(row) -> string,
   *   detail(row) -> Node            optional: enables expandable rows
   *   detailHeight                   optional, default 176
   *   selection: "multi" | "single" | null
   *   onRowClicked(row)              optional
   *   segments: { label, options: [{ id, label, test(row) }] }   optional external filter
   *   exportName                     CSV file name prefix
   *   exportValues: { colId: row => value }   optional CSV values for rendered columns
   *   noExport: [colId]              columns left out of the CSV (action buttons)
   *   totals(rows) -> object         optional pinned bottom row
   *   flashNew                       flash rows that arrive after the first load
   *   pageSize                       default 25; 0 disables pagination
   *   noRows                         overlay text
   *   search                         placeholder text; false hides the box
   *   toolbar                        false hides the toolbar
   *   gridOptions                    extra AG Grid options
   * }
   */
  function create(node, options) {
    const opts = Object.assign({ selection: "multi", detailHeight: 176, pageSize: 25, flashNew: false, search: "Search rows...", toolbar: true }, options || {});
    if (!available()) {
      unavailable(node, "The table library (AG Grid, cdn.jsdelivr.net) could not be loaded.");
      return null;
    }
    register();

    const ctl = {
      api: null,
      rows: [],
      expanded: new Set(),
      seen: null,
      segment: "all",
      floating: false,
      counter: null,
      exportButton: null,
    };

    const rowIdOf = (row) => String(opts.rowId(row));
    const idOf = (data) => (data && data[DETAIL] ? `${data[DETAIL]}::detail` : rowIdOf(data));

    function buildRowData() {
      const out = [];
      for (const row of ctl.rows) {
        out.push(row);
        const id = rowIdOf(row);
        if (opts.detail && ctl.expanded.has(id)) out.push(Object.assign({}, row, { [DETAIL]: id }));
      }
      return out;
    }

    function toggle(id) {
      if (ctl.expanded.has(id)) ctl.expanded.delete(id);
      else ctl.expanded.add(id);
      ctl.api.setGridOption("rowData", buildRowData());
      const node = ctl.api.getRowNode(id);
      if (node) ctl.api.refreshCells({ rowNodes: [node], columns: ["__expand"], force: true });
    }

    // Pinned and detail rows never show per-row widgets (chips, buttons).
    function guardRenderer(renderer) {
      if (typeof renderer !== "function") return renderer;
      return (params) => {
        if (params.node && params.node.rowPinned) return params.valueFormatted != null ? params.valueFormatted : params.value != null ? String(params.value) : "";
        return renderer(params);
      };
    }

    function columns() {
      const defs = opts.columnDefs.map((def) => Object.assign({}, def, {
        cellRenderer: guardRenderer(def.cellRenderer),
        // The pinned totals row only carries the fields it sets; derived values stay blank.
        valueGetter: typeof def.valueGetter === "function"
          ? (p) => (p.node && p.node.rowPinned ? (def.field && p.data ? p.data[def.field] : "") : def.valueGetter(p))
          : def.valueGetter,
        floatingFilter: def.filter === false ? false : ctl.floating,
      }));
      if (opts.detail) {
        defs.unshift({
          colId: "__expand",
          headerName: "",
          width: 46,
          minWidth: 46,
          maxWidth: 46,
          pinned: "left",
          sortable: false,
          filter: false,
          resizable: false,
          suppressMovable: true,
          suppressHeaderMenuButton: true,
          cellClass: "expand-cell",
          cellRenderer: (params) => {
            if (!params.data || params.node.rowPinned) return "";
            const id = rowIdOf(params.data);
            const open = ctl.expanded.has(id);
            return el("button", {
              type: "button",
              className: "expand-button",
              "aria-expanded": open ? "true" : "false",
              "aria-label": open ? "Hide details" : "Show details",
              title: open ? "Hide details" : "Show details",
              text: open ? "▾" : "▸",
              onclick: (event) => { event.stopPropagation(); toggle(id); },
            });
          },
        });
      }
      return defs;
    }

    function updateCounter() {
      if (!ctl.counter) return;
      let shown = 0;
      ctl.api.forEachNodeAfterFilter((n) => { if (n.data && !n.data[DETAIL]) shown += 1; });
      const selected = ctl.api.getSelectedRows().length;
      ctl.counter.textContent = shown === ctl.rows.length ? `${ctl.rows.length} row${ctl.rows.length === 1 ? "" : "s"}` : `${shown} of ${ctl.rows.length} rows`;
      if (ctl.exportButton) ctl.exportButton.textContent = selected ? `Export ${selected} selected` : "Export CSV";
    }

    function exportCsv() {
      const selected = ctl.api.getSelectedRows().length > 0;
      const keys = ctl.api.getColumns()
        .filter((c) => !c.getColId().startsWith("__") && !c.getColId().startsWith("ag-Grid") && !(opts.noExport || []).includes(c.getColId()))
        .map((c) => c.getColId());
      const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
      ctl.api.exportDataAsCsv({
        fileName: `${opts.exportName || "sara"}-${stamp}.csv`,
        onlySelected: selected,
        columnKeys: keys,
        skipPinnedBottom: true,
        shouldRowBeSkipped: (p) => Boolean(p.node.data && p.node.data[DETAIL]),
        processCellCallback: (p) => {
          const custom = (opts.exportValues || {})[p.column.getColId()];
          if (typeof custom === "function") return custom(p.node.data);
          return p.formatValue ? p.formatValue(p.value) : p.value;
        },
      });
    }

    // --- toolbar ---------------------------------------------------------------------
    let toolbar = null;
    if (opts.toolbar) {
      const left = el("div", { className: "gt-left" });
      const right = el("div", { className: "gt-right" });
      if (opts.search !== false) {
        const search = el("input", { type: "search", className: "gt-search", placeholder: opts.search, "aria-label": opts.search });
        search.addEventListener("input", () => ctl.api.setGridOption("quickFilterText", search.value));
        ctl.search = search;
        left.append(search);
      }
      if (opts.segments) {
        const group = el("div", { className: "segmented gt-segments", role: "group", "aria-label": opts.segments.label || "Filter rows" });
        const all = [{ id: "all", label: "All" }].concat(opts.segments.options);
        for (const option of all) {
          const button = el("button", {
            type: "button",
            text: option.label,
            dataset: { segment: option.id },
            "aria-pressed": option.id === ctl.segment ? "true" : "false",
            onclick: () => {
              ctl.segment = option.id;
              for (const b of group.children) b.setAttribute("aria-pressed", b === button ? "true" : "false");
              ctl.api.onFilterChanged();
            },
          });
          group.append(button);
        }
        left.append(group);
      }
      ctl.counter = el("span", { className: "gt-count", "aria-live": "polite" });
      const filtersButton = el("button", {
        type: "button",
        className: "btn-sm ghost",
        text: "Column filters",
        "aria-pressed": "false",
        onclick: () => {
          ctl.floating = !ctl.floating;
          filtersButton.setAttribute("aria-pressed", String(ctl.floating));
          ctl.api.setGridOption("columnDefs", columns());
          if (ctl.fit) requestAnimationFrame(ctl.fit);
        },
      });
      const resetButton = el("button", {
        type: "button",
        className: "btn-sm ghost",
        text: "Reset",
        title: "Clear search, filters, selection, and sorting",
        onclick: () => {
          if (ctl.search) ctl.search.value = "";
          ctl.api.setGridOption("quickFilterText", "");
          ctl.api.setFilterModel(null);
          ctl.api.deselectAll();
          ctl.api.resetColumnState();
          if (opts.segments) {
            ctl.segment = "all";
            ctl.setSegment("all");
          }
        },
      });
      ctl.exportButton = el("button", { type: "button", className: "btn-sm", text: "Export CSV", onclick: exportCsv });
      right.append(ctl.counter, filtersButton, resetButton, ctl.exportButton);
      toolbar = el("div", { className: "grid-toolbar" }, [left, right]);
      node.parentNode.insertBefore(toolbar, node);
    }

    const segmentTest = () => {
      if (!opts.segments || ctl.segment === "all") return null;
      const option = opts.segments.options.find((o) => o.id === ctl.segment);
      return option ? option.test : null;
    };

    const rowSelection = opts.selection === "multi"
      ? { mode: "multiRow", checkboxes: true, headerCheckbox: true, enableClickSelection: false, isRowSelectable: (n) => Boolean(n.data && !n.data[DETAIL]) }
      : opts.selection === "single"
        ? { mode: "singleRow", checkboxes: false, enableClickSelection: true, isRowSelectable: (n) => Boolean(n.data && !n.data[DETAIL]) }
        : undefined;

    const gridOptions = Object.assign({
      theme: theme(),
      defaultColDef: { sortable: true, resizable: true, filter: true, minWidth: 90, flex: 1 },
      rowData: [],
      columnDefs: columns(),
      getRowId: (p) => idOf(p.data),
      rowSelection,
      selectionColumnDef: { pinned: "left", width: 46, minWidth: 46, maxWidth: 46, suppressHeaderMenuButton: true },
      animateRows: true,
      tooltipShowDelay: 300,
      enableCellTextSelection: true,
      pagination: opts.pageSize > 0,
      paginationPageSize: opts.pageSize || 25,
      paginationPageSizeSelector: opts.pageSize > 0 ? [10, 25, 50, 100] : undefined,
      overlayNoRowsTemplate: `<span class="grid-empty">${(opts.noRows || "No rows yet.").replace(/[<>&]/g, "")}</span>`,
      isFullWidthRow: (p) => Boolean(p.rowNode.data && p.rowNode.data[DETAIL]),
      fullWidthCellRenderer: (p) => {
        const box = el("div", { className: "detail-row" });
        try { box.append(opts.detail(p.data)); } catch (_) { box.append(el("p", { className: "empty", text: "Details unavailable." })); }
        return box;
      },
      getRowHeight: (p) => (p.data && p.data[DETAIL] ? opts.detailHeight : undefined),
      postSortRows: (params) => {
        if (!opts.detail || !ctl.expanded.size) return;
        const nodes = params.nodes;
        const details = new Map();
        const rest = [];
        for (const n of nodes) {
          if (n.data && n.data[DETAIL]) details.set(n.data[DETAIL], n);
          else rest.push(n);
        }
        nodes.length = 0;
        for (const n of rest) {
          nodes.push(n);
          const d = n.data ? details.get(rowIdOf(n.data)) : null;
          if (d) nodes.push(d);
        }
      },
      isExternalFilterPresent: () => Boolean(segmentTest()),
      doesExternalFilterPass: (n) => {
        const test = segmentTest();
        return !test || Boolean(n.data && test(n.data));
      },
      onRowClicked: (event) => {
        if (!event.data || event.data[DETAIL] || event.node.rowPinned) return;
        if (opts.onRowClicked) opts.onRowClicked(event.data, event);
      },
      onModelUpdated: updateCounter,
      onSelectionChanged: updateCounter,
      onFilterChanged: updateCounter,
    }, opts.gridOptions || {});

    // autoHeight: size the container to the rows on the current page (detail rows included),
    // instead of AG Grid's autoHeight layout, which keeps a ~150px minimum body and leaves an
    // empty block above the totals row when there are only a few rows.
    const ROW = 40;
    const fit = () => {
      if (!opts.autoHeight || !ctl.api) return;
      const api = ctl.api;
      const count = api.getDisplayedRowCount();
      const size = opts.pageSize > 0 ? api.paginationGetPageSize() : count;
      const first = opts.pageSize > 0 ? api.paginationGetCurrentPage() * size : 0;
      let rows = 0;
      for (let i = first; i < Math.min(count, first + size); i += 1) {
        const n = api.getDisplayedRowAtIndex(i);
        rows += (n && n.rowHeight) || ROW;
      }
      const body = Math.max(rows, ROW * 3); // room for the "no rows" message
      const height = ROW /* header */ + (ctl.floating ? ROW : 0) + body
        + (opts.totals && ctl.rows.length ? ROW : 0) + (opts.pageSize > 0 ? 50 : 0) + 4;
      node.style.height = `${height}px`;
    };
    if (opts.autoHeight) {
      for (const evt of ["onModelUpdated", "onPaginationChanged"]) {
        const prior = gridOptions[evt];
        gridOptions[evt] = (e) => { if (prior) prior(e); requestAnimationFrame(fit); };
      }
    }
    ctl.fit = fit;
    ctl.api = window.agGrid.createGrid(node, gridOptions);

    ctl.setRows = (rows) => {
      ctl.rows = Array.isArray(rows) ? rows : [];
      const ids = new Set(ctl.rows.map(rowIdOf));
      for (const id of [...ctl.expanded]) if (!ids.has(id)) ctl.expanded.delete(id);
      ctl.api.setGridOption("rowData", buildRowData());
      if (opts.totals) ctl.api.setGridOption("pinnedBottomRowData", ctl.rows.length ? [opts.totals(ctl.rows)] : []);
      if (opts.flashNew) {
        if (ctl.seen) {
          const fresh = [];
          for (const id of ids) {
            if (!ctl.seen.has(id)) {
              const n = ctl.api.getRowNode(id);
              if (n) fresh.push(n);
            }
          }
          if (fresh.length && fresh.length < 50) ctl.api.flashCells({ rowNodes: fresh, flashDuration: 900, fadeDuration: 900 });
        }
        ctl.seen = ids;
      }
      updateCounter();
    };
    ctl.toggle = toggle;
    // Select a segment filter programmatically (used by KPI-card drill-down).
    ctl.setSegment = (id) => {
      if (!opts.segments) return;
      ctl.segment = id;
      const group = toolbar && toolbar.querySelector(".gt-segments");
      if (group) for (const b of group.children) b.setAttribute("aria-pressed", b.dataset.segment === id ? "true" : "false");
      ctl.api.onFilterChanged();
    };
    ctl.expand = (id) => { if (!ctl.expanded.has(String(id))) toggle(String(id)); };
    ctl.setQuickFilter = (text) => {
      if (ctl.search) ctl.search.value = text;
      ctl.api.setGridOption("quickFilterText", text);
    };
    ctl.toolbar = toolbar;
    return ctl;
  }

  // A small "copy" button for IDs (deposit, PayPal order, capture, refund).
  function copyButton(value, label) {
    const button = el("button", {
      type: "button",
      className: "copy-button",
      title: `Copy ${label || "value"}`,
      "aria-label": `Copy ${label || "value"}`,
      text: "⧉",
      onclick: async (event) => {
        event.stopPropagation();
        let ok = false;
        try { await navigator.clipboard.writeText(String(value)); ok = true; } catch (_) { ok = false; }
        button.textContent = ok ? "✓" : "!";
        button.dataset.state = ok ? "copied" : "failed";
        button.title = ok ? "Copied" : "Copy failed: select the text instead";
        setTimeout(() => { button.textContent = "⧉"; delete button.dataset.state; button.title = `Copy ${label || "value"}`; }, 1400);
      },
    });
    return button;
  }

  // A labelled key/value list used inside detail rows and inspectors. Monospace values (IDs)
  // get a copy button.
  function facts(pairs) {
    const list = el("dl", { className: "detail-facts" });
    for (const [label, value, mono] of pairs) {
      if (value === null || value === undefined || value === "") continue;
      list.append(el("dt", { text: label }));
      if (value instanceof Node) list.append(el("dd", null, [value]));
      else if (mono) list.append(el("dd", { className: "mono copyable" }, [el("span", { text: value }), copyButton(String(value).split(" · ")[0], label)]));
      else list.append(el("dd", { text: value }));
    }
    return list;
  }

  window.CTGrid = { available, create, facts, theme, el, copyButton };
})();
