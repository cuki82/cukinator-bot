#!/usr/bin/env python3
"""Transcripcion de audio con Whisper local. Uso: transcribe.py <audio_path>"""
import sys, warnings, os
warnings.filterwarnings("ignore")

if len(sys.argv) < 2 or not os.path.exists(sys.argv[1]):
    sys.exit(1)

try:
    import whisper
    model = whisper.load_model("base")
    result = model.transcribe(sys.argv[1], language="es")
    texto = result["text"].strip()
    print(texto, flush=True)
except Exception as e:
    sys.stderr.write(f"ERROR:{e}\n")
    sys.exit(1)
