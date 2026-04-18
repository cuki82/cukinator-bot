"""
services/db.py — Acceso a Postgres (Supabase) con pool de conexiones.

Fuente de verdad para KB, memoria, config de cada tenant. Requiere la
variable de entorno SUPABASE_DB_URL o DATABASE_URL (formato
postgresql://user:pass@host:port/db).

Uso:
    from services.db import pg_conn, pg_available

    if pg_available():
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1")
                print(cur.fetchone())

El módulo es lazy: la pool se crea al primer uso. Si no hay URL o psycopg2
no está instalado, pg_available() devuelve False y el caller cae a SQLite
(transicional — cuando la migración termine se quita el fallback).
"""
import os
import logging
import threading
from contextlib import contextmanager
from typing import Optional, Generator

log = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    from psycopg2 import pool as _pg_pool_mod
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

_pool: Optional[object] = None
_pool_lock = threading.Lock()
_init_tried = False


def _pg_url() -> Optional[str]:
    return os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")


def _init_pool() -> Optional[object]:
    global _pool, _init_tried
    if _pool is not None or _init_tried:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        _init_tried = True
        if not _HAS_PG:
            log.warning("psycopg2 no instalado — Postgres deshabilitado")
            return None
        url = _pg_url()
        if not url:
            log.info("SUPABASE_DB_URL/DATABASE_URL no seteado — Postgres deshabilitado")
            return None
        try:
            _pool = _pg_pool_mod.ThreadedConnectionPool(
                minconn=1, maxconn=10, dsn=url,
                connect_timeout=10, application_name="cukinator-bot",
            )
            log.info("Postgres pool listo (minconn=1, maxconn=10)")
        except Exception as e:
            log.error(f"No pude abrir pool Postgres: {e}")
            _pool = None
    return _pool


def pg_available() -> bool:
    """True si hay pool de Postgres utilizable."""
    return _init_pool() is not None


@contextmanager
def pg_conn() -> Generator:
    """Context manager: toma una conexión del pool, la devuelve al salir.

    Uso:
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1")

    Raises RuntimeError si no hay pool disponible (chequear pg_available()).
    """
    p = _init_pool()
    if p is None:
        raise RuntimeError("Postgres no configurado — setear SUPABASE_DB_URL")
    con = p.getconn()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        p.putconn(con)


def ping() -> bool:
    """Chequea que la conexión anda. Útil al bootear."""
    if not pg_available():
        return False
    try:
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone()[0] == 1
    except Exception as e:
        log.error(f"pg ping falló: {e}")
        return False


def warmup() -> None:
    """Abre una conexión al startup para evitar el cold-start de la primera
    query. Especialmente útil con Supabase (Supavisor pooler puede tardar
    ~200ms en el primer handshake). Llamar al arranque del bot/worker."""
    if ping():
        log.info("Postgres warmup OK")
    else:
        log.info("Postgres warmup: no disponible (SQLite fallback)")
