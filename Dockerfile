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
# pip-installed copy (from git master) in a venv + requests fixes that
RUN python3 -m venv /opt/yt-dlp && \
    /opt/yt-dlp/bin/pip install --no-cache-dir https://github.com/yt-dlp/yt-dlp/archive/refs/heads/master.tar.gz requests

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
