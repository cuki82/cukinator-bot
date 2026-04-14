FROM python:3.11-slim

# System dependencies for whisper, audio processing, swisseph
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install setuptools first (needed for whisper build)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Python dependencies
RUN pip install --no-cache-dir \
    python-telegram-bot==22.0 \
    anthropic==0.42.0 \
    httpx>=0.27.0 \
    openai-whisper==20231117 \
    pyswisseph==2.10.3.2 \
    requests==2.32.3 \
    duckduckgo-search==7.3.2 \
    elevenlabs==1.4.0 \
    fpdf2==2.8.1 \
    paramiko==3.4.0

WORKDIR /app
COPY . .

CMD ["python", "bot.py"]
