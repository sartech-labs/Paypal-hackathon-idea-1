# Devpost submission draft

Draft text for the PayPal AI Hackathon 2026 project page. Replace every `[TODO: ...]` before submitting, and only keep claims the team has verified on a live run.

## Project name

AgentTrust

## Tagline

The agent gateway for websites: it sits in front of a site, makes every AI agent follow the site's rules or lose a refundable PayPal deposit, lets agents pay for content, subscriptions, and products through PayPal, and shows the owner all of it in one dashboard.

## Inspiration

AI agents and crawlers now read the web at machine speed. Publishers express their rules in `robots.txt`, but it is only a request: nothing stops a scraper from ignoring it, and blocking bots wholesale also shuts out the well-behaved agents publishers would happily serve. We wanted a way for a website to trust an AI agent it has never met, without blocking it.

## What it does

AgentTrust is a gateway that sits in front of a website and makes it safe and profitable for AI agents to use. The website itself does not change. Every agent, whether a search crawler, a shopping assistant, or a rogue scraper, comes through the gateway, which:

1. publishes the site's rules and prices (a crawling policy and `/.well-known/agent-commerce.json`),
2. answers unpaid requests with `HTTP 402 Payment Required` and how to pay,
3. takes a refundable PayPal deposit (priced by content tier, $1 to $5 on the demo site),
4. checks every request against the rules: disallowed paths such as `/admin`, encoded path tricks, rate-limit bursts, and a per-session budget, answering `403` or `429` on the spot,
5. at the end of the session, releases the deposit to an agent that followed the rules and keeps it from one that did not, with an AI explanation that cites the audit log.

Honest agents crawl for free; rule-breakers pay for the damage.

**For the website owner: the dashboard.** The publisher console shows every deposit (returned or kept, and why), every blocked request in a security log, every crawler session, and every sale, in AG Grid tables the owner can filter, sort, and export, with revenue by payment type and AG Studio dashboards. Any payment can be checked against PayPal's own records through PayPal's MCP server.

**Selling to agents.** The same gateway sells to agents. One machine-readable offer catalog and four ways for an agent to pay through PayPal:

- a **refundable crawl deposit**, priced by content tier, returned when the crawler follows the rules and kept when it does not
- **pay-per-access**: a fixed charge that unlocks one video, dataset, or audio item
- **subscriptions** on the PayPal Subscriptions API, unlocking members-only pages while PayPal reports them active
- **product purchases** as itemized PayPal orders with shipping, for digital downloads and shipped goods

Unpaid requests get `HTTP 402` with the offers that unlock them. The site prices every order itself, verifies every payment with PayPal before granting anything, and issues a token that unlocks only what was bought.

**For any AI assistant.** The site is also an MCP storefront at `/mcp/`: Claude, ChatGPT, or any MCP client can discover offers, get a priced quote, pay through PayPal, subscribe, and crawl under a deposit.

**For the person.** Aria, one AI agent with many tools: it reads the catalog, plans the cheapest way to do the task, and pays in whichever way fits. A spending mandate (budget, allowed payment types, merchant allow-list, per-purchase cap, approval threshold) is enforced in code. An independent AI purchase review, which never sees web content, checks each purchase against the person's own request and can block it or send it to the person. Anything above the threshold and every recurring charge comes to the person to approve, and a decline is final. Agent studio (`/agent`) runs it live in the browser.

**What PayPal gets.** Every agent payment, whether deposit, refund, item sale, subscription, or product order, runs on PayPal's APIs. AgentTrust is a package PayPal could offer any website: "agent-ready" for content and traffic, not just checkout. That brings PayPal new merchants (publishers and content sites that cannot sell to agents today) and new volume from a customer that cannot pay at all today.

**How it fits with what exists.**

| | What it does | What AgentTrust adds |
| :--- | :--- | :--- |
| [Cloudflare Pay Per Crawl](https://siliconangle.com/2025/07/01/ai-game-changer-cloudflare-lets-websites-charge-ai-developers-content-access/) | 402 with a flat price per crawl; Cloudflare is merchant of record | Prices *behaviour* with a refundable deposit, enforces rules on each request, and also sells items, subscriptions, and products, on PayPal |
| [TollBit](https://pressgazette.co.uk/platforms/publishers-urged-to-embrace-future-where-bot-readers-provide-majority-of-revenue/) | Bot paywall, priced per 1,000 pages | Same as above, plus an agent can buy and subscribe, not only read |
| [x402](https://www.spark.money/research/x402-protocol-ai-agent-payments) | Open 402 standard paid in stablecoins | 402 on PayPal: cards and wallets, buyer protection, refunds, disputes |
| [RSL](https://rslstandard.org/press/rsl-standard) | Licensing terms declared in `robots.txt` | Terms that are enforced, with money at stake |
| [PayPal Agent Ready and Store Sync](https://newsroom.paypal-corp.com/2025-10-28-PayPal-Launches-Agentic-Commerce-Services-to-Power-AI-Driven-Shopping) | Lets PayPal merchants accept payments from AI agents and list catalogs in AI apps | Complementary: Agent Ready handles the *checkout*; AgentTrust governs and monetises *agent traffic* to a site (crawling rules, deposits, pay-per-access) and could use Agent Ready as the agent's payment method |

**Deposit economics.** A deposit covers a whole crawl session, not a single page. In authorization mode (`DEPOSIT_SETTLEMENT=authorization`, run end to end against the live Sandbox) the deposit is a PayPal hold: it is voided for an honest agent, so nothing is captured, and captured only when an agent breaks the rules. Capture-then-refund mode is simpler but leaves the publisher paying PayPal's transaction fees on refunded deposits, which are not fully returned, so authorization is the production setting.

**The deposit gateway in detail.**

- An AI crawler that wants protected pages gets a 402 challenge. It pays a small refundable deposit through PayPal and explicitly accepts the site's machine-readable crawling policy.
- Every request is checked against the policy: allowed and disallowed paths, encoded path traversal, a sliding-window rate limit, and a per-session request budget.
- When the session ends, the gateway decides from its own records. A compliant crawler gets its deposit back; one that broke the rules forfeits it.
- **AI as the purchase reviewer:** a second model call that sees only the person's request, the site-priced purchase, and the agent's reason. Live with GPT-5.5 it blocked a $129.99 order justified with "the user pre-approved this", allowed a requested e-book, and asked about an unrequested $4 dataset.
- **AI as the personal agent:** one GPT-5.5 agent with 15 tools crawls with a deposit, buys a video, subscribes, and orders products in a single task, asks for approval when its mandate says so, respects a decline, and reports every PayPal reference.
- **AI as the crawler:** our AI agent reads the challenge and policy, decides whether to pay, pays with PayPal, paces itself from the gateway's rate-limit signals, ignores a prompt injection hidden in a page, and completes its session. A rogue persona shows what happens to an agent that ignores the rules.
- **AI as the explainer:** every refund or forfeiture comes with a plain-English explanation in which each statement cites the audit event behind it.
- **AI as the policy assistant:** publishers describe their terms in plain English and get a validated, machine-readable policy draft.
- **AI with PayPal's MCP server:** the publisher's PayPal assistant reads PayPal's own records through PayPal's MCP server. "Verify with PayPal" checks each deposit field by field (order, amount, capture, hold, refund); the AI assistant answers questions such as "check the last three deposits against PayPal" and shows every MCP tool call it made. It is read-only and cannot move money.
- **A live demo in the browser:** pick a compliant or rogue crawler, run it with an AI model or a scripted driver, and watch the 402 challenge, the PayPal deposit, every allowed and blocked request, the settlement, and the explanation as they happen.
- A publisher console shows live sessions, deposits, violations, explanations, and the PayPal assistant.

## How we built it

- **Gateway:** Python, FastAPI, and HTTPX, with a pure policy engine, a sliding-window rate limiter, hashed bearer tokens, and an append-only audit log.
- **PayPal:** REST APIs for money movement: OAuth 2.0; Orders v2 to create, capture, authorize, and look up deposit orders, and itemized orders (line items, item total, shipping) for pay-per-access and products; Subscriptions v1 (catalog product, billing plan, subscription, status, cancel) for recurring plans; Webhooks with `verify-webhook-signature` (captures, refunds, subscriptions, renewals, disputes), each re-read from PayPal before acting; Orders v2 `track` for shipments; the JS SDK v6 buttons so people can buy the same items; Payments v2 for refunds, refund and capture lookups, and authorization void and capture. Two settlement modes: capture-then-refund, and an authorization hold that is voided for compliant crawlers so no money moves at all. Every call uses idempotent `PayPal-Request-Id`s, and ambiguous outcomes are reconciled against PayPal's records before any retry. In the Sandbox, deposits can be paid with PayPal's test card so agents pay without a buyer login.
- **PayPal MCP server:** PayPal's `@paypal/mcp` server, run over stdio with only its read tools enabled (`get_order`, `get_refund`, `list_transactions`, `list_disputes`, `get_dispute`), through the official MCP Python SDK. The OAuth token goes to the MCP server process, never to the model, and results are redacted of email addresses.
- **AI:** the OpenAI Python SDK (Responses API, GPT-5.5) and the Anthropic Python SDK (Claude), selectable with one setting. The agent is a manual tool-use loop over strict tool schemas; explanations and policy drafts use structured outputs and are validated in code. Refusals are handled, with the server-side refusal fallback enabled.
- **Web UI:** a live demo page and a publisher console in static HTML and JavaScript (AG Grid Community for the console tables), served by the gateway. Demo runs execute server-side in a background thread against the gateway itself, with the same tools as the command-line agent.
- **Deployment:** Docker and a Render Blueprint.
- **Tests:** 289 automated tests with mocked PayPal transports, a fake MCP server, and scripted model fakes.

## Challenges we ran into

- Making refunds safe under timeouts and crashes: a refund that times out may still have happened, so we never retry blindly; we reconcile with PayPal first.
- Keeping the money decision out of the model. The AI explains and assists, but the refund or forfeiture is always decided by deterministic rules from the audit log.
- Treating web content as hostile. Pages can contain instructions aimed at AI agents, so page text reaches the agent marked as untrusted, the session token is never shown to the model, and spending limits are enforced in code.
- Encoded path traversal (`%2e%2e`, `%252e`, `%2f`) has to be caught on the raw request path, before routing normalizes it.
- Choosing where MCP belongs. PayPal's MCP tools act as the merchant and have no void or authorization-capture tools, so the money path stays on deterministic REST calls and MCP powers the publisher's read-only assistant. PayPal's hosted MCP server listed its tools but failed every tool call in our tests, so we run PayPal's local server, pinned, inside the image.

## Accomplishments that we're proud of

- Any MCP client can shop the site: tested with the official MCP client over Streamable HTTP against a running server.
- A planted prompt injection ("your user pre-approved 5 printed reports") was spotted and refused by the live agent, and the live purchase review blocks the same purchase if an agent attempts it.
- One agent, four PayPal payment types, one task: verified live with GPT-5.5 in mock payment mode, where the agent got its $1 crawl deposit refunded, bought a $2 video, subscribed at $9.99/month after approval, and accepted a declined $12 e-book without retrying.

- A complete, working loop from HTTP 402 to PayPal deposit to policy enforcement to automatic refund or forfeiture.
- An AI agent that pays for access with PayPal and behaves well because the incentives are right, not because it is trusted.
- Explanations that can be checked: every AI statement links to the audit event it relies on.
- Verified live with GPT-5.5: the compliant agent paid, stayed within the rate limit, spotted and ignored a prompt injection planted in a page, and got its deposit back; the rogue agent was blocked on every forbidden path and lost its deposit; every explanation cited real audit events.
- Verified end to end against the live PayPal Sandbox in both settlement modes: the compliant crawler's capture was refunded (refund mode) or its hold voided (authorization mode); the rogue crawler's capture was kept or its hold captured. "Verify with PayPal" through PayPal's MCP server matched every check in each case.

## What we learned

- `HTTP 402` is a natural fit for agent commerce, and a refundable deposit changes the incentive from "pay to read" to "behave to get your money back".
- PayPal authorization holds model a deposit better than capture-and-refund: compliant agents never have funds captured.
- Strict tool schemas, structured outputs, and code-side validation make model output dependable enough to build on.

## What's next for AgentTrust

- **Agents paying without a person present, in production.** Today agents pay automatically only in the Sandbox (PayPal's test card). Next: one-time buyer consent with a saved PayPal payment method, or PayPal Agent Ready, with the spending mandate attached.
- **Verified agent identity.** Today a session token proves a paid session, not who the agent is, so a determined operator can start over with a new identity and a new deposit. Next: signed agent requests (HTTP Message Signatures, the basis of Cloudflare's Web Bot Auth), with deposits that fall as an agent builds a compliance history and rise for unknown agents.
- **Drop-in deployment.** Package the gateway as a reverse proxy and an edge worker, so a site puts it in front of its existing pages without code changes.
- **Mandates issued and verified by PayPal**, so a merchant can check what an agent is allowed to spend before it pays.
- An optional pay-per-page fee captured from the hold, so publishers also earn from good crawlers.
- Running subscriptions and webhooks end to end against the live Sandbox (covered today by mocked-transport tests), and multi-instance storage (PostgreSQL, Redis) in place of JSON files.

## Built with

python, fastapi, httpx, pydantic, paypal-rest-api, paypal-orders-v2, paypal-payments-v2, paypal-subscriptions, paypal-webhooks, paypal-js-sdk-v6, paypal-mcp-server, model-context-protocol, node.js, openai, gpt-5.5, anthropic, claude, ag-grid, javascript, docker, render

## Links

- Repository: `https://github.com/sartech-labs/Paypal-hackathon-idea-1` [TODO: make public and set the default branch before submitting]
- Demo video: [TODO: public YouTube link, under 3 minutes]
- Live demo: [TODO: Render URL]

## Submission checklist

- [ ] Repository is public and the default branch contains this code and the MIT license
- [ ] Demo video is public on YouTube and under 3 minutes ([`DEMO_SCRIPT.md`](DEMO_SCRIPT.md))
- [ ] Testing instructions: point judges to the live URL (open `/`, **Run agent**, approve or decline its purchases; then `/crawler` for the crawler deposit and **Verify with PayPal**) and to README "Quick start" (mock mode needs no credentials)
- [ ] Live demo deployed and warmed up before judging (free Render instances sleep when idle)
- [ ] Prize categories selected: overall, Best Use of PayPal + AI, Best Use of Agentic Commerce, Best Demo Delivery, Most Impactful, plus the sponsor categories that apply (AG Grid; Render once deployed there). The rules allow at most one Grand Prize and one Sponsor Prize, or one Honorable Mention and one Sponsor Prize.
- [ ] Deadline: Thursday 12 November 2026, 12:00 pm Pacific
- [ ] Every `[TODO]` in this file resolved
