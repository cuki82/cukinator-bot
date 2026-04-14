FROM python:3.11-slim

WORKDIR /app

# System dependencies for whisper, pyswisseph, etc
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

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

# Copy application code
COPY bot.py .
COPY bot_core.py .
COPY swiss_engine.py .
COPY memory_store.py .
COPY config_store.py .
COPY agent_ops.py .
COPY reinsurance_kb.py .
COPY transcribe.py .
COPY handlers/ ./handlers/
COPY modules/ ./modules/

# Create data directory for SQLite
RUN mkdir -p /data

ENV DB_PATH=/data/memory.db

CMD ["python", "bot.py"]
