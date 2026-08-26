# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.5-python3.12-trixie-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

COPY bot.py ./
COPY sibot ./sibot

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "bot.py"]
