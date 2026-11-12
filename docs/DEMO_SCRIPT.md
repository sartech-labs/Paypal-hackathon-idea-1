# Demo video script (under 3 minutes)

The Devpost submission needs a public YouTube video shorter than 3 minutes. This script follows a website owner from sign-in to a kept deposit. Everything happens in the browser on the live instance (https://sara-gateway.onrender.com) or a local one. No terminal appears on camera.

## Before recording

1. **Settings:** `PAYMENT_MODE=paypal_sandbox`, `DEPOSIT_SETTLEMENT=authorization`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`), `OWNER_EMAIL`, `OWNER_PASSWORD`, `SESSION_SECRET`, `SEED_DEFAULT_SITE=false`. Log in with PayPal must be turned on for the PayPal app, with `<APP_PUBLIC_URL>/paypal/connect/callback` as a return URL.
2. **Fresh state:** on Render a deploy resets storage. Locally, stop the server, move `storage/` aside, and restart, so the console starts empty and the onboarding shows.
3. **Accounts at hand:** the Sandbox business account (for Log in with PayPal) and the Sandbox personal account (to approve the test agent's deposit). Never show passwords on screen; pause the recording while typing them.
4. **Dry run:** run Price scraper and Polite researcher once. If a model run differs from this script, change the narration, not the code.
5. **Wake the service:** on Render's free plan, open the URL a few minutes before recording.
6. **Browser:** one window at 1440 px wide, zoom 100%, other tabs closed.

## Storyboard

| Time | Screen | Voiceover |
| :--- | :--- | :--- |
| 0:00–0:15 | Home page: "Good agents get in. Bad agents pay." and the gate-log animation | "AI agents now visit websites at machine speed. `robots.txt` asks them to behave, but nothing happens when they don't." |
| 0:15–0:25 | Sign in; the empty Overview with the onboarding | "SARA is a gateway I put in front of my website. Every agent leaves a refundable PayPal deposit and follows my rules, or loses it." |
| 0:25–0:45 | **Websites → Setup:** add the website. Its robots.txt rules appear. Point at the skipped domain steps. | "I add my site. SARA reads its robots.txt and turns it into enforced rules. Verifying the domain and switching DNS are skipped in this demo." |
| 0:45–1:05 | **Connect PayPal:** the Log in with PayPal button, the PayPal sign-in, back in the console with the account connected | "Then I connect the PayPal account that gets paid when an agent breaks the rules." |
| 1:05–1:15 | Set the deposit to $5 and block `/admin` and `/private`; save | "Five-dollar deposit, and my admin and private pages are off limits." |
| 1:15–1:55 | **Playground:** choose **Price scraper**, run. The 402, the PayPal button, approve as the agent's payer. The steps fill in: 403s, 429s, budget used up. Outcome: deposit kept, with the AI explanation. | "Now I attack my own site. This AI agent is told to scrape everything. It gets a 402 and pays a PayPal deposit; PayPal holds the money and nothing moves yet. It goes for forbidden pages and floods the site, and the gateway blocks every request. The deposit is kept, and the explanation cites each violation." |
| 1:55–2:20 | Run **Polite researcher**. Outcome: hold released. | "A well-behaved agent reads the allowed pages within the limits. Its hold is voided: it pays nothing, not even a PayPal fee." |
| 2:20–2:40 | **Money → Deposits:** open the kept deposit, its PayPal reference, **Verify with PayPal**; then **Overview** with the new numbers | "Every deposit has its PayPal reference, and I can check it against PayPal's own records through PayPal's MCP server. The overview shows who got in and who paid." |
| 2:40–2:55 | Closing card: "Good agents get in. Bad agents pay." and the repository URL | "SARA, on PayPal: let every agent in, and make the bad ones pay." |

Keep the final cut under 2:55. If time is short, cut the Polite researcher run to its outcome only.

**Fallback:** if the AI provider is slow on the day, use the scripted attacks (Never pays, Path trickery, Reuses a finished session). They use the same gateway and PayPal calls with no model; say so on screen.

## Earlier videos

`scripts/explainer_video` (motion-graphic explainer) and `scripts/demo_video/make_video.sh` (automated screen recorder) were made for an earlier version of the product, which had an agent-side Agent studio at `/agent`. They do not show the current owner console.
