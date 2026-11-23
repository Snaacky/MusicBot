FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM denoland/deno:bin-2.9.8 AS deno
FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    DJ_KHALED_HOME=/config \
    FFMPEG_PATH=/usr/bin/ffmpeg \
    DENO_PATH=/usr/local/bin/deno \
    DENO_DIR=/config/deno-cache \
    DENO_NO_UPDATE_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml uv.lock .python-version LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --no-cache \
    && mkdir -p /config/data

VOLUME ["/config"]
ARG BUILD_REVISION
ENV DJ_KHALED_BUILD_REVISION=${BUILD_REVISION}
STOPSIGNAL SIGTERM
ENTRYPOINT ["/app/.venv/bin/dj-khaled"]
