"""
services/tenants.py — Resolver multi-tenant.

Un mensaje entrante tiene un chat_id. De ese chat_id resolvemos qué tenant
está hablando (Reamerica, Díaz, etc.) y cuál es su schema en Postgres.

Si Postgres está disponible: lee la tabla shared.tenant_chat_ids.
Si no (desarrollo sin Supabase): cae a una tabla SQLite local con el mismo
shape, para que el código siga funcionando hasta que la migración complete.

El slug del tenant se usa como nombre de schema en Postgres. Por ejemplo
tenant 'reamerica' → kb_documents viven en 'reamerica.kb_documents'.
"""
import os
import sqlite3
import logging
from functools import lru_cache
from typing import Optional

from services.db import pg_available, pg_conn

log = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/home/cukibot/data/memory.db")
DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT", "reamerica")
DEFAULT_OWNER_CHAT = int(os.environ.get("OWNER_TELEGRAM_ID", "8626420783"))


# ── Fallback SQLite (usado mientras no hay Supabase) ──────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    schema_name  TEXT UNIQUE NOT NULL,
    owner_email  TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tenant_chat_ids (
    tenant_slug TEXT NOT NULL,
    chat_id     BIGINT NOT NULL,
    role        TEXT DEFAULT 'owner',
    added_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_slug, chat_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_chat ON tenant_chat_ids(chat_id);
"""


def _sqlite_conn():
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SQLITE_SCHEMA)
    # Seed del tenant base (reamerica). Tenants adicionales se agregan con
    # add_tenant(slug, display_name, owner_email) cuando lleguen — no hay
    # placeholders inventados.
    con.execute(
        "INSERT OR IGNORE INTO tenants(slug, display_name, schema_name, owner_email) VALUES (?, ?, ?, ?)",
        (DEFAULT_TENANT, "Reamerica Risk Advisors", DEFAULT_TENANT, "proyectoastroboy@gmail.com"),
    )
    con.execute(
        "INSERT OR IGNORE INTO tenant_chat_ids(tenant_slug, chat_id, role) VALUES (?, ?, ?)",
        (DEFAULT_TENANT, DEFAULT_OWNER_CHAT, "owner"),
    )
    con.commit()
    return con


# ── API pública ───────────────────────────────────────────────────────────

@lru_cache(maxsize=1024)
def resolve_tenant(chat_id: int) -> str:
    """Devuelve el slug del tenant para un chat_id. Cachea en memoria."""
    if pg_available():
        try:
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT tenant_slug FROM shared.tenant_chat_ids WHERE chat_id = %s",
                        (chat_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        return row[0]
        except Exception as e:
            log.warning(f"resolve_tenant(pg) falló: {e}, uso SQLite fallback")

    # Fallback SQLite
    try:
        con = _sqlite_conn()
        row = con.execute(
            "SELECT tenant_slug FROM tenant_chat_ids WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        con.close()
        if row:
            return row[0]
    except Exception as e:
        log.warning(f"resolve_tenant(sqlite) falló: {e}")

    # Default
    return DEFAULT_TENANT


def tenant_schema(slug: str) -> str:
    """El schema SQL donde viven las tablas del tenant."""
    # Validación: solo alfanumérico + _ (previene SQL injection en format())
    if not slug or not all(c.isalnum() or c == "_" for c in slug):
        raise ValueError(f"Slug inválido: {slug!r}")
    return slug


def list_tenants() -> list:
    """Lista todos los tenants registrados."""
    if pg_available():
        try:
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT slug, display_name, schema_name, owner_email FROM shared.tenants ORDER BY slug"
                    )
                    return [
                        {"slug": r[0], "name": r[1], "schema": r[2], "email": r[3]}
                        for r in cur.fetchall()
                    ]
        except Exception as e:
            log.warning(f"list_tenants(pg) falló: {e}")
    con = _sqlite_conn()
    rows = con.execute(
        "SELECT slug, display_name, schema_name, owner_email FROM tenants ORDER BY slug"
    ).fetchall()
    con.close()
    return [{"slug": r[0], "name": r[1], "schema": r[2], "email": r[3]} for r in rows]


def add_tenant(slug: str, display_name: str, owner_email: Optional[str] = None) -> dict:
    """Agrega un tenant nuevo. En Postgres también crea su schema."""
    if not slug.isalnum() and not all(c.isalnum() or c == "_" for c in slug):
        raise ValueError(f"Slug inválido (alfanumérico y _ solo): {slug!r}")

    if pg_available():
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO shared.tenants(slug, display_name, schema_name, owner_email) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (slug) DO NOTHING",
                    (slug, display_name, slug, owner_email),
                )
                cur.execute("SELECT shared.create_tenant_schema(%s)", (slug,))

    # Siempre también en SQLite (fallback + dev)
    con = _sqlite_conn()
    con.execute(
        "INSERT OR IGNORE INTO tenants(slug, display_name, schema_name, owner_email) VALUES (?, ?, ?, ?)",
        (slug, display_name, slug, owner_email),
    )
    con.commit()
    con.close()
    resolve_tenant.cache_clear()
    return {"slug": slug, "name": display_name, "schema": slug}


def link_chat_to_tenant(chat_id: int, tenant_slug: str, role: str = "member") -> None:
    """Asocia un chat_id con un tenant."""
    if pg_available():
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO shared.tenant_chat_ids(tenant_slug, chat_id, role) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (tenant_slug, chat_id, role),
                )
    con = _sqlite_conn()
    con.execute(
        "INSERT OR REPLACE INTO tenant_chat_ids(tenant_slug, chat_id, role) VALUES (?, ?, ?)",
        (tenant_slug, chat_id, role),
    )
    con.commit()
    con.close()
    resolve_tenant.cache_clear()
