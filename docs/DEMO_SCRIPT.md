# Demo video script (under 3 minutes)

The Devpost submission needs a public YouTube video shorter than 3 minutes. This script shows the full story: the problem, an AI agent that behaves, an AI agent that does not, real PayPal Sandbox money movement, and the AI explanation.

## Before recording

1. **Credentials:** `.env` has `PAYMENT_MODE=paypal_sandbox`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ANTHROPIC_API_KEY`, and an `ADMIN_API_TOKEN`. Keep `DASHBOARD_PUBLIC=true` so the console loads without typing the token on camera.
2. **Settlement mode:** decide between `DEPOSIT_SETTLEMENT=refund` (refund appears in the Sandbox account) and `authorization` (the hold is voided). Rehearse the chosen mode end to end first; authorization mode has not yet been run against the live Sandbox.
3. **Sandbox buyer:** log in to the PayPal Sandbox buyer account in a browser tab so approvals take one click.
4. **Fresh state:** stop the server, delete `storage/*.json` and `storage/audit.jsonl`, and restart so the console starts empty.
5. **Screen layout:** browser with the console on the left (about 60%), terminal on the right. Increase the terminal font size.
6. **Dry run:** run both agent personas once. If the model's behaviour differs from the plan, adjust the narration rather than the code.
7. **Fallback:** if Claude or PayPal is slow on the day, record with `PAYMENT_MODE=mock` and say so on screen ("simulated payments"), or use the scripted crawlers (`client.compliant`, `client.violating`).

## Storyboard

| Time | Screen | Voiceover |
| :--- | :--- | :--- |
| 0:00–0:15 | Title card, then the console's empty KPI row | "AI crawlers read the web at machine speed. `robots.txt` is only a request, and publishers pay for every scraper that ignores it." |
| 0:15–0:35 | Terminal: `curl -i http://127.0.0.1:8000/gateway/news` showing `HTTP 402` and the deposit | "CrawlerTrust turns the policy into a commitment. Before an AI agent can read, it posts a small refundable deposit through PayPal and accepts the rules." |
| 0:35–1:20 | Terminal: `python -m client.agent --persona compliant`. Show the reasoning lines, the 402, the PayPal approval click, paced requests, and the line where it ignores the hidden instruction on `/news/economy`. Console updates live. | "Here Claude is the crawler. It reads the policy, pays the deposit with PayPal, paces itself from the gateway's rate-limit signals, and ignores a prompt injection hidden in a page. It completes the session and gets its deposit back." |
| 1:20–1:35 | PayPal Sandbox: the refund (refund mode) or the voided authorization (authorization mode) | "That's a real Sandbox transaction, returned automatically." |
| 1:35–2:15 | Terminal: `python -m client.agent --persona rogue`. Console: blocked requests appear, the deposit turns **Forfeited**, the violations chart grows. | "Now the same agent, told to grab everything. It requests the admin area, private customer data, and bursts requests. The gateway blocks every one, and the deposit is forfeited by policy, not by the model." |
| 2:15–2:40 | Console: click **Explain** on the forfeited deposit. Click an evidence chip to highlight the audit event. | "Claude then explains the decision to both sides, and every statement cites the exact audit event behind it." |
| 2:40–2:55 | Console: the policy assistant drafts a policy from plain-English terms | "Publishers can write their terms in plain English and Claude drafts the machine-readable policy, validated before anything changes." |
| 2:55–3:00 | Closing card: repo URL and the one-line positioning | "Other 402 schemes charge for access. CrawlerTrust charges for misbehaviour. Good agents crawl free." |

Keep the final cut under 2:55 to leave margin for YouTube's processing.

## Commands used on camera

```bash
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

```bash
curl -i -H "X-Crawler-Id: demo-crawler" http://127.0.0.1:8000/gateway/news
```

```bash
python -m client.agent --persona compliant --base-url http://127.0.0.1:8000
```

```bash
python -m client.agent --persona rogue --base-url http://127.0.0.1:8000
```
