// Records the AgentTrust live demo in a real browser, scene by scene.
//
// Usage: node record.js <base-url> <out-dir> <driver: ai|scripted>
// Writes <out-dir>/raw.webm, markers.json (scene start/end seconds), facts.json (what
// actually happened, read from the gateway), and card PNGs for the title and closing.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [base, outDir, driver = "ai"] = process.argv.slice(2);
if (!base || !outDir) {
  console.error("usage: node record.js <base-url> <out-dir> <ai|scripted>");
  process.exit(2);
}
fs.mkdirSync(outDir, { recursive: true });

const W = 1920;
const H = 1080;
const ZOOM = 1.25; // larger UI text in a 1080p video
const RUN_TIMEOUT_MS = 6 * 60 * 1000;

// A visible cursor: headless recordings do not draw one.
const CURSOR_SCRIPT = `
  window.addEventListener("DOMContentLoaded", () => {
    const dot = document.createElement("div");
    dot.id = "__demo_cursor";
    Object.assign(dot.style, {
      position: "fixed", left: "0px", top: "0px", width: "22px", height: "22px",
      marginLeft: "-11px", marginTop: "-11px", borderRadius: "50%", zIndex: 2147483647,
      background: "rgba(42,120,214,0.35)", border: "2px solid rgba(42,120,214,0.95)",
      pointerEvents: "none", transition: "transform 120ms ease", opacity: "0"
    });
    document.body.appendChild(dot);
    document.addEventListener("mousemove", (e) => {
      dot.style.opacity = "1";
      dot.style.left = (e.clientX / ${ZOOM}) + "px";
      dot.style.top = (e.clientY / ${ZOOM}) + "px";
    }, true);
    document.addEventListener("mousedown", () => { dot.style.transform = "scale(0.7)"; }, true);
    document.addEventListener("mouseup", () => { dot.style.transform = "scale(1)"; }, true);
  });
`;

async function main() {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: 1,
    colorScheme: "light",
    recordVideo: { dir: outDir, size: { width: W, height: H } },
  });
  await context.addInitScript(CURSOR_SCRIPT);
  const page = await context.newPage();
  const t0 = Date.now();
  const now = () => (Date.now() - t0) / 1000;
  const markers = [];
  const facts = { driver };
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));

  async function scene(name, fn) {
    const start = now();
    console.log(`[scene] ${name} start ${start.toFixed(1)}s`);
    await fn();
    const end = now();
    markers.push({ name, start, end });
    console.log(`[scene] ${name} end ${end.toFixed(1)}s (${(end - start).toFixed(1)}s)`);
  }

  async function zoomed() {
    await page.addStyleTag({ content: `html { zoom: ${ZOOM}; } .timeline { max-height: 560px; }` });
  }

  async function pointAt(selector, { click = false, steps = 25 } = {}) {
    const target = page.locator(selector).first();
    await target.scrollIntoViewIfNeeded();
    const box = await target.boundingBox();
    if (!box) throw new Error(`no box for ${selector}`);
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps });
    await page.waitForTimeout(250);
    if (click) {
      await page.mouse.down();
      await page.waitForTimeout(80);
      await page.mouse.up();
    }
  }

  async function scrollToSelector(selector, offset = 12) {
    await page.evaluate(([sel, off]) => {
      const node = document.querySelector(sel);
      if (!node) return;
      node.scrollIntoView({ block: "start" });
      window.scrollBy(0, -off);
    }, [selector, offset]);
    await page.waitForTimeout(700);
  }

  async function runCrawler(persona) {
    await scrollToSelector(".launcher", 8);
    await pointAt(`input[name=persona][value=${persona}]`, { click: true });
    await pointAt(`input[name=driver][value=${driver}]`, { click: true });
    await pointAt("#start-button", { click: true });
    await page.waitForTimeout(700);
    await scrollToSelector("#steps", 8);
    await page.mouse.move(W - 40, H - 40, { steps: 10 });
    await page.waitForFunction(
      () => /Run (completed|failed|cancelled)/.test(document.getElementById("timeline").textContent),
      null,
      { timeout: RUN_TIMEOUT_MS, polling: 500 }
    );
    await page.waitForTimeout(1800);
  }

  async function latestRun() {
    const cfg = await (await page.request.get(`${base}/demo/config`)).json();
    const runId = cfg.recent_runs[0] && cfg.recent_runs[0].run_id;
    return (await page.request.get(`${base}/demo/runs/${runId}`)).json();
  }

  function summarize(run) {
    const results = run.events.filter((e) => e.kind === "tool_result").map((e) => e.data);
    const pages = results.filter((r) => r.name === "fetch_page").map((r) => r.result);
    return {
      status: run.status,
      error: run.error,
      model: run.model,
      deposit_state: run.deposit && run.deposit.deposit_state,
      paypal_order: run.deposit && run.deposit.provider_order_id,
      violations: run.session && run.session.total_violations,
      allowed: run.session && run.session.allowed_requests,
      paths: pages.map((p) => `${p.status} ${p.path || ""}`),
      injection_seen: pages.some((p) => p.prompt_injection),
      requested_private: pages.some((p) => (p.path || "").includes("/private")),
      blocked_reasons: [...new Set(pages.filter((p) => p.blocked).map((p) => p.reason_code))],
      waited: results.some((r) => r.name === "wait"),
      tools: [...new Set(results.map((r) => r.name))],
      explanation_by: run.explanation && run.explanation.generated_by,
      usage: run.usage,
    };
  }

  // --- scenes ------------------------------------------------------------------------------
  await page.goto(`${base}/`, { waitUntil: "networkidle" });
  await zoomed();
  await page.waitForTimeout(1500);

  await scene("overview", async () => {
    await page.mouse.move(W / 2, 200, { steps: 5 });
    await pointAt("#badges .badge:nth-child(1)");
    await page.waitForTimeout(900);
    await pointAt("#badges .badge:nth-child(3)");
    await page.waitForTimeout(900);
    await pointAt("#badges .badge:nth-child(4)");
    await page.waitForTimeout(1200);
    await pointAt(".choice-row .choice:nth-child(1)");
    await page.waitForTimeout(900);
    await pointAt(".choice-row .choice:nth-child(2)");
    await page.waitForTimeout(1200);
  });

  await scene("compliant", async () => {
    await runCrawler("compliant");
  });
  facts.compliant = summarize(await latestRun());

  await scene("compliant_outcome", async () => {
    await scrollToSelector("#explain-title", 120);
    await pointAt("#explanation .findings li:nth-child(1) .evidence-chip", { click: true });
    await page.waitForTimeout(3500);
  });

  await scene("verify", async () => {
    await scrollToSelector("#paypal-panel", 10);
    await pointAt("#paypal-panel .pp-section button.primary", { click: true });
    await page.waitForSelector("#paypal-panel .pp-verdict", { timeout: 120000 });
    await page.waitForTimeout(600);
    await scrollToSelector("#paypal-panel .pp-verdict", 140);
    await page.mouse.move(W - 520, H / 2, { steps: 15 });
    await page.waitForTimeout(4000);
  });
  facts.verify = await page.evaluate(() => ({
    verdict: (document.querySelector("#paypal-panel .pp-verdict") || {}).textContent || "",
    mismatches: document.querySelectorAll("#paypal-panel .pp-checks .chip-icon[data-tone=critical]").length,
    checks: document.querySelectorAll("#paypal-panel .pp-checks tbody tr").length,
  }));

  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  await page.waitForTimeout(800);

  await scene("rogue", async () => {
    await runCrawler("rogue");
  });
  facts.rogue = summarize(await latestRun());

  await scene("rogue_outcome", async () => {
    await scrollToSelector("#deposit-title", 100);
    await page.waitForTimeout(1500);
    await scrollToSelector("#explain-title", 120);
    await page.waitForTimeout(3000);
  });

  await scene("assistant", async () => {
    await scrollToSelector("#paypal-panel .pp-ask", 60);
    await pointAt("#paypal-panel .suggestions button:nth-child(1)", { click: true });
    const ask = "#paypal-panel .pp-ask .assistant-actions button";
    const enabled = await page.locator(ask).isEnabled();
    facts.assistant = { attempted: enabled };
    if (enabled) {
      await pointAt(ask, { click: true });
      await page.waitForFunction(
        () => {
          const box = document.querySelector("#paypal-panel .pp-ask [aria-live]");
          return box && !/reading PayPal/.test(box.textContent) && box.textContent.trim().length > 0;
        },
        null,
        { timeout: 240000, polling: 500 }
      );
      await page.waitForTimeout(300);
      facts.assistant.answer = await page.evaluate(() => (document.querySelector("#paypal-panel .pp-answer") || {}).textContent || "");
      facts.assistant.calls = await page.evaluate(() => document.querySelectorAll("#paypal-panel .pp-ask .trace li").length);
      facts.assistant.error = facts.assistant.answer
        ? null
        : await page.evaluate(() => (document.querySelector("#paypal-panel .pp-ask .notice") || {}).textContent || "");
    } else {
      await page.waitForTimeout(3000);
    }
  });

  // The answer is shown in real time (the wait above may be sped up in the edit).
  await scene("assistant_answer", async () => {
    if (facts.assistant.answer) {
      await scrollToSelector("#paypal-panel .pp-ask [aria-live]", 160);
      await page.mouse.move(W - 520, H / 2, { steps: 15 });
      await page.waitForTimeout(6500);
    } else {
      await page.waitForTimeout(500);
    }
  });

  // Load the console before its scene starts, so the video never shows a blank page.
  await page.goto(`${base}/console`, { waitUntil: "networkidle" });
  await zoomed();
  await page.waitForSelector("#grid-deposits button.cell-button", { timeout: 20000 });
  await page.waitForTimeout(1500);

  await scene("console", async () => {
    await page.waitForTimeout(1200);
    await pointAt("#grid-deposits button.cell-button", { click: true });
    await page.waitForTimeout(4500);
  });

  facts.errors = errors;
  const video = page.video();
  await context.close();
  const rawPath = await video.path();
  fs.renameSync(rawPath, path.join(outDir, "raw.webm"));
  fs.writeFileSync(path.join(outDir, "markers.json"), JSON.stringify(markers, null, 2));
  fs.writeFileSync(path.join(outDir, "facts.json"), JSON.stringify(facts, null, 2));

  // Title and closing cards (separate context, not recorded).
  const cards = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
  const cardPage = await cards.newPage();
  for (const name of ["title", "closing"]) {
    await cardPage.goto(`file://${path.join(__dirname, "cards.html")}#${name}`);
    await cardPage.waitForTimeout(300);
    await cardPage.screenshot({ path: path.join(outDir, `card-${name}.png`) });
  }
  await browser.close();
  console.log("facts:", JSON.stringify(facts, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
