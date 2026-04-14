# Dockerfile for Cukinator Bot
FROM python:3.11-slim

WORKDIR /app

# System dependencies for audio processing and compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install build tools
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Python dependencies (usando faster-whisper en lugar de openai-whisper)
RUN pip install --no-cache-dir \
    python-telegram-bot==22.0 \
    anthropic==0.42.0 \
    httpx>=0.27.0 \
    faster-whisper==1.0.3 \
    pyswisseph==2.10.3.2 \
    requests==2.32.3 \
    duckduckgo-search==7.3.2 \
    elevenlabs==1.4.0 \
    fpdf2==2.8.1 \
    paramiko==3.4.0

# Copy application code
COPY . .

# Run the bot
CMD ["python", "bot.py"]
