# Devpost submission draft

Draft text for the PayPal AI Hackathon 2026 project page. Replace every `[TODO: ...]` before submitting, and keep only claims the team has seen work on a live run.

## Project name

SARA (Safe Access for Responsible Agents), by Sartech Labs

## Tagline

Good agents get in. Bad agents pay. A gateway website owners put in front of their site: every AI agent leaves a refundable PayPal deposit and follows the site's rules, or loses the deposit.

## Inspiration

AI agents now read and shop on websites at machine speed. Site owners publish their rules in `robots.txt`, but it is only a request: a scraper that ignores it loses nothing. Blocking every bot also shuts out the helpful agents owners want. We wanted a third option: let every agent in, and put money behind the rules.

## What it does

SARA is a gateway that sits in front of a website. The website itself does not change.

**The owner sets it up in three steps:**

1. **Add the website.** SARA reads the site's own `robots.txt` and turns its `Disallow` lines into enforced rules.
2. **Connect PayPal.** Log in with PayPal links the account that receives kept deposits.
3. **Set the deposit and rules.** Deposit amount, blocked paths, rate limit, and request budget per session.

**Every agent then goes through the gateway:**

1. An agent without a session gets `HTTP 402 Payment Required` with the deposit, the rules, and the policy version.
2. The deposit is approved with PayPal's own button and placed as a PayPal **hold**: no money moves yet.
3. Each request is checked: blocked paths and path tricks (`403`), bursts (`429`), the request budget, a reused or forged session.
4. When the session ends, deterministic code decides from the gateway's records. An agent that followed the rules has its hold **voided** and pays nothing. One that broke them has its hold **captured**, and the owner is paid.
5. An AI explanation tells both sides why, and every statement in it cites the recorded event behind it.

People browsing the site are not affected: real browsers are forwarded as normal.

**The owner console:**

- **Overview:** deposits held, kept, and returned; agents let in and blocked; recent decisions; trends, for one website or all of them.
- **Websites → Setup:** add a website, connect PayPal, set the deposit and rules.
- **Websites → Playground:** send test agents at your own site. Five AI agents with a goal (Polite researcher, Price scraper, Admin prober, Tricked by a page, Budget burner), four scripted attacks (reused session, forged identity, never pays, path trickery), or a goal you write yourself. The test agent's deposit is approved with the PayPal button, like a real one.
- **Agents:** every session, request, and violation with the rule it broke.
- **Money:** each deposit with its PayPal reference and "why this outcome", earnings paid to the connected PayPal account, and AG Grid / AG Studio analytics. "Verify with PayPal" compares any deposit with PayPal's own record through PayPal's MCP server.

**Verified agents.** Agents can sign their requests (Web Bot Auth, Ed25519). A verified agent's record sets its price: half the deposit with a clean record, three times after a deposit was kept, refused after three.

**Why a hold.** In authorization mode an honest agent's deposit is only ever a hold that is voided, so no money moves and no PayPal fee is charged. Only rule breakers are charged.

**The evidence.** One AI bot hit a single site 39,000 times a minute (Fastly); 13.3% of AI bot scrapes ignored robots.txt in Q2 2025, up from 3.3% (TollBit); bots are 51% of web traffic, 37% bad bots (Imperva 2025); AI-referred shoppers to US retail sites rose 693% in the 2025 holidays (Adobe).

**How it fits with what exists.**

| | What it does | What SARA adds |
| :--- | :--- | :--- |
| [Cloudflare Pay Per Crawl](https://siliconangle.com/2025/07/01/ai-game-changer-cloudflare-lets-websites-charge-ai-developers-content-access/) | 402 with a flat price per crawl; Cloudflare is merchant of record | Prices *behaviour* with a refundable deposit and enforces the rules on every request, on PayPal |
| [TollBit](https://pressgazette.co.uk/platforms/publishers-urged-to-embrace-future-where-bot-readers-provide-majority-of-revenue/) | Bot paywall, priced per 1,000 pages | Honest agents read for free; only rule breakers pay |
| [x402](https://www.spark.money/research/x402-protocol-ai-agent-payments) | Open 402 standard paid in stablecoins | 402 on PayPal: cards and wallets, buyer protection, refunds, disputes |
| [RSL](https://rslstandard.org/press/rsl-standard) | Licensing terms declared in `robots.txt` | The same terms, enforced with money at stake |
| [PayPal Agent Ready](https://newsroom.paypal-corp.com/2025-10-28-PayPal-Launches-Agentic-Commerce-Services-to-Power-AI-Driven-Shopping) | PayPal merchants accept payments from AI agents | Complementary: Agent Ready lets an agent pay at checkout; SARA makes every agent follow the site's rules first, and could take the deposit through Agent Ready |

**What PayPal gets.** Every deposit, void, and capture runs on PayPal. SARA brings PayPal site owners who earn nothing from agents today: publishers and content sites. At Morgan Stanley's base case of $190 billion of US agent-driven e-commerce by 2030, each 1% that runs through SARA sites on PayPal is about $1.9 billion a year (illustrative arithmetic).

## How we built it

- **Gateway:** Python, FastAPI, and HTTPX, with a pure policy engine, a sliding-window rate limiter, hashed bearer tokens, an append-only audit log, and a reverse proxy that forwards allowed requests to the owner's website. Each website has its own rules and numbers.
- **PayPal:**
  - Log in with PayPal (OpenID Connect) to connect the owner's account.
  - The JS SDK v6 `<paypal-button>` to approve deposits.
  - Orders v2 with `intent: AUTHORIZE` for the hold.
  - Payments v2 to void or capture the hold, plus refunds for the alternative capture-then-refund mode.
  - Webhooks checked with `verify-webhook-signature`.
  - PayPal's MCP server (`@paypal/mcp`, read tools only) for "Verify with PayPal" and the PayPal assistant.
  - Every call uses an idempotent `PayPal-Request-Id`, and an unclear outcome is checked against PayPal's records before any retry.
- **AI:** the OpenAI Responses API (GPT-5.5) or the Anthropic Messages API (Claude), chosen with one setting. Playground agents are tool-use loops over strict schemas. Explanations and policy drafts use structured outputs and are validated in code. Refusals are handled.
- **Console:** static HTML and JavaScript served by the gateway, with AG Grid tables and AG Studio dashboards, and a signed, HttpOnly owner session.
- **Deployment:** Docker and a Render Blueprint.
- **Tests:** 404 automated tests, all offline, with mocked PayPal transports, a fake MCP server, a fake origin website, and scripted model fakes.

## Challenges we ran into

- **Keeping the money decision out of the model.** The AI explains and tests; the void or capture is decided by deterministic rules from the audit log.
- **Treating web content as hostile.** Pages can carry instructions aimed at agents. Page text reaches agents marked as untrusted, session tokens never reach the model, and the gateway blocks a fooled agent anyway.
- **Log in with PayPal in practice.** PayPal can confirm a login before it releases the profile. The connection is kept, and the account is named once PayPal shares it.
- **Real websites redirect.** Many sites send `example.com` to `www.example.com`. SARA follows a single redirect on the same site when the website is added, so agents get pages, not redirects.
- **Encoded path tricks** (`%2e%2e`, `%252e`, `%2f`) must be caught on the raw path, before routing normalizes it.
- **Where MCP belongs.** PayPal's MCP tools act as the merchant and have no void or authorization-capture tools. The money path stays on REST calls, and MCP powers read-only verification.

## Accomplishments that we're proud of

- **The whole loop runs live on PayPal:** 402, then a PayPal hold approved with PayPal's button, then enforcement, then a voided hold for the compliant agent and a captured hold for the rule breaker.
- **Log in with PayPal** connects the owner's payout account from the console.
- **Checkable explanations:** every statement the AI makes cites an audit event.
- **A playground where owners attack their own site** with real AI agents and see each decision as it happens.

## What we learned

- `HTTP 402` with a refundable deposit changes the incentive from "pay to read" to "behave and pay nothing".
- PayPal authorization holds fit a deposit better than capture-and-refund: honest agents are never charged and carry no fee.
- Strict tool schemas, structured outputs, and validation in code make model output dependable enough to build on.

## What's next for SARA

- **Domain verification and a one-step DNS switch**, so any owner can put SARA in front of their own site.
- **Payouts to each owner's own PayPal account** through PayPal's partner onboarding.
- **PayPal Agent Ready** as the agent's way to pay the deposit.
- **Shared agent records**, so a rule breaker's record follows it from site to site.
- **An edge worker and CMS plugins**, and multi-instance storage (PostgreSQL, Redis).

## Built with

python, fastapi, httpx, pydantic, paypal-rest-api, paypal-orders-v2, paypal-payments-v2, log-in-with-paypal, paypal-webhooks, paypal-js-sdk-v6, paypal-mcp-server, model-context-protocol, node.js, openai, gpt-5.5, anthropic, claude, ag-grid, javascript, docker, render

## Links

- Repository: `https://github.com/sartech-labs/Paypal-hackathon-idea-1` [TODO: make public before submitting]
- Demo video: [TODO: public YouTube link, under 3 minutes]
- Live: https://sara-gateway.onrender.com

## Testing instructions (Devpost field)

Draft for the private testing-instructions field. Fill the sign-in details there only, never in this repository.

1. Open https://sara-gateway.onrender.com (allow about a minute for it to wake) and sign in with `[TODO: owner email]` / `[TODO: owner password]`.
2. **Overview** shows the gateway's numbers.
3. **Websites → Setup** shows the protected website, the connected PayPal account, the deposit, and the rules.
4. **Websites → Playground:** choose **Price scraper** and run it. Approve the deposit with the PayPal button using `[TODO: Sandbox buyer login]`. The scraper is blocked, its deposit is kept, and the explanation says why. Then run **Polite researcher**: its hold is released.
5. **Money → Deposits:** open a deposit to see its PayPal reference, "why this outcome", and **Verify with PayPal**.

To run it locally with no credentials, follow the README's "Run it yourself".

## Submission checklist

- [ ] Repository is public and the default branch contains this code and the MIT license
- [ ] Demo video is public on YouTube and under 3 minutes ([`DEMO_SCRIPT.md`](DEMO_SCRIPT.md))
- [ ] The live site is set up again after the last deploy (website added, PayPal connected, deposit and rules set) and opened a few minutes before the deadline
- [ ] Testing instructions filled in on Devpost
- [ ] Prize categories selected: overall, Best Use of PayPal + AI, Best Use of Agentic Commerce, Best Demo Delivery, Most Impactful, plus the sponsor categories that apply (AG Grid, Render). At most one Grand Prize and one Sponsor Prize, or one Honorable Mention and one Sponsor Prize.
- [ ] Deadline: Thursday 12 November 2026, 12:00 pm Pacific
- [ ] Every `[TODO]` in this file resolved
