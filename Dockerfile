FROM python:3.11-bullseye

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    g++ \
    make \
    cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar numpy primero (whisper lo necesita)
RUN pip install --no-cache-dir numpy==1.26.4

# Instalar el resto
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py transcribe.py ./

# Pre-descargar modelo Whisper tiny durante el build
RUN python -c "import whisper; whisper.load_model('tiny')" || true

CMD ["python", "bot.py"]
