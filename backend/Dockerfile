# =============================================================================
# FastAPI backend — multi-stage.
#   target: dev   -> hot-reload, dev/test tooling included
#   target: prod  -> slim runtime, non-root
# =============================================================================

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl is used by the container healthcheck; libpq for psql-style tooling.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libpq5 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency metadata first so `docker build` reuses the pip layer
# whenever only application source has changed.
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py


# --- dev ---------------------------------------------------------------------
FROM base AS dev
RUN pip install -e ".[dev]"
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# --- prod --------------------------------------------------------------------
FROM base AS prod
RUN pip install .
COPY . .

# Never run the API as root.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
