# syntax=docker/dockerfile:1.7

# ─── Builder ────────────────────────────────────────────────────────────────
# Use a venv in the builder so the runtime image only needs Python + the venv.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# No build-essential needed today — fastapi / uvicorn / pydantic all
# ship prebuilt wheels for python:3.12-slim. Phase 1 added sqlalchemy,
# alembic, geoalchemy2 (pure-Python) and psycopg[binary] (which bundles
# libpq in the wheel). Add the apt layer back if/when rasterio lands.

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ─── Runtime ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

# Run as non-root.
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app

# Bring the venv from the builder; no compilers in the runtime image.
COPY --from=builder /opt/venv /opt/venv

# App code. main.py uses sibling-module imports (from models import ...),
# so the working dir IS the import root — no package wrapping.
COPY --chown=app:app . .

USER app

EXPOSE 8000

# Lightweight HTTP healthcheck against /health.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os, urllib.request, sys; \
url=f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=3).status == 200 else 1)"

# Fly.io / most PaaS providers inject PORT. Fall back to 8000 locally.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
