#!/usr/bin/env python3
"""
transcribe.py - Transcribe audio usando faster-whisper
Uso: python transcribe.py archivo.ogg
Salida: texto transcrito a stdout
"""
import sys
import os

def transcribe(audio_path: str) -> str:
    """Transcribe audio file to text using faster-whisper."""
    try:
        from faster_whisper import WhisperModel
        
        # Modelo pequeño para velocidad, CPU only
        model = WhisperModel("base", device="cpu", compute_type="int8")
        
        segments, info = model.transcribe(audio_path, language="es")
        
        # Juntar todos los segmentos
        texto = " ".join(segment.text.strip() for segment in segments)
        
        return texto.strip()
    
    except Exception as e:
        return f"ERROR: {e}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("ERROR: Falta path del archivo de audio")
        sys.exit(1)
    
    audio_path = sys.argv[1]
    
    if not os.path.exists(audio_path):
        print(f"ERROR: Archivo no encontrado: {audio_path}")
        sys.exit(1)
    
    result = transcribe(audio_path)
    print(result)
