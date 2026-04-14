#!/usr/bin/env python3
"""
transcribe.py
Transcribe audio usando OpenAI Whisper API.
Uso: python transcribe.py archivo.ogg
"""
import sys
import os
from openai import OpenAI

def transcribe(audio_path: str) -> str:
    """Transcribe un archivo de audio usando Whisper API."""
    try:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="es"
            )
        
        return transcript.text
    except Exception as e:
        return f"ERROR: {e}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("ERROR: No se especificó archivo de audio")
        sys.exit(1)
    
    audio_path = sys.argv[1]
    
    if not os.path.exists(audio_path):
        print(f"ERROR: Archivo no encontrado: {audio_path}")
        sys.exit(1)
    
    resultado = transcribe(audio_path)
    print(resultado)
