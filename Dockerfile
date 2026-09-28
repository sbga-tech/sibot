# syntax=docker/dockerfile:1

# Rendered /ai images use Maple Mono Normal NL NF CN (SIL OFL 1.1).
FROM alpine:3.22 AS fonts
ARG MAPLE_VERSION=v7.9
ARG MAPLE_SHA256=af8082c484cb1103da6c2efa4b76f403fe342f9a3ac81ff665b8c6c66c2f8863
RUN apk add --no-cache curl unzip \
    && curl -fsSL --retry 5 -o /tmp/maple.zip \
        "https://github.com/subframe7536/maple-font/releases/download/${MAPLE_VERSION}/MapleMonoNormalNL-NF-CN.zip" \
    && echo "${MAPLE_SHA256}  /tmp/maple.zip" | sha256sum -c - \
    && mkdir /fonts \
    && unzip -j /tmp/maple.zip \
        MapleMonoNormalNL-NF-CN-Regular.ttf MapleMonoNormalNL-NF-CN-Bold.ttf LICENSE.txt \
        -d /fonts

FROM ghcr.io/astral-sh/uv:0.12.5-python3.12-trixie-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

COPY --from=fonts /fonts /usr/share/fonts/maple

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project

COPY bot.py ./
COPY sibot ./sibot

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "bot.py"]
