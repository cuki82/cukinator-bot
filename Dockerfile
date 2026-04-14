FROM python:3.11-slim

WORKDIR /app

# System dependencies for whisper, audio processing, and Swiss Ephemeris
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    git \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install specific versions that work together
RUN pip install --no-cache-dir \
    openai-whisper==20250625 \
    ddgs==9.13.0 \
    python-telegram-bot==22.7

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

# Create data directory for persistent storage
RUN mkdir -p /data

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DB_PATH=/data/memory.db

# Run the bot
CMD ["python", "bot.py"]
