"""
services/intent_state.py — Estado conversacional del Intent Router.

Permite que una respuesta del bot que haga una pregunta confirmable
("¿querés que pase al worker?") deje pending un intent + accion.
Si el siguiente mensaje del user es una confirmacion corta ("si",
"dale", "mandasela"), el router HEREDA ese intent en vez de
re-clasificar y caer en conversational.

TTL: 10 min por defecto — despues expira solo.

Schema:
    pending_intents (
        chat_id INTEGER PRIMARY KEY,
        intent  TEXT NOT NULL,
        action  TEXT NOT NULL,
        created INTEGER NOT NULL,
        ttl_s   INTEGER NOT NULL DEFAULT 600
    )

Tambien expone telemetria — cada clasificacion se loguea en intent_log
para diagnostico de misclassifications.
"""
from __future__ import annotations
import os
import sqlite3
import time
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", os.path.expanduser("~/data/memory.db"))


def _con():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _ensure_schema():
    with _con() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_intents (
                chat_id  INTEGER PRIMARY KEY,
                intent   TEXT NOT NULL,
                action   TEXT NOT NULL,
                created  INTEGER NOT NULL,
                ttl_s    INTEGER NOT NULL DEFAULT 600
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS intent_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                chat_id   INTEGER,
                user_text TEXT NOT NULL,
                intent    TEXT NOT NULL,
                layer     TEXT NOT NULL,        -- 'pending'|'rule'|'embed'|'llm'
                confidence REAL,                -- 0..1 si la layer la calcula
                duration_ms INTEGER,
                metadata  TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_intent_log_ts ON intent_log(ts DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_intent_log_chat ON intent_log(chat_id, ts DESC)")
        c.commit()


_ensure_schema()


@dataclass
class PendingIntent:
    chat_id: int
    intent: str
    action: str
    created: int
    ttl_s: int

    @property
    def expired(self) -> bool:
        return (time.time() - self.created) > self.ttl_s


# ─── API publica ───────────────────────────────────────────────────────

def remember_pending(chat_id: int, intent: str, action: str, ttl_s: int = 600) -> None:
    """Guarda una accion pendiente. Si ya habia una, la sobreescribe."""
    if not chat_id or not intent or not action:
        return
    try:
        with _con() as c:
            c.execute("""
                INSERT OR REPLACE INTO pending_intents
                (chat_id, intent, action, created, ttl_s)
                VALUES (?, ?, ?, ?, ?)
            """, (chat_id, intent.strip().lower(), action.strip(),
                  int(time.time()), ttl_s))
            c.commit()
        log.info(f"[intent_state] pending set chat={chat_id} intent={intent} action={action[:60]!r}")
    except Exception as e:
        log.warning(f"[intent_state] remember_pending fail: {e}")


def get_pending(chat_id: int) -> Optional[PendingIntent]:
    """Devuelve la accion pendiente si todavia esta vigente. La borra si expiro."""
    try:
        with _con() as c:
            r = c.execute(
                "SELECT chat_id, intent, action, created, ttl_s FROM pending_intents WHERE chat_id=?",
                (chat_id,)
            ).fetchone()
        if not r:
            return None
        p = PendingIntent(*r)
        if p.expired:
            clear_pending(chat_id)
            return None
        return p
    except Exception as e:
        log.warning(f"[intent_state] get_pending fail: {e}")
        return None


def clear_pending(chat_id: int) -> None:
    try:
        with _con() as c:
            c.execute("DELETE FROM pending_intents WHERE chat_id=?", (chat_id,))
            c.commit()
    except Exception as e:
        log.warning(f"[intent_state] clear_pending fail: {e}")


# ─── Telemetria ─────────────────────────────────────────────────────────

def log_classification(
    chat_id: Optional[int],
    user_text: str,
    intent: str,
    layer: str,
    confidence: Optional[float] = None,
    duration_ms: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Loguea cada clasificacion para diagnostico futuro."""
    import json
    try:
        with _con() as c:
            c.execute("""
                INSERT INTO intent_log
                (chat_id, user_text, intent, layer, confidence, duration_ms, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id, user_text[:500], intent, layer,
                confidence, duration_ms,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
            ))
            c.commit()
    except Exception as e:
        log.debug(f"[intent_state] log_classification fail: {e}")


# ─── Detector de confirmacion corta ────────────────────────────────────

# Frases que son SOLO una confirmacion (no agregan info).
# Si el user manda esto SIN un pending activo, no inferimos nada.
_CONFIRMATION_TOKENS = {
    "si", "sí", "ok", "okay", "dale", "listo", "perfecto", "buenisimo", "buenísimo",
    "go", "vamos", "yes", "yep", "yeah", "claro", "obvio",
    "hacelo", "hazlo", "andá", "anda", "andale", "avanza", "avanzá",
    "configuralo", "configurá", "configura",
    "mandasela", "mandásela", "mandala", "mandalo",
    "procede", "procedé", "continua", "continuá", "seguí", "segui",
    "implementalo", "implementá", "implementa",
    "armalo", "armá", "arma",
    "hace", "hacé", "haz",
    "approvado", "aprobado", "ok dale", "si dale", "dale si", "sí, hacelo", "sí hacelo",
    "sí, dale", "sí dale", "dale, hacelo", "ok hacelo",
    "perfecto, dale", "listo, dale",
}

_CONFIRMATION_REGEX_HINTS = [
    r"^s[ií][\s,!.]*$",
    r"^(?:s[ií][\s,]+)?(?:dale|hacelo|listo|perfecto|ok|mandasela|mand[aá]sela)[\s!.]*$",
    r"^(?:s[ií][\s,]+)?ar[ranma|m[aá]l[oa]|configur[aá]l?[oa]?)\b",
    r"^(?:s[ií][\s,]+)?(?:procede|contin[uú]a|sig[ua][ei])[\s!.]*$",
    r"^(?:s[ií][\s,]+)?(?:implement[aá]l?[oa]?|avanzal?[oa]?|haz?l?[oa]?)\b",
]


def is_short_confirmation(text: str) -> bool:
    """True si el mensaje del user es una confirmacion corta sin info adicional.
    Reglas:
    - Length <= 6 palabras
    - Tokens solo de confirmacion, o matchea pattern confirmacion-puro
    """
    import re
    t = (text or "").strip().lower()
    if not t:
        return False
    # Normalizar puntuacion al final
    t_clean = re.sub(r"[!.?\s,]+$", "", t)
    if t_clean in _CONFIRMATION_TOKENS:
        return True
    words = t_clean.split()
    if len(words) > 6:
        return False
    # Todos los tokens son de confirmacion
    if words and all(w in _CONFIRMATION_TOKENS for w in words):
        return True
    # Regex de patrones cortos
    for pat in _CONFIRMATION_REGEX_HINTS:
        try:
            if re.match(pat, t):
                return True
        except re.error:
            continue
    return False


# ─── Parser/Emisor del tag [PENDING:intent:action] ──────────────────────

# El LLM puede emitir un tag al final de su respuesta para guardar
# una accion pendiente que el user puede confirmar despues.
# Formato: [PENDING:coding:configurar bot en grupo "Humanos vs bots"]
# El bot core extrae el tag, lo guarda en pending_intents, y NO lo
# muestra al user en el mensaje final.

import re as _re
_PENDING_TAG = _re.compile(
    r"\[PENDING\s*:\s*([a-z_]+)\s*:\s*([^\]]+)\]",
    flags=_re.IGNORECASE,
)


def extract_pending_tag(bot_response: str) -> tuple[str, Optional[tuple[str, str]]]:
    """Busca un tag [PENDING:intent:action] en la respuesta del LLM.
    Devuelve (respuesta_sin_tag, (intent, action) | None).
    """
    m = _PENDING_TAG.search(bot_response or "")
    if not m:
        return bot_response, None
    intent = m.group(1).strip().lower()
    action = m.group(2).strip()
    cleaned = _PENDING_TAG.sub("", bot_response).strip()
    return cleaned, (intent, action)


# ─── Helper combinado ───────────────────────────────────────────────────

def resolve_with_pending(chat_id: int, user_text: str) -> Optional[PendingIntent]:
    """Si hay pending vigente Y user mando confirmacion corta, devuelve el
    PendingIntent. Sino None."""
    if not is_short_confirmation(user_text):
        return None
    p = get_pending(chat_id)
    if not p:
        return None
    log.info(f"[intent_state] confirmation matched pending: chat={chat_id} → {p.intent} '{p.action[:60]}'")
    return p
