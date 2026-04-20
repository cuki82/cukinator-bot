"""
services/outlook.py — Cliente Microsoft Graph API para Outlook corporativo.

Auth: OAuth2 client_credentials contra Azure AD. Token cacheado ~50min
(Microsoft emite ~1h). Refresh transparente al expirar.

Per-tenant: credenciales en vault con prefijo OUTLOOK_<TENANT>_*. Hoy:
OUTLOOK_REAMERICA_*. Mañana: OUTLOOK_GOODSTEN_* si usan Outlook tambien.

Uso típico:
    from services.outlook import outlook_inbox, outlook_send, outlook_thread
    mails = outlook_inbox(days=7, unread=True, tenant="reamerica",
                          user="mromanelli@reamerica-re.com")
    outlook_send(to=["pepe@cedente.com"], subject="...", body_html="...",
                 tenant="reamerica", from_user="mromanelli@reamerica-re.com")

Alcance:
- Graph v1.0 /users/{user}/messages (admin app permissions).
- Requiere admin consent para Mail.Read, Mail.Send, Mail.ReadWrite.
- NUNCA enviar sin confirmación explícita del owner.
"""
import os
import time
import logging
import requests
from typing import Optional, List, Dict, Any

log = logging.getLogger(__name__)

# {(tenant,): {"token": str, "exp": float}}
_TOKEN_CACHE: dict = {}
_TOKEN_TTL_SECONDS = 50 * 60


def _vault_get(key: str) -> Optional[str]:
    try:
        from services.vault import get
        v = get(key)
        return v if v and v != "None" else None
    except Exception as e:
        log.debug(f"vault read fail for {key}: {e}")
        return None


def _creds(tenant: str) -> dict:
    """Trae creds del vault. Raises si falta algo crítico."""
    prefix = f"OUTLOOK_{tenant.upper()}_"
    out = {
        "client_id":     _vault_get(prefix + "CLIENT_ID"),
        "client_secret": _vault_get(prefix + "CLIENT_SECRET"),
        "tenant_id":     _vault_get(prefix + "TENANT_ID"),
        "scope":         _vault_get(prefix + "SCOPE") or "https://graph.microsoft.com/.default",
        "authority":     _vault_get(prefix + "AUTHORITY") or None,
        "graph_base":    _vault_get(prefix + "GRAPH_BASE") or "https://graph.microsoft.com/v1.0",
    }
    missing = [k for k in ("client_id", "client_secret", "tenant_id") if not out[k]]
    if missing:
        raise RuntimeError(f"Outlook {tenant}: faltan en vault: {missing} (prefix={prefix})")
    if not out["authority"]:
        out["authority"] = f"https://login.microsoftonline.com/{out['tenant_id']}"
    return out


def outlook_token(tenant: str = "reamerica", force_refresh: bool = False) -> str:
    """Obtiene access_token vía client_credentials. Cacheado 50min."""
    key = tenant.lower()
    now = time.time()
    cached = _TOKEN_CACHE.get(key)
    if cached and not force_refresh and cached["exp"] > now:
        return cached["token"]

    c = _creds(tenant)
    log.info(f"Outlook token request {tenant} client_id=...{c['client_id'][-8:]}")
    r = requests.post(
        f"{c['authority']}/oauth2/v2.0/token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     c["client_id"],
            "client_secret": c["client_secret"],
            "scope":         c["scope"],
        },
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Outlook token HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"Outlook token sin access_token: {list(j.keys())}")
    _TOKEN_CACHE[key] = {"token": j["access_token"], "exp": now + _TOKEN_TTL_SECONDS}
    log.info(f"Outlook token ok {tenant} expires_in={j.get('expires_in')}s")
    return j["access_token"]


def _graph_get(path: str, tenant: str = "reamerica", params: Optional[dict] = None) -> dict:
    """GET contra Graph con retry en 401 (token expirado)."""
    c = _creds(tenant)
    token = outlook_token(tenant)
    url = f"{c['graph_base']}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code == 401:
        token = outlook_token(tenant, force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Graph GET {path} HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def _graph_post(path: str, payload: dict, tenant: str = "reamerica") -> Any:
    c = _creds(tenant)
    token = outlook_token(tenant)
    url = f"{c['graph_base']}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code == 401:
        token = outlook_token(tenant, force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Graph POST {path} HTTP {r.status_code}: {r.text[:400]}")
    if r.status_code == 204 or not r.content:
        return {"status": "ok"}
    return r.json()


def outlook_inbox(user: str, days: int = 7, unread: bool = False,
                  tenant: str = "reamerica", top: int = 25) -> List[dict]:
    """Lista mails del inbox de un user. `user` = email o user-id de Azure.
    days: solo los de los últimos N días. unread=True → solo no leídos."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    filt = [f"receivedDateTime ge {cutoff}"]
    if unread:
        filt.append("isRead eq false")
    params = {
        "$top":      str(min(top, 50)),
        "$filter":   " and ".join(filt),
        "$select":   "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview,webLink",
        "$orderby":  "receivedDateTime desc",
    }
    data = _graph_get(f"/users/{user}/mailFolders/inbox/messages",
                      tenant=tenant, params=params)
    return data.get("value", [])


def outlook_thread(user: str, message_id: str, tenant: str = "reamerica") -> dict:
    """Trae un mensaje completo con body (html + text)."""
    params = {"$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                          "body,bodyPreview,hasAttachments,webLink,conversationId"}
    return _graph_get(f"/users/{user}/messages/{message_id}",
                      tenant=tenant, params=params)


def outlook_send(from_user: str, to: List[str], subject: str,
                 body_html: str, cc: Optional[List[str]] = None,
                 tenant: str = "reamerica", save_to_sent: bool = True) -> dict:
    """Envía mail desde `from_user`.
    IMPORTANTE: el callsite DEBE tener confirmación explícita del owner antes
    de invocar esta función. NUNCA llamar desde tool LLM sin guardrails."""
    msg = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body_html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        },
        "saveToSentItems": save_to_sent,
    }
    if cc:
        msg["message"]["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
    return _graph_post(f"/users/{from_user}/sendMail", msg, tenant=tenant)


def outlook_search(user: str, query: str, tenant: str = "reamerica",
                   top: int = 25) -> List[dict]:
    """Búsqueda fulltext en mailbox del user. KQL syntax.
    Ej: query='from:broker@x.com AND subject:cotización'"""
    params = {
        "$search":  f'"{query}"',
        "$top":     str(min(top, 50)),
        "$select":  "id,subject,from,receivedDateTime,bodyPreview,webLink",
    }
    data = _graph_get(f"/users/{user}/messages", tenant=tenant, params=params)
    return data.get("value", [])


def outlook_list_users(tenant: str = "reamerica", top: int = 50) -> List[dict]:
    """Lista users del tenant Azure (para debug + seleccionar from_user)."""
    params = {"$top": str(min(top, 100)), "$select": "id,displayName,userPrincipalName,mail"}
    data = _graph_get("/users", tenant=tenant, params=params)
    return data.get("value", [])
