# CrawlerTrust gateway: FastAPI app, publisher dashboard, and crawler CLIs.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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

# One worker only: deposits, sessions, and rate limits live in this process (JSON files
# plus in-memory state). Render sets WEB_CONCURRENCY, which uvicorn would otherwise use.
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
