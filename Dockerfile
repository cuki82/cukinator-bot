FROM python:3.11

RUN apt-get update && apt-get install -y \
    ffmpeg gcc g++ \
    fonts-dejavu-mono \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir numpy==1.26.4
RUN pip install --no-cache-dir pyswisseph==2.10.3.2
RUN pip install --no-cache-dir openai-whisper==20250625
RUN pip install --no-cache-dir \
    python-telegram-bot==22.7 \
    anthropic==0.94.0 \
    ddgs==9.13.0 \
    fpdf2==2.8.7 \
    geopy==2.4.1 \
    pytz==2026.1.post1 \
    requests==2.32.5 \
    timezonefinder==8.2.2

RUN mkdir -p /data

COPY bot.py transcribe.py swiss_engine.py memory_store.py config_store.py ./

CMD ["python", "bot.py"]
