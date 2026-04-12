"""
memory_store.py — Sistema de memoria persistente completa para Railway.

Arquitectura:
- sessions: agrupan mensajes por sesión (gap > 2h = nueva sesión)
- messages: cada intercambio user/assistant con metadata
- memory_index: resúmenes, topics, entities por sesión
- Búsqueda por tags + full-text SQLite FTS5
"""

import sqlite3
import json
import datetime
import os
import hashlib

DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

# ── Schema ──────────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT    PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    start_time   DATETIME NOT NULL,
    end_time     DATETIME,
    summary      TEXT,
    summary_tech TEXT,
    topics       TEXT,
    entities     TEXT,
    tags         TEXT,
    msg_count    INTEGER DEFAULT 0,
    summarized   INTEGER DEFAULT 0,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    session_id   TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    content_hash TEXT,
    metadata     TEXT,
    tags         TEXT,
    timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_index (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    session_id   TEXT,
    msg_id       INTEGER,
    type         TEXT NOT NULL,
    title        TEXT,
    content      TEXT NOT NULL,
    entities     TEXT,
    topics       TEXT,
    tags         TEXT,
    relevance    REAL DEFAULT 1.0,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS person_memory (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    name         TEXT    NOT NULL,
    facts        TEXT,
    last_seen    DATETIME,
    tags         TEXT,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(chat_id, name)
);

CREATE INDEX IF NOT EXISTS idx_msg_chat   ON messages(chat_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_msg_sess   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_chat   ON memory_index(chat_id);
CREATE INDEX IF NOT EXISTS idx_mem_tags   ON memory_index(tags);
CREATE INDEX IF NOT EXISTS idx_sess_chat  ON sessions(chat_id);
CREATE INDEX IF NOT EXISTS idx_person     ON person_memory(chat_id, name);
"""

SESSION_GAP_HOURS = 2  # gap > 2h = nueva sesión
SUMMARIZE_EVERY   = 20  # resumir cada N mensajes


def get_conn(db_path: str = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def init_memory_store(db_path: str = None):
    con = get_conn(db_path)

    # Crear tablas nuevas (no tocar messages si ya existe)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id   TEXT    PRIMARY KEY,
            chat_id      INTEGER NOT NULL,
            start_time   DATETIME NOT NULL,
            end_time     DATETIME,
            summary      TEXT,
            summary_tech TEXT,
            topics       TEXT,
            entities     TEXT,
            tags         TEXT,
            msg_count    INTEGER DEFAULT 0,
            summarized   INTEGER DEFAULT 0,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS memory_index (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            session_id   TEXT,
            msg_id       INTEGER,
            type         TEXT NOT NULL,
            title        TEXT,
            content      TEXT NOT NULL,
            entities     TEXT,
            topics       TEXT,
            tags         TEXT,
            relevance    REAL DEFAULT 1.0,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS person_memory (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            name         TEXT    NOT NULL,
            facts        TEXT,
            last_seen    DATETIME,
            tags         TEXT,
            updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, name)
        );

        CREATE INDEX IF NOT EXISTS idx_sess_chat  ON sessions(chat_id);
        CREATE INDEX IF NOT EXISTS idx_mem_chat   ON memory_index(chat_id);
        CREATE INDEX IF NOT EXISTS idx_mem_tags   ON memory_index(tags);
        CREATE INDEX IF NOT EXISTS idx_person     ON person_memory(chat_id, name);
    """)

    # Migrar tabla messages: agregar columnas nuevas si no existen
    existing_cols = [r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()]

    migrations = [
        ("session_id",   "ALTER TABLE messages ADD COLUMN session_id TEXT DEFAULT 'legacy'"),
        ("content_hash", "ALTER TABLE messages ADD COLUMN content_hash TEXT"),
        ("metadata",     "ALTER TABLE messages ADD COLUMN metadata TEXT"),
        ("tags",         "ALTER TABLE messages ADD COLUMN tags TEXT"),
        ("timestamp",    "ALTER TABLE messages ADD COLUMN timestamp DATETIME"),
    ]
    for col, sql in migrations:
        if col not in existing_cols:
            con.execute(sql)

    # Rellenar timestamp desde ts si existe
    if "ts" in existing_cols and "timestamp" in [r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()]:
        con.execute("UPDATE messages SET timestamp = ts WHERE timestamp IS NULL")

    # Crear índices de messages si no existen
    con.executescript("""
        CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages(chat_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_msg_sess ON messages(session_id);
    """)

    con.commit()
    con.close()


# ── Sesiones ────────────────────────────────────────────────────────────────────
def get_or_create_session(chat_id: int, db_path: str = None) -> str:
    """
    Retorna session_id activa o crea una nueva si el último mensaje
    fue hace más de SESSION_GAP_HOURS horas.
    """
    con = get_conn(db_path)
    now = datetime.datetime.utcnow()
    threshold = now - datetime.timedelta(hours=SESSION_GAP_HOURS)

    row = con.execute(
        "SELECT session_id, end_time FROM sessions WHERE chat_id=? ORDER BY start_time DESC LIMIT 1",
        (chat_id,)
    ).fetchone()

    if row:
        last_end = row["end_time"] or row["session_id"][:19]
        try:
            last_dt = datetime.datetime.fromisoformat(last_end)
        except Exception:
            last_dt = threshold - datetime.timedelta(hours=1)

        if last_dt >= threshold:
            con.close()
            return row["session_id"]

    # Nueva sesión
    session_id = f"{chat_id}_{now.strftime('%Y%m%d_%H%M%S')}"
    con.execute(
        "INSERT INTO sessions (session_id, chat_id, start_time, end_time) VALUES (?,?,?,?)",
        (session_id, chat_id, now.isoformat(), now.isoformat())
    )
    con.commit()
    con.close()
    return session_id


def update_session_end(session_id: str, db_path: str = None):
    con = get_conn(db_path)
    con.execute(
        "UPDATE sessions SET end_time=?, msg_count=msg_count+1 WHERE session_id=?",
        (datetime.datetime.utcnow().isoformat(), session_id)
    )
    con.commit()
    con.close()


# ── Guardado de mensajes ─────────────────────────────────────────────────────────
def save_message_full(chat_id: int, role: str, content: str,
                      metadata: dict = None, tags: list = None,
                      db_path: str = None) -> tuple:
    """
    Guarda un mensaje completo con session_id, hash, metadata y tags.
    Retorna (msg_id, session_id).
    """
    session_id = get_or_create_session(chat_id, db_path)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    con = get_conn(db_path)

    # Evitar duplicados exactos
    existing = con.execute(
        "SELECT id FROM messages WHERE chat_id=? AND content_hash=? AND role=?",
        (chat_id, content_hash, role)
    ).fetchone()
    if existing:
        con.close()
        return existing["id"], session_id

    cursor = con.execute(
        "INSERT INTO messages (chat_id, session_id, role, content, content_hash, metadata, tags) VALUES (?,?,?,?,?,?,?)",
        (chat_id, session_id, role, content, content_hash,
         json.dumps(metadata or {}),
         json.dumps(tags or []))
    )
    msg_id = cursor.lastrowid
    con.commit()
    con.close()

    update_session_end(session_id, db_path)
    return msg_id, session_id


# ── Historial ────────────────────────────────────────────────────────────────────
def get_history_full(chat_id: int, limit: int = 20, db_path: str = None) -> list:
    """Retorna los últimos N mensajes del chat (formato para Claude API)."""
    con = get_conn(db_path)
    rows = con.execute(
        "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    con.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def get_sessions(chat_id: int, limit: int = 10, db_path: str = None) -> list:
    con = get_conn(db_path)
    rows = con.execute(
        "SELECT session_id, start_time, end_time, summary, topics, tags, msg_count FROM sessions WHERE chat_id=? ORDER BY start_time DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ── Memoria semántica ────────────────────────────────────────────────────────────
def save_memory_fact(chat_id: int, content: str, fact_type: str = "fact",
                     title: str = "", entities: list = None,
                     topics: list = None, tags: list = None,
                     session_id: str = None, db_path: str = None) -> int:
    con = get_conn(db_path)
    cursor = con.execute(
        "INSERT INTO memory_index (chat_id, session_id, type, title, content, entities, topics, tags) VALUES (?,?,?,?,?,?,?,?)",
        (chat_id, session_id, fact_type, title, content,
         json.dumps(entities or []),
         json.dumps(topics or []),
         json.dumps(tags or []))
    )
    fact_id = cursor.lastrowid
    con.commit()
    con.close()
    return fact_id


def search_memory(chat_id: int, query: str, limit: int = 10, db_path: str = None) -> list:
    """
    Búsqueda en memoria por keywords en content, title, topics, entities, tags.
    """
    con = get_conn(db_path)
    words = [w.lower().strip() for w in query.split() if len(w) > 2]

    results = []
    seen = set()

    for word in words[:5]:  # máximo 5 keywords
        rows = con.execute(
            """SELECT m.id, m.type, m.title, m.content, m.topics, m.tags, m.created_at,
                      s.summary as session_summary
               FROM memory_index m
               LEFT JOIN sessions s ON m.session_id = s.session_id
               WHERE m.chat_id=?
               AND (LOWER(m.content) LIKE ? OR LOWER(m.title) LIKE ?
                    OR LOWER(m.topics) LIKE ? OR LOWER(m.tags) LIKE ?)
               ORDER BY m.created_at DESC LIMIT ?""",
            (chat_id, f"%{word}%", f"%{word}%", f"%{word}%", f"%{word}%", limit)
        ).fetchall()
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                results.append(dict(r))

    # También buscar en mensajes recientes
    msg_rows = con.execute(
        """SELECT id, role, content, timestamp FROM messages
           WHERE chat_id=? AND LOWER(content) LIKE ?
           ORDER BY id DESC LIMIT ?""",
        (chat_id, f"%{query.lower()[:30]}%", limit)
    ).fetchall()

    con.close()
    return {"memory_facts": results[:limit], "messages": [dict(r) for r in msg_rows[:5]]}


def search_person_memory(chat_id: int, name: str, db_path: str = None) -> dict:
    """Recupera todo lo que se sabe de una persona."""
    con = get_conn(db_path)

    # Buscar en person_memory
    person = con.execute(
        "SELECT * FROM person_memory WHERE chat_id=? AND LOWER(name) LIKE ?",
        (chat_id, f"%{name.lower()}%")
    ).fetchone()

    # Buscar menciones en mensajes
    mentions = con.execute(
        """SELECT role, content, timestamp FROM messages
           WHERE chat_id=? AND LOWER(content) LIKE ?
           ORDER BY id DESC LIMIT 10""",
        (chat_id, f"%{name.lower()}%")
    ).fetchall()

    # Buscar en memory_index
    mem = con.execute(
        """SELECT type, title, content, created_at FROM memory_index
           WHERE chat_id=? AND (LOWER(entities) LIKE ? OR LOWER(content) LIKE ?)
           ORDER BY created_at DESC LIMIT 10""",
        (chat_id, f"%{name.lower()}%", f"%{name.lower()}%")
    ).fetchall()

    con.close()
    return {
        "person_record": dict(person) if person else None,
        "message_mentions": [dict(r) for r in mentions],
        "memory_facts": [dict(r) for r in mem],
    }


def upsert_person_memory(chat_id: int, name: str, facts: dict, tags: list = None, db_path: str = None):
    """Guarda o actualiza lo que se sabe de una persona."""
    con = get_conn(db_path)
    existing = con.execute(
        "SELECT facts FROM person_memory WHERE chat_id=? AND LOWER(name)=?",
        (chat_id, name.lower())
    ).fetchone()

    now = datetime.datetime.utcnow().isoformat()
    if existing:
        existing_facts = json.loads(existing["facts"] or "{}")
        existing_facts.update(facts)
        con.execute(
            "UPDATE person_memory SET facts=?, tags=?, last_seen=?, updated_at=? WHERE chat_id=? AND LOWER(name)=?",
            (json.dumps(existing_facts, ensure_ascii=False), json.dumps(tags or []), now, now, chat_id, name.lower())
        )
    else:
        con.execute(
            "INSERT INTO person_memory (chat_id, name, facts, tags, last_seen) VALUES (?,?,?,?,?)",
            (chat_id, name, json.dumps(facts, ensure_ascii=False), json.dumps(tags or []), now)
        )
    con.commit()
    con.close()


# ── Resumen automático con Claude ────────────────────────────────────────────────
def needs_summary(session_id: str, db_path: str = None) -> bool:
    con = get_conn(db_path)
    row = con.execute(
        "SELECT msg_count, summarized FROM sessions WHERE session_id=?",
        (session_id,)
    ).fetchone()
    con.close()
    if not row:
        return False
    return row["msg_count"] >= SUMMARIZE_EVERY and not row["summarized"]


def save_session_summary(session_id: str, summary: str, topics: list,
                         entities: list, tags: list, db_path: str = None):
    con = get_conn(db_path)
    con.execute(
        "UPDATE sessions SET summary=?, topics=?, entities=?, tags=?, summarized=1 WHERE session_id=?",
        (summary, json.dumps(topics), json.dumps(entities), json.dumps(tags), session_id)
    )
    con.commit()
    con.close()


def generate_summary_prompt(chat_id: int, session_id: str, db_path: str = None) -> str:
    """Construye el prompt para que Claude genere el resumen de la sesión."""
    con = get_conn(db_path)
    rows = con.execute(
        "SELECT role, content FROM messages WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()
    con.close()

    conversation = "\n".join([f"{r['role'].upper()}: {r['content'][:300]}" for r in rows])
    return f"""Analizá esta conversación y respondé SOLO con un JSON con este formato exacto:
{{
  "summary": "resumen de 2-3 oraciones de qué se habló",
  "topics": ["tema1", "tema2"],
  "entities": ["persona1", "lugar1", "concepto1"],
  "tags": ["tag1", "tag2"]
}}

Conversación:
{conversation[:3000]}"""


# ── Limpieza de memoria ──────────────────────────────────────────────────────────
def clear_chat_history(chat_id: int, db_path: str = None):
    con = get_conn(db_path)
    con.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    con.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()


def get_memory_stats(chat_id: int, db_path: str = None) -> dict:
    con = get_conn(db_path)
    msgs = con.execute("SELECT COUNT(*) as c FROM messages WHERE chat_id=?", (chat_id,)).fetchone()["c"]
    sess = con.execute("SELECT COUNT(*) as c FROM sessions WHERE chat_id=?", (chat_id,)).fetchone()["c"]
    facts = con.execute("SELECT COUNT(*) as c FROM memory_index WHERE chat_id=?", (chat_id,)).fetchone()["c"]
    persons = con.execute("SELECT COUNT(*) as c FROM person_memory WHERE chat_id=?", (chat_id,)).fetchone()["c"]
    con.close()
    return {"messages": msgs, "sessions": sess, "memory_facts": facts, "persons": persons}


if __name__ == "__main__":
    import os
    os.environ["DB_PATH"] = "/opt/cukinator/memory.db"
    init_memory_store()
    print("Memory store inicializado OK")
    stats = get_memory_stats(0)
    print(f"Stats: {stats}")
