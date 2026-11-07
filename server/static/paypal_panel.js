/* PayPal assistant panel (shared by the live demo and the publisher console).
   Reads PayPal's records through the gateway's PayPal MCP integration. Values from
   PayPal, crawlers, and the model are inserted with textContent, never innerHTML. */
(() => {
  "use strict";

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

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

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

  const SUGGESTIONS = [
    "Check the last 3 deposits against PayPal's records.",
    "Was the most recent refund completed in PayPal?",
    "Do we have any open PayPal disputes?",
  ];

  // Answers are plain text: a sentence, then lines starting with "- ".
  function renderAnswer(text) {
    const box = el("div", { className: "pp-answer" });
    let list = null;
    for (const raw of String(text || "").split(/\n+/)) {
      const line = raw.trim();
      if (!line) continue;
      if (/^[-*•]\s+/.test(line)) {
        if (!list) { list = el("ul"); box.append(list); }
        list.append(el("li", { text: line.replace(/^[-*•]\s+/, "") }));
      } else {
        list = null;
        box.append(el("p", { text: line }));
      }
    }
    return box;
  }

  function renderTrace(calls) {
    if (!calls || !calls.length) return null;
    return el("ol", { className: "trace", "aria-label": "Tool calls" }, calls.map((call) => {
      const source = call.source === "paypal_mcp" ? "PayPal MCP" : "Gateway";
      return el("li", null, [
        call.ok ? chip("good", "✓", source) : chip("critical", "✕", source),
        el("span", null, [
          el("span", { className: "trace-tool", text: `${call.tool}(${Object.keys(call.arguments || {}).length ? JSON.stringify(call.arguments) : ""})` }),
          el("span", { className: "hint", text: ` ${call.duration_ms} ms` }),
        ]),
        el("span", { className: "trace-summary", text: call.summary }),
        call.preview
          ? el("details", { className: "tl-more" }, [el("summary", { text: "Raw PayPal result (emails redacted)" }), el("pre", { className: "raw", text: call.preview })])
          : null,
      ]);
    }));
  }

  function create(root, options) {
    const opts = Object.assign({ headers: () => ({}), compact: false }, options || {});
    const state = { status: null, deposit: null, busy: false, lastVerifiedId: null };

    const statusRow = el("div", { className: "pp-status" });
    const verifyButton = el("button", { type: "button", className: "primary", text: "Verify deposit with PayPal" });
    const verifyHint = el("p", { className: "hint" });
    const verifyResult = el("div", { "aria-live": "polite" });
    const question = el("textarea", { id: `${root.id}-question`, rows: "2", maxlength: "600" });
    question.value = SUGGESTIONS[0];
    const askButton = el("button", { type: "button", text: "Ask the PayPal assistant" });
    const askHint = el("span", { className: "hint" });
    const askResult = el("div", { "aria-live": "polite" });

    clear(root);
    root.append(
      el("div", { className: "card-head" }, [
        el("div", null, [
          el("h2", { id: "paypal-title", text: "PayPal assistant" }),
          el("p", { className: "card-sub", text: "Reads PayPal's own records through PayPal's MCP server. Read-only: it cannot refund, capture, or change anything." }),
        ]),
      ]),
      statusRow,
      el("div", { className: "pp-section" }, [
        el("h3", { text: "Verify with PayPal" }),
        el("p", { className: "hint", text: "Compares the gateway's deposit record with the PayPal order and refund, field by field. No AI involved." }),
        el("div", { className: "assistant-actions" }, [verifyButton]),
        verifyHint,
        verifyResult,
      ]),
      el("div", { className: "pp-section pp-ask" }, [
        el("h3", null, [el("label", { for: question.id, text: "Ask about your PayPal records" })]),
        question,
        el("div", { className: "suggestions" }, SUGGESTIONS.map((text) =>
          el("button", { type: "button", text, onclick: () => { question.value = text; question.focus(); } })
        )),
        el("div", { className: "assistant-actions" }, [askButton, askHint]),
        askResult,
      ])
    );

    async function request(url, init) {
      const response = await fetch(url, Object.assign({}, init, {
        headers: Object.assign({ Accept: "application/json" }, opts.headers(), (init && init.headers) || {}),
      }));
      let body = null;
      try { body = await response.json(); } catch (_) { body = null; }
      return { ok: response.ok, status: response.status, body };
    }

    function message(body, status) {
      return (body && (body.message || (body.detail && body.detail.message))) || `Request failed (HTTP ${status}).`;
    }

    function update() {
      const s = state.status;
      clear(statusRow);
      if (!s) {
        statusRow.append(badge("neutral", "Checking PayPal MCP..."));
      } else {
        statusRow.append(
          badge(s.mcp_configured ? "good" : "neutral", s.mcp_configured ? `MCP: ${s.mcp_transport === "hosted" ? "PayPal hosted server" : "@paypal/mcp (local)"}` : "PayPal MCP not configured"),
          badge("neutral", `${s.read_only_tools.length} read-only tools`),
          badge(s.ai_available ? "good" : "neutral", s.ai_available ? `AI: ${providerName(s.ai_provider)} (${s.ai_model})` : "AI: not configured")
        );
      }

      const d = state.deposit;
      const ready = s && s.mcp_configured && !state.busy;
      if (!s) {
        verifyButton.disabled = true;
        verifyHint.textContent = "";
      } else if (!s.mcp_configured) {
        verifyButton.disabled = true;
        verifyHint.textContent = s.mcp_unavailable_reason || "Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET on the gateway.";
      } else if (!d) {
        verifyButton.disabled = true;
        verifyHint.textContent = opts.compact ? "Select a deposit to verify it." : "Run a crawler first; its deposit appears here.";
      } else if (d.payment_mode === "mock") {
        verifyButton.disabled = true;
        verifyHint.textContent = "This deposit used simulated payments, so PayPal has no record of it.";
      } else if (!d.provider_order_id && !d.paypal_order) {
        verifyButton.disabled = true;
        verifyHint.textContent = "This deposit has no PayPal order yet.";
      } else {
        verifyButton.disabled = !ready;
        verifyHint.textContent = `Deposit ${d.deposit_id}`;
      }
      verifyButton.textContent = state.busy === "verify" ? "Verifying with PayPal..." : "Verify deposit with PayPal";

      askButton.disabled = !(ready && s.ai_available);
      askButton.textContent = state.busy === "ask" ? "Asking PayPal..." : `Ask ${s && s.ai_available ? providerName(s.ai_provider) : "the assistant"}`;
      askHint.textContent = !s ? "" : !s.ai_available ? "Needs an AI key on the gateway (OPENAI_API_KEY or ANTHROPIC_API_KEY)." : (s.questions_remaining_this_hour !== null && s.questions_remaining_this_hour !== undefined ? `${s.questions_remaining_this_hour} public requests left this hour` : "");
    }

    async function loadStatus() {
      try {
        const result = await request("/paypal/assistant/status");
        state.status = result.ok ? result.body : null;
        if (!result.ok) {
          clear(statusRow);
          statusRow.append(badge("neutral", message(result.body, result.status)));
          verifyButton.disabled = true;
          askButton.disabled = true;
          return;
        }
      } catch (_) {
        state.status = null;
      }
      update();
    }

    async function verify() {
      const d = state.deposit;
      if (!d) return;
      state.busy = "verify";
      update();
      clear(verifyResult);
      verifyResult.append(el("p", { className: "notice", text: "Starting PayPal's MCP server and reading the order..." }));
      try {
        const result = await request(`/paypal/deposits/${encodeURIComponent(d.deposit_id)}/verify`, { method: "POST" });
        clear(verifyResult);
        if (!result.ok) {
          verifyResult.append(el("p", { className: "notice", text: message(result.body, result.status) }));
          return;
        }
        const v = result.body;
        state.lastVerifiedId = d.deposit_id;
        verifyResult.append(
          el("p", { className: "pp-verdict" }, [
            v.consistent ? chip("good", "✓", "Matches PayPal") : chip("critical", "✕", "Mismatch"),
            el("span", { text: ` ${v.verdict}` }),
          ]),
          el("table", { className: "pp-checks" }, [
            el("thead", null, [el("tr", null, [el("th", { text: "Check" }), el("th", { text: "Gateway" }), el("th", { text: "PayPal (via MCP)" }), el("th", { text: "" })])]),
            el("tbody", null, v.checks.map((c) => el("tr", null, [
              el("td", { text: c.label }),
              el("td", { className: "mono", text: c.agenttrust }),
              el("td", { className: "mono", text: c.paypal }),
              el("td", null, [c.status === "match" ? chip("good", "✓", "Match") : c.status === "mismatch" ? chip("critical", "✕", "Differs") : chip("neutral", "i", "Info")]),
            ]))),
          ]),
          el("p", { className: "notice", text: `${v.mcp_server} · checked ${new Date(v.checked_at).toLocaleTimeString()}` }),
          renderTrace(v.calls)
        );
      } catch (_) {
        clear(verifyResult);
        verifyResult.append(el("p", { className: "notice", text: "Could not reach the gateway." }));
      } finally {
        state.busy = false;
        update();
      }
    }

    async function ask() {
      const text = question.value.trim();
      clear(askResult);
      if (text.length < 3) {
        askResult.append(el("p", { className: "notice", text: "Type a question first." }));
        return;
      }
      state.busy = "ask";
      update();
      askResult.append(el("p", { className: "notice", text: `${providerName(state.status && state.status.ai_provider)} is reading PayPal through MCP...` }));
      try {
        const result = await request("/paypal/assistant/ask", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: text }),
        });
        clear(askResult);
        if (!result.ok) {
          askResult.append(el("p", { className: "notice", text: message(result.body, result.status) }));
          return;
        }
        const a = result.body;
        askResult.append(
          renderAnswer(a.answer),
          el("p", { className: "notice", text: `${providerName(a.generated_by)} (${a.model}) · ${a.calls.length} tool call(s) · ${a.notice}` }),
          renderTrace(a.calls)
        );
      } catch (_) {
        clear(askResult);
        askResult.append(el("p", { className: "notice", text: "Could not reach the gateway." }));
      } finally {
        state.busy = false;
        loadStatus();
      }
    }

    verifyButton.addEventListener("click", verify);
    askButton.addEventListener("click", ask);
    update();
    loadStatus();

    return {
      setDeposit(deposit) {
        const changed = (deposit && deposit.deposit_id) !== (state.deposit && state.deposit.deposit_id);
        state.deposit = deposit || null;
        if (changed) clear(verifyResult);
        update();
      },
      refresh: loadStatus,
      verify,
    };
  }

  window.CTPayPal = { create };
})();
