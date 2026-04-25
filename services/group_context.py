"""
services/group_context.py — Buffer en memoria del contexto reciente de cada
grupo de Telegram donde el bot está activo.

Problema que resuelve:
    En grupos, el bot ignora silenciosamente mensajes que no le están
    dirigidos (filtro mention/reply). Eso hace que cuando alguien SÍ lo
    invoca, el bot no tenga idea de lo que se venía hablando entre los
    humanos. Pierde anáforas ("ella", "le", "ese tema"), nombres, contexto.

Solución:
    Mantener un buffer ring por chat_id (deque maxlen=N) con TODOS los
    mensajes del grupo (los procesados Y los ignorados). Cuando el bot
    va a responder, se inyecta este buffer como contexto al prompt.

    NO se persiste en SQLite — es solo memoria. Si el bot reinicia, se
    pierde el contexto reciente del grupo (es aceptable, son grupos).
"""
from __future__ import annotations
import time
import threading
from collections import deque
from typing import Dict, Deque, Optional

# chat_id → deque de tuplas (ts_unix, sender_name, text)
_BUFFERS: Dict[int, Deque[tuple]] = {}
_LOCK = threading.Lock()

# Capacidad por grupo. 20 mensajes alcanza para mantener un hilo y no
# explotar tokens (cada msg promedio 80 chars → ~1600 chars de contexto).
MAX_PER_CHAT = 20

# Edad máxima de un mensaje antes de descartarlo del contexto.
# 30 min — más allá de eso normalmente ya cambió el tema.
MAX_AGE_S = 1800


def append_message(chat_id: int, sender_name: str, text: str) -> None:
    """Agrega un mensaje al buffer. Solo aplicar a chats de grupo."""
    if not chat_id or not text:
        return
    text = text.strip()
    if not text:
        return
    with _LOCK:
        if chat_id not in _BUFFERS:
            _BUFFERS[chat_id] = deque(maxlen=MAX_PER_CHAT)
        _BUFFERS[chat_id].append((time.time(), sender_name or "?", text[:500]))


def get_context(chat_id: int, exclude_last: bool = True) -> str:
    """Devuelve el contexto formateado para inyectar al prompt.
    exclude_last: si True, excluye el último mensaje (que típicamente es
    el mensaje del user que está siendo procesado AHORA — no queremos
    duplicarlo en el contexto).
    """
    with _LOCK:
        buf = _BUFFERS.get(chat_id)
        if not buf:
            return ""
        items = list(buf)
    if exclude_last and items:
        items = items[:-1]
    # Filtrar viejos
    now = time.time()
    items = [(ts, s, t) for ts, s, t in items if now - ts <= MAX_AGE_S]
    if not items:
        return ""
    lines = [
        "── CONTEXTO RECIENTE DEL GRUPO (mensajes de los últimos minutos, "
        "incluyendo charlas entre humanos que NO te llamaban a vos) ──",
        "Usalo SOLO para resolver referencias ('ella', 'eso', 'él', nombres, "
        "temas previos). NUNCA lo cites textualmente al user. NUNCA respondas "
        "como si esos mensajes fueran para vos — eran entre otras personas.",
        "",
    ]
    for ts, sender, text in items:
        # mm:ss antes del ahora (para sentido temporal)
        ago = int(now - ts)
        if ago < 60:
            ago_str = f"hace {ago}s"
        else:
            ago_str = f"hace {ago // 60}m"
        lines.append(f"[{sender} · {ago_str}] {text}")
    lines.append("── FIN CONTEXTO GRUPO ──\n")
    return "\n".join(lines)


def reset(chat_id: int) -> None:
    """Borra el buffer de un grupo (útil si el grupo cambia de tema o admin lo pide)."""
    with _LOCK:
        _BUFFERS.pop(chat_id, None)


def stats() -> dict:
    """Diagnóstico — para /debug u otro endpoint admin."""
    with _LOCK:
        return {
            "groups_active": len(_BUFFERS),
            "by_group": {cid: len(buf) for cid, buf in _BUFFERS.items()},
            "max_per_chat": MAX_PER_CHAT,
            "max_age_s": MAX_AGE_S,
        }
