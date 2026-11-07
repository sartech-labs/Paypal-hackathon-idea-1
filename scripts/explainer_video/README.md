# Explainer video pipeline (16:9, 1920x1080)

Builds the Devpost video. It is website-first: the problems websites face from AI agents, the SARA gateway that fixes them (with a live rogue scraper hitting it), the owner's AG Grid dashboard, then how a person uses an agent on such sites, and what PayPal gains. Real screen recordings from the live PayPal Sandbox demo, word-synced captions, and an OpenAI TTS narration.

| File | What it does |
| :--- | :--- |
| `storyboard.json` | Every scene: narration (`vo`), caption with `*accent*` words and ` / ` page breaks, theme (`paper` for problems, `midnight` for the solution), HUD chapter and counter, and the visual. Recording scenes name a source clip and a time range. |
| `engine.html` | Deterministic renderer: `window.__render(scene, t, frame)` draws any frame. Visuals: `orbit`, `gateway` (agents, the gateway's steps, the website), `triad` (the three stakeholders, one highlighted), `tiles`, `big3`, `bars`, `compare`, `walls`, `chat`, `reveal`, `offers`, `flow`, `checks`, `verdicts`, `term`, `end`, and `rec` (a recording in a browser frame, sped up to fit its line, with camera moves and callouts). Visual items appear on the word they name (`cue`). |
| `record_demo.py` | Records Agent studio against a running gateway (Playwright, 1536x864), answering the agent's approval prompts like a person; saves `marks.json` and `facts.json`. |
| `record_crawler.py` | Records the crawler demo page while a scripted rogue scraper hits the gateway: 402, PayPal deposit, 403/429 blocks, deposit kept with the AI explanation. |
| `record_owner.py` | Records the website owner's console: Overview (a kept deposit and its AI explanation), Security, Operations, Commerce in AG Grid, Verify with PayPal (MCP), AG Studio. Run a rogue crawler first so there is something to block. |
| `record_console.py` | Older, shorter console recording (Commerce, Verify, AG Studio); not used by the current storyboard. |
| `capture_mcp.py` | Runs a real MCP storefront session (official MCP client) and saves the calls shown in the MCP scene. |
| `sync_ranges.py` | After re-recording, points every recording scene at the right moments of the new clips, using each recorder's `marks.json`. |
| `build.py` | TTS per scene (`gpt-4o-mini-tts`, voice `marin`) and Whisper word timestamps, recording frames cut and sped to fit each line, every frame rendered in Chromium and piped to ffmpeg, then the narration mixed to -14 LUFS. |

## Rebuild

```bash
pip install imageio-ffmpeg playwright numpy && python -m playwright install chromium
```

```bash
# 1. a Sandbox gateway (separate storage), then the recordings
PAYMENT_MODE=paypal_sandbox PAYPAL_SANDBOX_AUTOPAY=true python -m uvicorn server.main:app --port 8011
python scripts/explainer_video/record_demo.py --base http://127.0.0.1:8011 --out video-out/rec
python scripts/explainer_video/record_crawler.py --base http://127.0.0.1:8011 --out video-out/rec_crawler   # also leaves a rogue run for the owner view
python scripts/explainer_video/record_owner.py --base http://127.0.0.1:8011 --out video-out/rec_owner
python scripts/explainer_video/capture_mcp.py http://127.0.0.1:8011/mcp/ video-out/mcp_session.json
python scripts/explainer_video/sync_ranges.py
```

```bash
# 2. previews of chosen scenes, then the full render
python scripts/explainer_video/build.py --only-preview s03,d03
python scripts/explainer_video/build.py
```

Output: `video-out/sara-explainer.mp4` and `.srt`. Recording scene time ranges in `storyboard.json` refer to `video-out/rec/marks.json`, `video-out/rec_crawler/marks.json` and `video-out/rec_owner/marks.json`; after a new recording, update them. The build re-generates a scene's narration if Whisper hears less than 80% of its words (the TTS occasionally drops a sentence).

## Sources for on-screen facts

| Claim | Source |
| :--- | :--- |
| Crawl-to-referral ratios (Google ~14:1, OpenAI ~1,700:1, Anthropic ~73,000:1) | Cloudflare crawl-to-referral data, 2025, as reported in [Digital Applied (Aug 2026)](https://www.digitalapplied.com/blog/ai-crawl-economics-pay-per-crawl-referral-data-2026) and Cloudflare's Pay Per Crawl coverage ([Engadget](https://engadget.com/ai/cloudflare-experiment-will-block-ai-bot-scrapers-unless-they-pay-a-fee-121523327.html)) |
| Purchase review verdicts (allow e-book, ask dataset, block $129.99 "pre-approved" order) | Live GPT-5.5 run of `client/purchase_critic.py`, 7 Nov 2026 |
| Demo PayPal IDs | Live PayPal Sandbox run, 7 Nov 2026 (`video-out/rec/facts.json`, `video-out/mcp_session.json`, `video-out/rec_owner/verify.txt`) |
| PayPal's benefit ("a package PayPal can offer", "new volume") | The pitch, not a shipped PayPal product: the code runs every agent payment on PayPal APIs, but no PayPal pricing or merchant package exists |
