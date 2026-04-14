FROM python:3.11

RUN apt-get update && apt-get install -y \
    ffmpeg gcc g++ \
    fonts-dejavu-mono \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar en orden para evitar conflictos de cache
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir numpy==1.26.4
RUN pip install --no-cache-dir pyswisseph==2.10.3.2
RUN pip install --no-cache-dir openai-whisper==20250625
RUN pip install --no-cache-dir ddgs==9.13.0
RUN pip install --no-cache-dir "python-telegram-bot[job-queue]==22.7"
RUN pip install --no-cache-dir anthropic==0.94.0
RUN pip install --no-cache-dir fpdf2==2.8.7 geopy==2.4.1 timezonefinder==8.2.2
RUN pip install --no-cache-dir requests==2.32.5 pytz==2026.1.post1 httpx==0.27.2
RUN pip install --no-cache-dir gTTS==2.5.4 yt-dlp==2026.3.17

RUN mkdir -p /data

COPY bot.py bot_core.py transcribe.py ./
COPY swiss_engine.py memory_store.py config_store.py reinsurance_kb.py agent_ops.py ./
COPY handlers/ ./handlers/

CMD ["python", "bot.py"]
