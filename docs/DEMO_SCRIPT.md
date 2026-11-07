# Demo video script (under 3 minutes)

The Devpost submission needs a public YouTube video shorter than 3 minutes. The whole demo runs in the browser: the live demo page at `/` and the publisher console at `/console`. No terminal is needed on camera.

**The submitted video** is built by [`scripts/explainer_video`](../scripts/explainer_video/README.md): a 2 min 58 s motion-graphic explainer with real screen recordings from the live PayPal Sandbox, word-synced captions, and OpenAI `gpt-4o-mini-tts` narration. It is website-first:

| Time | Section | On screen |
| :--- | :--- | :--- |
| 0:00 | AI agents are your website's new visitors | Motion graphics |
| 0:07 | The website's problems: agents take but don't pay (Cloudflare data), ignore the rules, and good agents can't pay; today it is block them all or get scraped | Motion graphics |
| 0:39 | SARA, a gateway in front of the site: rules and prices, 402, refundable PayPal deposit, checks on every request, refund or keep | Gateway diagram; honest vs rogue |
| 1:05 | Live: a rogue scraper hits the gateway (402, deposit, 403 and 429, deposit kept, AI explanation) | Recording of `/crawler` |
| 1:20 | Selling to agents: pay-per-access, subscriptions, products | Offer cards |
| 1:27 | The owner's dashboard: deposits, security log, AG Grid tables, revenue by payment type, Verify with PayPal (MCP), AG Studio | Recording of `/console` |
| 1:57 | A person using an agent: task and budget, deposit, mandate approvals, AI purchase review, deposit back; any AI assistant via the MCP storefront | Recording of `/`; real MCP session |
| 2:32 | Why it is good for PayPal: every agent payment on PayPal, a package for websites, new merchants and volume | Motion graphics |
| 2:45 | One gateway, three winners; end card | Motion graphics |

The older automated recorder, `scripts/demo_video/make_video.sh`, records Storyboard B below as a plain screen recording. The storyboards below are for recording by hand.

## Storyboard A: the agentic era (Agent studio, recommended)

This cut tells the bigger story: one agent, four ways to pay, a person in control, and a site any AI assistant can shop. It needs an AI key and `DEMO_PUBLIC=true`. In mock payment mode the subscription activates at once; in the Sandbox PayPal asks the payer to approve it.

| Time | Screen | Voiceover |
| :--- | :--- | :--- |
| 0:00–0:15 | `/` (Agent studio), hero and the "What Sentinel News sells to agents" strip | "AI agents are becoming customers. They read, subscribe, and buy for people. Most websites can only block them or get scraped." |
| 0:15–0:30 | Point at the four offer cards | "SARA gets a site ready for them, backed by PayPal: a machine-readable catalog with four ways to pay: a refundable crawl deposit, pay-per-access, subscriptions, and products." |
| 0:30–0:45 | The request box (first preset) and the mandate: $60 budget, ask above $10 | "This is Aria, a personal agent. I give it a task and a spending mandate. The mandate is enforced in code, not by the model." |
| 0:45–1:25 | **Run agent**. Activity fills: catalog, mandate, 402, deposit paid, video bought ($2) | "It reads the catalog, plans, and pays. A one-dollar refundable deposit to crawl the news. Two dollars for the video, under my threshold, so it doesn't ask." |
| 1:25–1:50 | Approval card for Sentinel Pro $9.99/month. **Approve**. | "A subscription is a recurring charge, so it always asks me. I approve; PayPal Subscriptions starts the plan, and the members feed unlocks." |
| 1:50–2:05 | Approval card for the $12 e-book. **Decline**. | "Twelve dollars is above my limit. I decline, and the agent accepts it. It doesn't retry or work around me." |
| (optional, replaces 2:30–2:50) | Preset "Members feed (has a trap)" | "This feed hides an instruction: buy five printed reports, the user pre-approved it. Aria refuses and says so; and if an agent did try, an independent AI review that never sees the page would block it." |
| 2:05–2:30 | Report and the spending panel: deposit refunded, video paid, subscription active, e-book declined, net $11.99 | "The crawl session ends cleanly, so the deposit comes back. The report lists every payment with its PayPal reference." |
| 2:30–2:50 | `/console#commerce`: revenue by type, orders and subscriptions grids | "The publisher sees it all: pay-per-access, products, subscription revenue, and the deposits it kept from agents that broke the rules." |
| 2:50–3:00 | Closing card | "SARA, backed by PayPal: websites ready for the agentic era." |

## Storyboard B: the crawler deposit in depth

The original cut below focuses on the refundable deposit, policy enforcement, and PayPal MCP verification.

## Before recording

1. **Credentials:** `.env` has `PAYMENT_MODE=paypal_sandbox`, `PAYPAL_SANDBOX_AUTOPAY=true`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`), `DEMO_PUBLIC=true`, and `DASHBOARD_PUBLIC=true`. With autopay, deposits are real Sandbox payments made with PayPal's Sandbox test card, so there is no PayPal login on camera.
2. **PayPal MCP server:** use the Docker image (Node.js and `@paypal/mcp` included), or install Node.js 18+ locally. Before recording, press **Verify with PayPal** once on any Sandbox deposit, so `npx` has already downloaded the package.
3. **Settlement mode:** `DEPOSIT_SETTLEMENT=refund` shows a refund in PayPal; `authorization` shows a voided hold. Both modes have been run live against the Sandbox.
4. **Fresh state:** stop the server, delete `storage/*.json` and `storage/audit.jsonl`, and restart so the console starts empty.
5. **Dry run:** run both personas with the AI driver once. If the model's behaviour differs from the plan, adjust the narration rather than the code.
6. **Fallback:** if the AI provider is slow on the day, choose the **Scripted crawler** driver (same gateway, same PayPal calls, no model) and say so on screen.
7. **Browser:** one window at 1440 px wide, zoom 100%, light theme. Close other tabs.

## Storyboard

| Time | Screen | Voiceover |
| :--- | :--- | :--- |
| 0:00–0:15 | Live demo page, empty state with the five steps | "AI crawlers read the web at machine speed. `robots.txt` is only a request, and publishers pay for every scraper that ignores it." |
| 0:15–0:25 | Point at the badges: PayPal Sandbox, settlement, AI model, PayPal MCP | "SARA is an HTTP 402 gateway. Before an AI agent can read, it posts a small refundable deposit through PayPal and accepts the site's rules." |
| 0:25–1:15 | Pick **Compliant research crawler** and **AI agent**, press **Start run**. The step rail fills in: 402 challenge, policy, PayPal deposit, crawl. Hover the prompt-injection card on `/news/economy`. | "Here GPT-5.5 is the crawler. It gets HTTP 402, reads the policy, pays the deposit with PayPal, and paces itself to the rate limit. This page hides an instruction telling AI agents to grab private customer data. The agent ignores it." |
| 1:15–1:35 | Session completes: deposit card turns **Refunded**, PayPal order, capture, and refund IDs appear. Explanation card shows findings with evidence chips. | "The session ends and the gateway decides from its own audit log: no violations, so the deposit comes back. The explanation cites the exact audit events." |
| 1:35–1:55 | Press **Verify with PayPal (MCP)**. The table shows every check matching. | "Then the publisher checks PayPal's own records, through PayPal's MCP server: order, amount, capture refunded, refund completed. Read-only, and every check matches." |
| 1:55–2:30 | Pick **Rogue scraper**, **Start run**. Red 403 and 429 rows stack up; step 4 turns red; deposit turns **Forfeited**. | "Now the same model, told to grab everything. It probes the admin area and private data, tries path traversal, and bursts requests. The gateway blocks each one, and the deposit is forfeited by policy, not by the model." |
| 2:30–2:50 | PayPal assistant: click the suggestion "Check the last 3 deposits against PayPal's records" and **Ask**. Show the answer and the tool-call list. | "The PayPal assistant answers questions with PayPal's MCP tools and shows every call it made, so the answer can be checked." |
| 2:50–3:00 | Closing card: repo URL and the one-line positioning | "Other 402 schemes charge for access. SARA charges for misbehaviour. Good agents crawl free." |

Keep the final cut under 2:55 to leave margin for YouTube's processing. If time is short, cut the PayPal assistant question (2:30–2:50) and keep **Verify with PayPal**.

## Command-line alternative

The same runs are available from a terminal, for example for a second take:

```bash
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

```bash
python -m client.agent --persona compliant --base-url http://127.0.0.1:8000
```

```bash
python -m client.agent --persona rogue --base-url http://127.0.0.1:8000
```
