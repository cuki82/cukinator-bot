#!/usr/bin/env python3
"""Transcripción de audio con OpenAI Whisper API. Uso: transcribe.py <audio_path>

Usa la API cloud (no requiere torch ni ffmpeg locales). Necesita OPENAI_API_KEY
en el entorno. Si falla la API o no hay key, devuelve ERROR: por stderr.
"""
import sys
import os
import warnings

warnings.filterwarnings("ignore")

if len(sys.argv) < 2 or not os.path.exists(sys.argv[1]):
    sys.exit(1)

audio_path = sys.argv[1]

# Cargar vault si está disponible (para que OPENAI_API_KEY se hidrate desde DB)
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    _repo = os.path.dirname(_here)
    sys.path.insert(0, _repo)
    from services.vault import load_all_to_env  # type: ignore
    load_all_to_env()
except Exception:
    pass

api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
if not api_key:
    sys.stderr.write("ERROR:OPENAI_API_KEY no configurada\n")
    sys.exit(1)

try:
    import requests
    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (os.path.basename(audio_path), f, "audio/ogg")},
            data={"model": "whisper-1", "language": "es"},
            timeout=60,
        )
    if r.status_code != 200:
        sys.stderr.write(f"ERROR:HTTP {r.status_code}: {r.text[:200]}\n")
        sys.exit(1)
    texto = (r.json().get("text") or "").strip()
    print(texto, flush=True)
except Exception as e:
    sys.stderr.write(f"ERROR:{e}\n")
    sys.exit(1)
