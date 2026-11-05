#!/usr/bin/env bash
# Record and render the CrawlerTrust demo video against the live PayPal Sandbox.
#
# Usage: scripts/demo_video/make_video.sh [ai|scripted] [output-dir]
# Needs: PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (Sandbox app), and for the AI driver
# OPENAI_API_KEY (narration also uses it for text-to-speech). Node.js with Playwright,
# ffmpeg, and the Python requirements must be installed.
set -euo pipefail

DRIVER="${1:-ai}"
OUT="$(mkdir -p "${2:-video-out}" && cd "${2:-video-out}" && pwd)"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${PYTHON:-python3}"
PORT="${DEMO_VIDEO_PORT:-8765}"

: "${PAYPAL_CLIENT_ID:?Set PAYPAL_CLIENT_ID (PayPal Sandbox app)}"
: "${PAYPAL_CLIENT_SECRET:?Set PAYPAL_CLIENT_SECRET (PayPal Sandbox app)}"
if [ "$DRIVER" = "ai" ] && [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "The AI driver needs OPENAI_API_KEY (or ANTHROPIC_API_KEY)." >&2
  exit 2
fi

STATE="$(mktemp -d)"
export PAYMENT_MODE=paypal_sandbox PAYPAL_SANDBOX_AUTOPAY=true \
  DEPOSIT_SETTLEMENT="${DEPOSIT_SETTLEMENT:-refund}" \
  DEMO_PUBLIC=true DASHBOARD_PUBLIC=true APP_PUBLIC_URL="http://127.0.0.1:${PORT}" \
  AUDIT_LOG_PATH="$STATE/audit.jsonl" PAYMENTS_DB_PATH="$STATE/payments.json" SESSIONS_DB_PATH="$STATE/sessions.json"
if [ -n "${OPENAI_API_KEY:-}" ]; then export AI_PROVIDER="${AI_PROVIDER:-openai}"; fi

cd "$REPO"
echo "Warming up PayPal's MCP server package..."
PAYPAL_ACCESS_TOKEN=warmup timeout 180 npx -y @paypal/mcp@1.8.1 --tools=orders.get </dev/null >/dev/null 2>&1 || true

"$PY" -m uvicorn server.main:app --host 127.0.0.1 --port "$PORT" >"$OUT/server.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null && break; sleep 0.5; done
curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null || { echo "Gateway did not start; see $OUT/server.log" >&2; exit 1; }

echo "Recording ($DRIVER driver)..."
node "$REPO/scripts/demo_video/record.js" "http://127.0.0.1:${PORT}" "$OUT" "$DRIVER"

echo "Composing..."
"$PY" "$REPO/scripts/demo_video/compose.py" "$OUT" "${@:3}"
