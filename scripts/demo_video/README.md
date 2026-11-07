# Demo video pipeline

Records the live demo in a real browser against the PayPal Sandbox and renders a narrated, captioned MP4 for the Devpost submission (YouTube, under 3 minutes).

```bash
scripts/demo_video/make_video.sh ai video-out
```

- **Needs:** `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` (Sandbox app with card payments enabled), and `OPENAI_API_KEY` for the AI driver and the narration. Also Node.js with Playwright, ffmpeg (with libass and libx264), and the Python requirements.
- **What it does:** starts a fresh gateway (Sandbox payments with test-card autopay, public demo), then `record.js` drives the demo page through the scenes (compliant run, explanation, Verify with PayPal through PayPal's MCP server, rogue run, PayPal assistant question, console) while Playwright records. `compose.py` cuts the recording into scenes, speeds up long runs (with an on-screen speed badge), adds the title and closing cards, narrates with OpenAI text-to-speech, and burns in captions.
- **Honest narration:** `record.js` saves what actually happened (`facts.json`: deposit outcomes, violations, whether the agent fell for the injection, PayPal verification, explanation source). `compose.py` builds the narration from those facts and stops if a take does not match the script (for example the compliant agent got its deposit forfeited). Re-record, or pass `--force` to render a failed take for review.
- **Output:** `video-out/agenttrust-demo.mp4` and `agenttrust-demo.srt` (the captions are already burned into the MP4; upload the SRT only if you want YouTube's own caption track as well).
- **Options:** `scripted` instead of `ai` records the scripted crawler (no AI key; narration says so). `DEMO_TTS_VOICE` (default `marin`), `DEMO_TTS_MODEL` (default `gpt-4o-mini-tts`, falling back to `tts-1-hd`), `DEPOSIT_SETTLEMENT=authorization` for the hold flow. `--no-tts` renders captions only.
