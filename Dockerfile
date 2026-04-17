FROM python:3.11

RUN apt-get update && apt-get install -y \
    ffmpeg gcc g++ \
    fonts-dejavu-mono \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir numpy==1.26.4
RUN pip install --no-cache-dir pyswisseph==2.10.3.2
RUN pip install --no-cache-dir openai-whisper==20250625
RUN pip install --no-cache-dir ddgs==9.13.0
RUN pip install --no-cache-dir "python-telegram-bot[job-queue]==22.7"
RUN pip install --no-cache-dir anthropic==0.94.0
RUN pip install --no-cache-dir fpdf2==2.8.7 geopy==2.4.1 timezonefinder==8.2.2
RUN pip install --no-cache-dir requests==2.32.5 pytz==2026.1.post1 httpx==0.27.2
RUN pip install --no-cache-dir gTTS==2.5.4 yt-dlp==2026.3.17 paramiko==3.5.0 pypdf==4.3.1
RUN pip install --no-cache-dir cryptography==44.0.2
RUN pip install --no-cache-dir fastapi==0.115.12 uvicorn==0.34.2 pydantic==2.11.3

RUN mkdir -p /data

COPY core/       ./core/
COPY agents/     ./agents/
COPY handlers/   ./handlers/
COPY services/   ./services/
COPY modules/    ./modules/
COPY workers/    ./workers/

CMD ["python", "core/bot.py"]
