# gmail-automator runtime image.
#
# Builds with either the classic builder or BuildKit: no `--mount` directives, so `docker build`
# works on a stock Docker install. Swap the uv `COPY --from` line for a local `pip install uv` if
# you build without registry access.

# ---- build stage -------------------------------------------------------------
FROM python:3.12-slim-bookworm AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /build

# Dependency metadata first, so an application-only change reuses the install layer.
COPY pyproject.toml README.md ./
RUN uv pip install --system --no-deps .

COPY src ./src
RUN uv pip install --system --no-deps .

# Resolve and install the real dependency set against the built package.
COPY src ./src
RUN uv pip install --system .

# ---- runtime stage -----------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="gmail-automator" \
      org.opencontainers.image.description="Self-hosted Gmail gateway for AI agents (MCP + REST)" \
      org.opencontainers.image.licenses="MIT"

# `curl` exists only for the compose healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/gmail-automator /usr/local/bin/gmail-automator

# Alembic needs the migration files and alembic.ini at runtime; the app needs its own package.
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

# R12: non-root uid, a single writable volume, no shell.
RUN groupadd --gid 10001 gmailautomator \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin gmailautomator \
    && mkdir -p /data \
    && chown -R gmailautomator:gmailautomator /data /app
USER 10001:10001

ENV GMAIL_AUTOMATOR_DATABASE_URL=sqlite:////data/gmail-automator.db \
    GMAIL_AUTOMATOR_ALEMBIC_INI=/app/alembic.ini \
    GMAIL_AUTOMATOR_HOST=0.0.0.0 \
    GMAIL_AUTOMATOR_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
VOLUME ["/data"]
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["gmail-automator"]
CMD ["serve"]
