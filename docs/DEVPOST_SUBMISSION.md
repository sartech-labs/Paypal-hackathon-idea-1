# Devpost submission draft

Draft text for the PayPal AI Hackathon 2026 project page. Replace every `[TODO: ...]` before submitting, and only keep claims the team has verified on a live run.

## Project name

CrawlerTrust

## Tagline

Refundable PayPal deposits that keep AI crawlers accountable: good agents crawl free, agents that break the rules pay.

## Inspiration

AI agents and crawlers now read the web at machine speed. Publishers express their rules in `robots.txt`, but it is only a request: nothing stops a scraper from ignoring it, and blocking bots wholesale also shuts out the well-behaved agents publishers would happily serve. We wanted a way for a website to trust an AI agent it has never met, without blocking it.

## What it does

CrawlerTrust is an HTTP `402 Payment Required` gateway in front of a website.

- An AI crawler that wants protected pages gets a 402 challenge. It pays a small refundable deposit through PayPal and explicitly accepts the site's machine-readable crawling policy.
- Every request is checked against the policy: allowed and disallowed paths, encoded path traversal, a sliding-window rate limit, and a per-session request budget.
- When the session ends, the gateway decides from its own records. A compliant crawler gets its deposit back; one that broke the rules forfeits it.
- **Claude as the crawler:** our AI agent reads the challenge and policy, decides whether to pay, pays with PayPal, paces itself from the gateway's rate-limit signals, ignores a prompt injection hidden in a page, and completes its session. A rogue persona shows what happens to an agent that ignores the rules.
- **Claude as the explainer:** every refund or forfeiture comes with a plain-English explanation in which each statement cites the audit event behind it.
- **Claude as the policy assistant:** publishers describe their terms in plain English and get a validated, machine-readable policy draft.
- A publisher console shows live sessions, deposits, violations, and explanations.

## How we built it

- **Gateway:** Python, FastAPI, and HTTPX, with a pure policy engine, a sliding-window rate limiter, hashed bearer tokens, and an append-only audit log.
- **PayPal:** REST APIs directly: OAuth 2.0; Orders v2 to create, capture, authorize, and look up deposit orders; Payments v2 for refunds, refund and capture lookups, and authorization void and capture. Two settlement modes: capture-then-refund, and an authorization hold that is voided for compliant crawlers so no money moves at all. Every call uses idempotent `PayPal-Request-Id`s, and ambiguous outcomes are reconciled against PayPal's records before any retry.
- **AI:** the Anthropic Python SDK. The agent is a manual tool-use loop over strict tool schemas; explanations and policy drafts use structured outputs and are validated in code. Refusals are handled, with the server-side refusal fallback enabled.
- **Console:** static HTML and JavaScript with AG Grid Community, served by the gateway.
- **Deployment:** Docker and a Render Blueprint.
- **Tests:** 170 automated tests with mocked PayPal transports and scripted model fakes.

## Challenges we ran into

- Making refunds safe under timeouts and crashes: a refund that times out may still have happened, so we never retry blindly; we reconcile with PayPal first.
- Keeping the money decision out of the model. The AI explains and assists, but the refund or forfeiture is always decided by deterministic rules from the audit log.
- Treating web content as hostile. Pages can contain instructions aimed at AI agents, so page text reaches the agent marked as untrusted, the session token is never shown to the model, and spending limits are enforced in code.
- Encoded path traversal (`%2e%2e`, `%252e`, `%2f`) has to be caught on the raw request path, before routing normalizes it.

## Accomplishments that we're proud of

- A complete, working loop from HTTP 402 to PayPal deposit to policy enforcement to automatic refund or forfeiture.
- An AI agent that pays for access with PayPal and behaves well because the incentives are right, not because it is trusted.
- Explanations that can be checked: every AI statement links to the audit event it relies on.
- [TODO: add live Sandbox results, for example "Verified end to end against the PayPal Sandbox in both settlement modes", once done.]

## What we learned

- `HTTP 402` is a natural fit for agent commerce, and a refundable deposit changes the incentive from "pay to read" to "behave to get your money back".
- PayPal authorization holds model a deposit better than capture-and-refund: compliant agents never have funds captured.
- Strict tool schemas, structured outputs, and code-side validation make model output dependable enough to build on.

## What's next for CrawlerTrust

- One-time buyer consent with a saved PayPal payment method and a spending cap, so agents pay autonomously.
- Reputation-based deposits that fall as an agent builds a compliance history.
- An optional pay-per-page fee captured from the hold, so publishers also earn from good crawlers.
- Multi-instance storage, PayPal webhooks, and cryptographic agent identity (HTTP Message Signatures).

## Built with

python, fastapi, httpx, pydantic, paypal-rest-api, paypal-orders-v2, paypal-payments-v2, anthropic, claude, ag-grid, docker, render

## Links

- Repository: `https://github.com/sartech-labs/Paypal-hackathon-idea-1` [TODO: make public and set the default branch before submitting]
- Demo video: [TODO: public YouTube link, under 3 minutes]
- Live demo: [TODO: Render URL]

## Submission checklist

- [ ] Repository is public and the default branch contains this code and the MIT license
- [ ] Demo video is public on YouTube and under 3 minutes ([`DEMO_SCRIPT.md`](DEMO_SCRIPT.md))
- [ ] Testing instructions: point judges to README "Quick start" (mock mode needs no credentials) and the live URL
- [ ] Live demo deployed and warmed up before judging (free Render instances sleep when idle)
- [ ] Prize categories selected: overall, Best Use of PayPal + AI, Best Use of Agentic Commerce, Best Demo Delivery, Most Impactful, plus sponsor categories that apply (AG Grid, Render) [TODO: confirm eligibility rules for multiple prizes]
- [ ] Every `[TODO]` in this file resolved
