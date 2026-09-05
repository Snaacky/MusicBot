FROM node:24-bookworm-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    python3 \
    python3-venv \
    build-essential \
    pkg-config \
    libopus-dev \
    ffmpeg \
    curl \
    unzip \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Standalone yt-dlp binary cannot use HTTPS proxies (no requests/curl_cffi bundled);
# pip-installed nightly (yt-dlp[default] pulls in requests) in a venv fixes that
RUN python3 -m venv /opt/yt-dlp && \
    /opt/yt-dlp/bin/pip install --no-cache-dir --pre "yt-dlp[default]"

# make youtube-dl-exec use the venv binary instead of downloading the standalone one
ENV YOUTUBE_DL_DIR=/opt/yt-dlp/bin \
    YOUTUBE_DL_FILENAME=yt-dlp \
    YOUTUBE_DL_SKIP_DOWNLOAD=1

RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

COPY package*.json ./

RUN npm install

COPY . .

ENTRYPOINT ["npm", "run", "start"]
