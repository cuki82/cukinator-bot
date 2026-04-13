"""
agent_ops.py — Sistema operativo del agente: changelog, estado, ejecución.
Permite control remoto desde Telegram como panel de administración.
"""

import sqlite3
import json
import datetime
import os
import hashlib

DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

# ── Schema ──────────────────────────────────────────────────────────────────────
SCHEMA_OPS = """
CREATE TABLE IF NOT EXISTS agent_changelog (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           DATETIME DEFAULT CURRENT_TIMESTAMP,
    instruction  TEXT NOT NULL,
    intent       TEXT,
    plan         TEXT,
    action       TEXT NOT NULL,
    result       TEXT,
    files_changed TEXT,
    status       TEXT DEFAULT 'done',
    requires     TEXT,
    chat_id      INTEGER
);

CREATE TABLE IF NOT EXISTS agent_secrets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_name     TEXT UNIQUE NOT NULL,
    key_hash     TEXT,
    service      TEXT,
    description  TEXT,
    masked_value TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_skills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT UNIQUE NOT NULL,
    description  TEXT,
    trigger_phrases TEXT,
    tool_name    TEXT,
    config_json  TEXT,
    status       TEXT DEFAULT 'active',
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def init_agent_ops(db_path: str = None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_OPS)
    con.commit()
    con.close()


# ── Changelog ──────────────────────────────────────────────────────────────────
def log_change(instruction: str, action: str, result: str,
               intent: str = None, plan: str = None,
               files_changed: list = None, status: str = "done",
               requires: str = None, chat_id: int = None,
               db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.execute("""
        INSERT INTO agent_changelog
        (instruction, intent, plan, action, result, files_changed, status, requires, chat_id)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (instruction, intent, plan, action, result,
           json.dumps(files_changed or []), status, requires, chat_id))
    con.commit()
    con.close()


def get_changelog(limit: int = 10, db_path: str = None) -> list:
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    rows = con.execute("""
        SELECT ts, instruction, action, result, status, requires, files_changed
        FROM agent_changelog ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    con.close()
    return [{
        "ts": r[0][:16], "instruction": r[1][:80],
        "action": r[2][:100], "result": r[3][:100] if r[3] else "",
        "status": r[4], "requires": r[5],
        "files": json.loads(r[6]) if r[6] else []
    } for r in rows]


# ── Secrets ────────────────────────────────────────────────────────────────────
def store_secret(key_name: str, value: str, service: str = None,
                 description: str = None, db_path: str = None):
    path = db_path or DB_PATH
    # Guardar el valor real en variable de entorno del proceso (no en DB)
    os.environ[key_name.upper()] = value
    # En DB solo guardar hash y valor enmascarado
    key_hash = hashlib.sha256(value.encode()).hexdigest()[:16]
    masked = value[:4] + "***" + value[-4:] if len(value) > 8 else "***"
    con = sqlite3.connect(path)
    con.execute("""
        INSERT OR REPLACE INTO agent_secrets
        (key_name, key_hash, service, description, masked_value)
        VALUES (?,?,?,?,?)
    """, (key_name, key_hash, service, description, masked))
    con.commit()
    con.close()
    return masked


def list_secrets(db_path: str = None) -> list:
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT key_name, service, description, masked_value, created_at FROM agent_secrets ORDER BY id DESC"
    ).fetchall()
    con.close()
    return [{"key": r[0], "service": r[1], "desc": r[2], "value": r[3], "ts": r[4][:16]} for r in rows]


# ── Skills ─────────────────────────────────────────────────────────────────────
def register_skill(name: str, description: str, trigger_phrases: list,
                   tool_name: str = None, config: dict = None, db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.execute("""
        INSERT OR REPLACE INTO agent_skills
        (name, description, trigger_phrases, tool_name, config_json, updated_at)
        VALUES (?,?,?,?,?,?)
    """, (name, description, json.dumps(trigger_phrases),
           tool_name, json.dumps(config or {}), datetime.datetime.utcnow().isoformat()))
    con.commit()
    con.close()


def list_skills(db_path: str = None) -> list:
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT name, description, trigger_phrases, status, updated_at FROM agent_skills ORDER BY id DESC"
    ).fetchall()
    con.close()
    return [{
        "name": r[0], "description": r[1],
        "triggers": json.loads(r[2]) if r[2] else [],
        "status": r[3], "updated": r[4][:16]
    } for r in rows]


# ── Estado del sistema ─────────────────────────────────────────────────────────
def get_agent_status(db_path: str = None) -> dict:
    path = db_path or DB_PATH
    con = sqlite3.connect(path)

    # Contar elementos en cada tabla
    tables = {
        "mensajes":    "messages",
        "documentos":  "ri_documents",
        "conceptos":   "ri_concepts",
        "configs":     "configurations",
        "skills":      "agent_skills",
        "secrets":     "agent_secrets",
        "changelog":   "agent_changelog",
        "perfiles_astro": "perfiles_astro",
    }
    counts = {}
    for label, table in tables.items():
        try:
            counts[label] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            counts[label] = "n/a"
    con.close()

    # Últimos cambios
    last_changes = get_changelog(3, path)

    return {
        "db_path": path,
        "db_size_kb": round(os.path.getsize(path) / 1024, 1) if os.path.exists(path) else 0,
        "counts": counts,
        "last_changes": last_changes,
        "env_keys_set": [k for k in os.environ if k in (
            "TELEGRAM_TOKEN", "ANTHROPIC_KEY", "GAS_URL",
            "ELEVENLABS_KEY", "ELEVENLABS_VOICE", "DB_PATH"
        )],
    }


# ── Clasificador de intención ──────────────────────────────────────────────────
INTENT_KEYWORDS = {
    "config":       ["configurá", "configura", "guardá", "guarda", "dejá", "deja",
                     "setear", "activar", "activa", "desactivar", "cambiá", "cambia"],
    "skill":        ["skill", "habilidad", "capacidad", "función", "agrega", "agregá",
                     "creá", "crea", "armá", "arma", "nuevo skill", "nueva función"],
    "integration":  ["api", "integración", "conectate", "conectá", "webhook",
                     "endpoint", "token", "key", "credencial"],
    "menu":         ["menú", "menu", "comando", "submenú", "opción", "botón"],
    "debug":        ["error", "bug", "falla", "roto", "diagnóstico", "debug", "logs"],
    "status":       ["estado", "status", "qué tengo", "qué hay", "mostrame", "listá"],
    "code":         ["código", "función", "clase", "módulo", "archivo", "script",
                     "implementá", "implementa", "program"],
    "deploy":       ["deploy", "reiniciá", "reinicia", "actualizar", "subir", "github", "push"],
    "secret":       ["api key", "token", "password", "contraseña", "secreto", "credencial"],
}

def classify_intent(text: str) -> str:
    text_lower = text.lower()
    scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        scores[intent] = sum(1 for kw in keywords if kw in text_lower)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "conversation"


if __name__ == "__main__":
    import os; os.environ["DB_PATH"] = "/opt/cukinator/memory.db"
    init_agent_ops()
    print("Agent ops OK:", get_agent_status())
