# Multi-stage build. Builder stage resolves + installs from the locked
# uv.lock (uv sync --frozen -- never resolves fresh, so what runs in the
# container is exactly what tests ran against); runtime stage is a slim
# image with only the built venv, no build toolchain, no dev dependencies
# (pytest/ruff/httpx never ship).
#
# Build from the repo root: docker build -f docker/Dockerfile -t pulse-serve .
# (docker-compose.yml does this automatically via `context: .`.)

FROM python:3.13-slim AS builder

# `latest` rather than a pinned uv version: this Dockerfile was never built
# (Docker daemon wasn't running on the dev machine -- see docs/design.md's
# verification note), so a pinned tag risked referencing a uv release that
# doesn't exist. uv.lock is what actually pins reproducibility here.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, in their own layer -- keyed only on pyproject.toml +
# uv.lock, so an application-code-only change doesn't invalidate the
# dependency install cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now the application itself.
COPY pulse_serve ./pulse_serve
COPY scripts ./scripts
COPY migrations ./migrations
COPY models ./models
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim AS runtime

RUN groupadd --gid 1000 pulse && useradd --uid 1000 --gid pulse --create-home pulse

WORKDIR /app
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER pulse

EXPOSE 8000
# 9200: worker's own /metrics, when this same image runs as the worker
# service (docker-compose.yml's `command` override).
EXPOSE 9200

# docker-compose.yml overrides `command` for the worker and migrate
# services -- this default is the api process.
CMD ["uvicorn", "pulse_serve.app:app", "--host", "0.0.0.0", "--port", "8000"]
