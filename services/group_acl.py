"""
services/group_acl.py — Access Control List para mensajes de grupos.

Política:
- Bot solo procesa mensajes de grupos cuya chat_id esté en GROUP_WHITELIST.
- Tools permitidas en grupos = solo cálculo astrológico INLINE (sin DB).
- System prompt restringido: prohíbe consultar perfiles guardados, datos de
  Reamerica/Goodsten, Gmail, Calendar, VPS, etc.
- Privacy mode del bot ya filtra los mensajes que no lo mencionan, pero igual
  hacemos doble check acá.

Whitelist:
- Env var GROUP_WHITELIST con IDs separados por coma:
    GROUP_WHITELIST="-1001234567890,-1009876543210"
- Si no está seteada o el grupo no está en ella → ignorar el mensaje
  (no responder, no consumir tokens).
"""
from __future__ import annotations
import os
import re
import logging
from typing import Optional

log = logging.getLogger(__name__)


def _parse_whitelist() -> set[int]:
    raw = os.environ.get("GROUP_WHITELIST", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning(f"[group_acl] GROUP_WHITELIST item inválido: {part!r}")
    return out


# Tools permitidas en grupos:
# - calcular_carta_natal: cálculo astrológico INLINE (no toca DB).
# - enviar_voz: TTS con voz clonada — si el user mandó audio o pide voz
#   explícita, el bot tiene que poder responder con audio.
GROUP_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "calcular_carta_natal",
    "enviar_voz",
})

# Tools EXPRESAMENTE prohibidas en grupos (todas las demás también lo están,
# por whitelist; esto es solo documentación + safety net).
GROUP_BLOCKED_TOOLS: frozenset[str] = frozenset({
    # Astro DB
    "astro_guardar_perfil", "astro_ver_perfil", "astro_listar_perfiles",
    "astro_eliminar_perfil",
    # Solar/lunar usan perfil guardado — prohibidas hasta que tengan modo inline
    "calcular_retorno_solar", "calcular_retorno_lunar",
    "calcular_transitos", "analisis_triple_capa", "analisis_pista_rango",
    # Sistemas privados / Owner
    "gmail_listar", "gmail_buscar", "gmail_enviar", "gmail_borrador",
    "calendar_listar", "calendar_crear", "calendar_actualizar",
    "outlook_listar", "outlook_buscar", "outlook_enviar", "outlook_borrador",
    "sf_consultar", "sf_broker_performance",
    "vps_run", "vps_logs", "vps_status",
    "kb_buscar", "kb_listar", "ingest_pdf",
    "agente_worker", "agent_designer",
    "set_voz_activa", "test_voice",
    # Personal memory
    "remember_fact", "memory_search", "list_persons",
})


def is_group_chat_type(chat_type: Optional[str]) -> bool:
    """True si el chat es grupo o supergrupo."""
    return chat_type in ("group", "supergroup")


# Username del bot — usado para detectar menciones. Lazy-init la primera vez
# que se use; cacheado el resto del proceso.
_BOT_USERNAME: Optional[str] = None


def _bot_username() -> Optional[str]:
    """Devuelve el username del bot (sin @), cacheado. Hace getMe la primera
    vez. Retorna None si falla."""
    global _BOT_USERNAME
    if _BOT_USERNAME is not None:
        return _BOT_USERNAME
    try:
        import urllib.request, json
        token = (os.environ.get("TELEGRAM_TOKEN")
                 or os.environ.get("TG_TOKEN", ""))
        if not token:
            return None
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=5
        ) as r:
            d = json.load(r)
        _BOT_USERNAME = d.get("result", {}).get("username", "").lower() or None
        if _BOT_USERNAME:
            log.info(f"[group_acl] bot username detected: @{_BOT_USERNAME}")
    except Exception as e:
        log.warning(f"[group_acl] _bot_username fail: {e}")
    return _BOT_USERNAME


def _bot_nicknames() -> list[str]:
    """Apodos por los que el bot se puede invocar SIN @. Configurable por env.
    Default: 'cuki,cukinator'. Lowercase, sin espacios."""
    raw = os.environ.get("GROUP_BOT_NICKNAMES", "cuki,cukinator")
    return [n.strip().lower() for n in raw.split(",") if n.strip()]


def _directed_threshold() -> float:
    """Score mínimo (0–1) para considerar un mensaje dirigido al bot en grupos.
    Default 0.7. Subir = más estricto (menos falsos positivos, más falsos negativos).
    Bajar = más permisivo. Configurable por env GROUP_DIRECTED_THRESHOLD."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("GROUP_DIRECTED_THRESHOLD", "0.7"))))
    except ValueError:
        return 0.7


def score_directed_to_bot(update) -> tuple[float, list[str]]:
    """En grupos, devuelve (score 0–1, lista de señales detectadas).
    Señales y pesos:
      - Privado / sin chat → 1.0 (siempre directo)
      - @mention explícita del bot → 1.0
      - Reply a mensaje del bot → 1.0
      - Comando con sufijo @bot (/start@bot) → 1.0
      - Nickname al inicio (vocativo: 'cuki, hola') → 0.85
      - Nickname al final ('hola cuki') → 0.75
      - Nickname en cualquier parte → 0.55
      - Reply a mensaje cualquiera + bot mencionado por nick → 0.7
      - Audio sin mención (no se puede transcribir aún) → 0.0
      - Nada de lo anterior → 0.0
    """
    msg = getattr(update, "message", None) or getattr(update, "effective_message", None)
    if not msg:
        return 0.0, ["no_message"]
    chat = getattr(update, "effective_chat", None)
    if not chat or chat.type == "private":
        return 1.0, ["private_chat"]

    signals: list[str] = []
    score = 0.0
    botname = (_bot_username() or "").lower()
    text_blob = (msg.text or msg.caption or "").lower().strip()

    # 1. Reply a un mensaje del bot — señal fortísima
    reply = getattr(msg, "reply_to_message", None)
    if reply and getattr(reply, "from_user", None):
        if botname and (reply.from_user.username or "").lower() == botname:
            return 1.0, ["reply_to_bot"]

    # 2. @mention explícita del bot — señal fortísima
    if botname and f"@{botname}" in text_blob:
        return 1.0, ["explicit_at_mention"]

    # 3. Comando con sufijo @bot
    if text_blob.startswith("/") and botname and f"@{botname}" in text_blob:
        return 1.0, ["command_with_suffix"]

    # 4. Si NO hay texto (solo audio/foto/etc) y no hay reply al bot → no dirigido
    if not text_blob:
        return 0.0, ["no_text_no_reply"]

    # 5. Nicknames (vocativos sin @)
    nicks = _bot_nicknames()
    for nick in nicks:
        # Vocativo al inicio: "cuki, ...", "cukinator hola", "cuki:"
        # Patron: nick + (espacio | coma | dos puntos) al inicio
        if re.match(rf"^{re.escape(nick)}[\s,:;!.\-]", text_blob) or text_blob == nick:
            return 0.85, [f"nick_vocative_start:{nick}"]
        # Vocativo al final: "..., cuki" / "..., cuki?"
        if re.search(rf"[\s,;!.\-]{re.escape(nick)}[\s,!.?]*$", text_blob):
            score = max(score, 0.75)
            signals.append(f"nick_vocative_end:{nick}")
            continue
        # Nick en el medio (con word-boundary)
        if re.search(rf"\b{re.escape(nick)}\b", text_blob):
            score = max(score, 0.55)
            signals.append(f"nick_inline:{nick}")

    # 6. Reply a un mensaje cualquiera (no al bot) → score más bajo, requiere
    # alguna otra señal. Sin nick, no responde.
    if reply and signals:
        score = max(score, score + 0.15)
        signals.append("reply_chained")

    # 7. Pregunta directa después de un mensaje reciente del bot (<2 min)
    # → señal débil (mucho de eso es ruido entre humanos). Solo si hay nick.
    # No implementado por ahora — requiere consulta al historial. Skip.

    return min(score, 1.0), signals or ["no_signals"]


def is_directed_to_bot(update) -> bool:
    """Wrapper booleano sobre score_directed_to_bot() con threshold configurable.
    Mantiene compatibilidad con callers existentes."""
    score, signals = score_directed_to_bot(update)
    threshold = _directed_threshold()
    directed = score >= threshold
    log.info(f"[group_acl] directed_score={score:.2f} threshold={threshold:.2f} "
             f"→ directed={directed} signals={signals}")
    return directed


def is_allowed_group(chat_id: int) -> bool:
    """True si el group_id está en la whitelist."""
    return chat_id in _parse_whitelist()


def filter_tools_for_group(all_tools: list) -> list:
    """Filtra la lista de tool definitions a las permitidas en grupos.
    `all_tools` es la lista de dicts con shape {"name": ..., ...}."""
    return [t for t in all_tools if t.get("name") in GROUP_ALLOWED_TOOLS]


def group_system_suffix() -> str:
    """Suffix para el system prompt cuando estamos en un grupo."""
    return (
        "\n\n=== MODO GRUPO ===\n"
        "Estás en un grupo de Telegram con varios participantes.\n\n"
        "QUÉ PODÉS HACER acá:\n"
        " • Conversar normal con cualquier participante.\n"
        " • Calcular cartas natales INLINE: necesitás fecha (DD/MM/AAAA), hora (HH:MM) y lugar (ciudad, país) en el mismo mensaje. Tool: `calcular_carta_natal`.\n"
        " • Mandar audios con tu voz (la voz clonada COCOBASILE). Tool: `enviar_voz`. SI el user mandó un mensaje de voz, respondé con voz por default. Si te lo pide explícito ('mandá audio'), también.\n\n"
        "QUÉ NO PODÉS HACER acá (porque es un grupo público):\n"
        " • Acceder a la base de datos del bot (perfiles guardados, historial de chats privados, info personal).\n"
        " • Acceder a Reamerica, Goodsten ni clientes (Salesforce, brokers, oportunidades, primas, IBF, accounts). Si te preguntan, decí 'En el grupo no tengo acceso a esa info — es solo en privado'.\n"
        " • Acceder a Gmail, Calendar, Outlook, OneDrive, VPS, sistemas internos.\n"
        " • Guardar perfiles, notas o preferencias.\n"
        " • Calcular retorno solar/lunar (necesitan perfil guardado — solo en privado).\n\n"
        "IDENTIFICACIÓN DE QUIÉN ESCRIBE — REGLA CRÍTICA:\n"
        " • Cada mensaje del usuario en este grupo te llega prefixeado con [Nombre] al principio. Ejemplo: '[Cuki] hola amor', '[Panther] qué tal'.\n"
        " • Llamá SIEMPRE al user por ese nombre exacto. Ej: '[Cuki] muy bien' → 'Gracias, Cuki, ...'.\n"
        " • NUNCA inventes iniciales (H, P, etc), apodos, ni le pongas el nombre de OTRA persona del grupo. Si el prefijo dice 'Cuki', es Cuki, no es 'H', no es Panther.\n"
        " • Si el bloque CONTEXTO RECIENTE DEL GRUPO incluye mensajes de otros (ej. Panther, Heb), eso es solo CONTEXTO para entender referencias ('ella'/'eso'). Ese mensaje NO ES PARA VOS y NO le respondas a esa persona — solo respondé al [Nombre] del último mensaje (el actual).\n\n"
        "REGLAS DE ESTILO:\n"
        " • Respuestas BREVES (5–8 líneas máximo). Es un grupo, no monopolices.\n"
        " • Cuando vayas a mandar audio, NUNCA digas frases del tipo 'no estoy conectado al módulo de voz', 'solo veo la respuesta en texto', 'el archivo lo genera el sistema'. Esas son alucinaciones — la tool `enviar_voz` SÍ funciona en este grupo. Si te piden audio, llamala directo y listo.\n"
        " • Tono cálido y rioplatense, pero profesional — hay terceros leyendo.\n"
    )


def get_whitelist_summary() -> dict:
    """Para diagnóstico — resumen de la config actual."""
    wl = _parse_whitelist()
    return {
        "whitelist_count": len(wl),
        "whitelist": sorted(wl),
        "allowed_tools": sorted(GROUP_ALLOWED_TOOLS),
        "blocked_tools_count": len(GROUP_BLOCKED_TOOLS),
    }
