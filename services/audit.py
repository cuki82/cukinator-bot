"""
services/audit.py — logger de audit_events cross-tenant.

Cada acción relevante (tool invocado, escritura al vault, guardado de
perfil astro, crash, etc.) queda registrada en shared.audit_events para
debugging + compliance. Fallo silente si no hay Postgres.

Uso:
    from services.audit import log_event
    log_event(
        action="tool_invoke",
        resource="calcular_transitos",
        tenant="reamerica", chat_id=8626420783, actor="bot",
        details={"target": "natal", "elapsed_ms": 120},
    )
"""
import json
import logging
import threading
from typing import Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()


def log_event(action: str, resource: Optional[str] = None,
              tenant: Optional[str] = None, chat_id: Optional[int] = None,
              actor: str = "bot", details: Optional[dict] = None) -> None:
    """Registra un evento en shared.audit_events. Fallo silente si no hay PG."""
    try:
        from services.db import pg_available, pg_conn
    except Exception:
        return
    if not pg_available():
        return
    try:
        with _lock:
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute("""
                        INSERT INTO shared.audit_events
                            (tenant_slug, chat_id, actor, action, resource, details)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """, (tenant, chat_id, actor, action, resource, json.dumps(details or {})))
    except Exception as e:
        log.debug(f"audit log_event skip: {e}")


def recent(chat_id: Optional[int] = None, action: Optional[str] = None,
           limit: int = 50) -> list:
    """Devuelve los últimos N eventos filtrados. Útil para /auditlog en el bot."""
    try:
        from services.db import pg_available, pg_conn
    except Exception:
        return []
    if not pg_available():
        return []
    where = "WHERE 1=1"
    params = []
    if chat_id is not None:
        where += " AND chat_id = %s"; params.append(chat_id)
    if action is not None:
        where += " AND action = %s"; params.append(action)
    params.append(limit)
    try:
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute(f"""
                    SELECT ts, tenant_slug, chat_id, actor, action, resource, details
                    FROM shared.audit_events
                    {where}
                    ORDER BY ts DESC
                    LIMIT %s
                """, params)
                rows = cur.fetchall()
        return [
            {"ts": r[0].isoformat() if r[0] else None,
             "tenant": r[1], "chat_id": r[2], "actor": r[3],
             "action": r[4], "resource": r[5], "details": r[6]}
            for r in rows
        ]
    except Exception as e:
        log.warning(f"audit recent fail: {e}")
        return []
