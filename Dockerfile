# SARA gateway: FastAPI app, live demo, publisher console, crawler CLIs, and
# PayPal's MCP server (Node.js) for the read-only PayPal assistant.

# PayPal MCP server (@paypal/mcp), installed from the pinned lockfile.
FROM node:22-bookworm-slim AS paypal-mcp
WORKDIR /opt/paypal-mcp
COPY deploy/paypal-mcp/package.json deploy/paypal-mcp/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Node.js runtime for the MCP server (the slim image already has libstdc++ and libgcc).
COPY --from=paypal-mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=paypal-mcp /opt/paypal-mcp /opt/paypal-mcp
ENV PAYPAL_MCP_COMMAND="node /opt/paypal-mcp/node_modules/@paypal/mcp/dist/index.js"

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server ./server
COPY client ./client
COPY data ./data
COPY storage/README.md ./storage/README.md

RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app/storage
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# One worker only: deposits, sessions, rate limits, and demo runs live in this process
# (JSON files plus in-memory state). Render sets WEB_CONCURRENCY, which uvicorn would
# otherwise use.
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
