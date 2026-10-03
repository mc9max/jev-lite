# syntax=docker/dockerfile:1
# ══════════════════════════════════════════════════════════════════════════
# Railway Template: Jev Lite
# Upstream API: TypeSafe Jev (System One) — https://api.typesafe.ai/v1/systemone
# This image is a thin stateless client: FastAPI proxy + single-file web
# console. No model weights, no GPU, no volume. Hobby plan friendly (~150 MB).
# ══════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# Deps first (cache-friendly layer); then code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ ./static/

EXPOSE 8080

# In-image healthcheck (Railway uses /health on PORT from railway.json).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080')), timeout=4)"

# Non-root (uid 1000). Safe here: no volume is mounted, so there is nothing
# root-owned to collide with (the Railway-volume EACCES trap applies to
# volume-mounted paths only).
USER 1000

# Shell form so ${PORT} is expanded at container start (Railway injects PORT).
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
