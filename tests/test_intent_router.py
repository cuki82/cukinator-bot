#!/usr/bin/env python3
"""Tests del intent_router — clasificación + selección de modelo."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.intent_router import classify, classify_complexity, select_model


CASES_INTENT = [
    ("hola cuki qué hora es en Buenos Aires",                       "conversational"),
    ("cómo estás?",                                                  "conversational"),
    ("explicame el reaseguro proporcional en detalle",              "reinsurance"),
    ("cómo está implementado el voice handler?",                     "coding"),
    ("revisá el código del intent router",                           "coding"),
    ("investigá por qué no funciona el transcribe",                  "coding"),
    ("proponé una mejora al router",                                 "coding"),
    ("entrá al VPS y corré systemctl status cukinator",              "coding"),
    ("buscá en internet qué pasó en Python 3.13",                    "research"),
    ("explicá mi carta natal completa",                              "astrology"),
    ("qué tránsitos tengo hoy",                                      "astrology"),
    ("recordá mi preferencia",                                       "personal"),
]

CASES_COMPLEXITY = [
    ("qué hora es",                                                  "simple"),
    ("ok",                                                           "simple"),
    ("contame sobre Postgres",                                       "simple"),  # <8 palabras
    ("explicame en detalle los aspectos astrológicos del trígono",   "complex"),
    ("no proporcional vs proporcional",                              "simple"),  # 'no' pelado removido
]

CASES_MODEL = [
    ("qué hora es",                  "conversational", "claude-haiku-4-5"),
    ("explicá mi carta natal profundamente con tránsitos completos", "astrology", "claude-opus-4-5"),
    ("entrá al VPS",                 "coding",         "claude-sonnet-4-6"),  # medium por default
]


def _check(label, got, expected):
    ok = "OK  " if got == expected else "FAIL"
    print(f"{ok} {label}: got={got!r} expected={expected!r}")
    return got == expected


def main():
    fails = 0
    print("\n== classify(intent) ==")
    for text, exp in CASES_INTENT:
        if not _check(text[:60], classify(text), exp):
            fails += 1

    print("\n== classify_complexity ==")
    for text, exp in CASES_COMPLEXITY:
        if not _check(text[:60], classify_complexity(text), exp):
            fails += 1

    print("\n== select_model ==")
    for text, intent, exp in CASES_MODEL:
        if not _check(f"{intent}/{text[:40]}", select_model(text, intent), exp):
            fails += 1

    total = len(CASES_INTENT) + len(CASES_COMPLEXITY) + len(CASES_MODEL)
    print(f"\n{total - fails}/{total} passed")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
