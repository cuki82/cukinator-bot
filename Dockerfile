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

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /data

COPY bot.py bot_core.py transcribe.py ./
COPY swiss_engine.py memory_store.py config_store.py reinsurance_kb.py agent_ops.py ./
COPY handlers/ ./handlers/

CMD ["python", "bot.py"]
# rebuild 1776149812
