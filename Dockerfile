# ── examtopics-scrapper (web UI) ──────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Install deps first so this layer is cached unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (scraper engine + web frontend)
COPY scraper.py webapp.py ./

# Run as non-root
RUN useradd --create-home --uid 1000 scraper && chown -R scraper:scraper /app
USER scraper

EXPOSE 8000

# Serve the Flask app with gunicorn.
#  - gthread workers: each worker handles several requests concurrently
#    via threads, so a single slow upstream can't starve the whole worker.
#  - --timeout 120: generous safety net; the scraper itself enforces a
#    tight (connect=10s, read=30s) timeout in code.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--worker-class", "gthread", "--workers", "2", "--threads", "4", \
     "--timeout", "120", "--access-logfile", "-", "webapp:app"]
