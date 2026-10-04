# syntax=docker/dockerfile:1
# Build context: repository root (the uv workspace lives there).

FROM python:3.11-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1
WORKDIR /app

# Dependencies first: this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml apps/api/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package api --no-install-workspace

# Then the application itself, installed (non-editable) into the venv.
COPY apps/api/app apps/api/app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --package api --no-editable

FROM python:3.11-slim AS runner
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    API_HOST=0.0.0.0
WORKDIR /app
RUN useradd --system --uid 1001 --no-create-home api
COPY --from=builder --chown=api:api /app/.venv .venv
USER api
CMD ["python", "-m", "app"]
