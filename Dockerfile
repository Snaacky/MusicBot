FROM node:24-bookworm-slim

# Set base working directory to /app
WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    python3 \
    build-essential \
    pkg-config \
    libopus-dev \
    ffmpeg \
    curl \
    unzip \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Add deno to the path
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

# Clone the snaacky/musicbot repository into /app/musicbot
RUN git clone https://github.com/snaacky/musicbot.git musicbot

# Change the working directory to /app/musicbot
WORKDIR /app/musicbot

# Install JavaScript dependencies
RUN npm install

# Start the bot
ENTRYPOINT ["npm", "run", "start"]