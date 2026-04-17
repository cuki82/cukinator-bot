"""
intent_router.py — Clasificador de intenciones para el bot de Telegram.

Dos categorías principales:
- conversational: el bot responde directo con Claude
- coding_task: se delega al Agent Worker en el VPS

El router es deliberadamente simple — el Orchestrator no necesita
ser sofisticado para clasificar. Claude lo hace mejor que reglas.
"""

import os
import logging
import anthropic

log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")

# ── Clasificador via Claude (rápido, modelo pequeño) ──────────────────────────

ROUTER_SYSTEM = """Sos un clasificador de intenciones. Analizás el mensaje del usuario y devolvés EXACTAMENTE una de estas categorías:

conversational
coding_task

REGLAS:
- coding_task si el mensaje implica: editar código, modificar archivos del bot, cambiar handlers, agregar módulos, tocar GitHub, hacer commits, abrir PRs, cambiar configuración del repo, modificar el bot en sí mismo.
- conversational para TODO lo demás: preguntas, búsquedas, clima, emails, calendario, VPS status, astrología, reservas, charla general, análisis, explicaciones.

Respondé SOLO con la categoría. Sin explicación. Sin puntos. Solo la palabra."""

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _client


def classify(user_text: str) -> str:
    """
    Clasifica usando SOLO keywords — sin llamada a API.
    Sin latencia, sin costo.
    """
    return _keyword_classify(user_text)


def _keyword_classify(text: str) -> str:
    """Fallback por keywords cuando la API falla."""
    text_lower = text.lower()
    coding_keywords = [
        "modificá el bot", "modifica el bot", "cambiá el código", "cambia el código",
        "agregá un handler", "agrega un handler", "nuevo módulo", "nueva función",
        "editá", "edita el archivo", "cambiá bot_core", "modifica bot_core",
        "push a github", "hacé un commit", "hace un commit", "abrí un pr", "abri un pr",
        "cambiá el system prompt", "modifica el system prompt",
        "agregá un tool", "agrega un tool", "nueva integración al bot",
        "refactorizá", "refactoriza", "reescribí", "reescribe",
    ]
    if any(kw in text_lower for kw in coding_keywords):
        return "coding_task"
    return "conversational"
