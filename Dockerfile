# ── examtopics-scrapper (web UI) ──────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Install deps first so this layer is cached unless requirements change
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (scraper engine + bulk finder + examcademy source + web frontend)
COPY scraper.py finder.py examcademy.py webapp.py ./

# Run as non-root
RUN useradd --create-home --uid 1000 scraper && chown -R scraper:scraper /app
USER scraper

EXPOSE 8000

# Serve the Flask app with gunicorn.
#  - gthread + a SINGLE worker: bulk jobs are tracked in process memory, so
#    with 2+ workers a status poll could land on the worker that doesn't
#    know the job and 404. Threads give us the concurrency instead.
#  - --timeout 60: the scraper enforces a 25s wall-clock budget across all
#    retries (SCRAPER_TOTAL_TIMEOUT), so this only fires if something is
#    badly wrong. It must stay *above* that budget, or gunicorn kills the
#    worker before the app can render its own error page. Bulk work runs on
#    a background thread and so is not bound by it.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--worker-class", "gthread", "--workers", "1", "--threads", "16", \
     "--timeout", "60", "--access-logfile", "-", "webapp:app"]
