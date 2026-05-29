FROM ghcr.io/astral-sh/uv:0.5-python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev || uv sync --no-dev


FROM python:3.13-slim-bookworm AS runtime

RUN useradd --create-home --uid 1000 vhsbot
WORKDIR /app

COPY --from=builder --chown=vhsbot:vhsbot /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/vhsbot.db \
    SNAPSHOT_DIR=/data/snapshots

USER vhsbot
VOLUME ["/data"]

CMD ["python", "-m", "vhsbot.main"]
