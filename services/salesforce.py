"""
services/salesforce.py — Cliente Salesforce REST API para CRM por tenant.

Auth: OAuth2 client_credentials. El token se cachea en memoria por (tenant, env)
con TTL de 50 minutos (Salesforce los emite con ~2h de vida; 50 min nos deja
margen). El refresh es transparente.

Per-tenant + per-env: las credenciales viven en el vault del VPS con prefijo
SF_<TENANT>_<ENV>_*. Hoy: SF_REAMERICA_UAT_*. Mañana: SF_REAMERICA_PROD_* y
SF_<OTROTENANT>_*.

Uso típico:
    from services.salesforce import sf_query, sf_describe
    rows = sf_query("SELECT Id, Name FROM Account LIMIT 10",
                    tenant="reamerica", env="uat")
    schema = sf_describe("Account", tenant="reamerica", env="uat")

Seguridad:
- sf_query NO valida que sea SOLO SELECT. La validación de ALLOW_ONLY_SELECT
  vive en el callsite (ej. en la tool del LLM se filtra). Para queries
  destructivas (DELETE/UPDATE) hay que usar sf_dml() explícito desde el
  worker o un comando autenticado del owner.
- Las credenciales se leen del vault — NUNCA hardcodear.
- Logs: enmascaran client_id (últimos 8 chars) y NUNCA loguean el token.
"""
import os
import time
import logging
import requests
from typing import Optional

log = logging.getLogger(__name__)

# Cache de tokens: {(tenant, env): {"token": str, "instance_url": str, "exp": float}}
_TOKEN_CACHE: dict = {}
_TOKEN_TTL_SECONDS = 50 * 60  # 50 min — SF emite ~2h, refresh anticipado


def _vault_get(key: str) -> Optional[str]:
    """Lee del vault. Devuelve None si no existe o si el vault no está disponible."""
    try:
        from services.vault import get
        v = get(key)
        return v if v and v != "None" else None
    except Exception as e:
        log.debug(f"vault read fail for {key}: {e}")
        return None


def _creds(tenant: str, env: str) -> dict:
    """Trae las creds del vault para (tenant, env). Raises si falta algo."""
    prefix = f"SF_{tenant.upper()}_{env.upper()}_"
    out = {
        "domain":        _vault_get(prefix + "DOMAIN"),
        "client_id":     _vault_get(prefix + "CLIENT_ID"),
        "client_secret": _vault_get(prefix + "CLIENT_SECRET"),
        "username":      _vault_get(prefix + "USERNAME"),
        "api_version":   _vault_get(prefix + "API_VERSION") or "v59.0",
        "grant_type":    _vault_get(prefix + "GRANT_TYPE") or "client_credentials",
    }
    missing = [k for k, v in out.items() if not v and k not in ("username", "api_version", "grant_type")]
    if missing:
        raise RuntimeError(
            f"Salesforce {tenant}/{env}: faltan credenciales en vault: {missing}. "
            f"Esperaba claves con prefijo {prefix}"
        )
    return out


def sf_token(tenant: str = "reamerica", env: str = "uat",
             force_refresh: bool = False) -> tuple:
    """Obtiene (access_token, instance_url) usando OAuth2 client_credentials.
    Cachea ~50min. Refresh transparente al expirar."""
    key = (tenant.lower(), env.lower())
    now = time.time()
    cached = _TOKEN_CACHE.get(key)
    if cached and not force_refresh and cached["exp"] > now:
        return cached["token"], cached["instance_url"]

    c = _creds(tenant, env)
    log.info(f"SF token request {tenant}/{env} client_id=...{c['client_id'][-8:]}")
    r = requests.post(
        f"https://{c['domain']}/services/oauth2/token",
        data={
            "grant_type":    c["grant_type"],
            "client_id":     c["client_id"],
            "client_secret": c["client_secret"],
        },
        timeout=15,
    )
    if r.status_code != 200:
        # No loguear body completo (puede contener detalles del client_id)
        raise RuntimeError(f"SF token HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"SF token sin access_token: {list(j.keys())}")
    inst = j.get("instance_url", f"https://{c['domain']}")
    _TOKEN_CACHE[key] = {
        "token":        j["access_token"],
        "instance_url": inst,
        "exp":          now + _TOKEN_TTL_SECONDS,
    }
    log.info(f"SF token ok {tenant}/{env} instance={inst} exp_in={_TOKEN_TTL_SECONDS}s")
    return j["access_token"], inst


def _api(tenant: str, env: str) -> str:
    """API version del tenant/env."""
    return _creds(tenant, env)["api_version"]


def sf_query(soql: str, tenant: str = "reamerica", env: str = "uat",
             follow_next: bool = True, max_records: int = 200) -> list:
    """Ejecuta un SOQL contra Salesforce y devuelve lista de records.
    follow_next=True pagina automáticamente hasta max_records (default 200).
    El callsite es responsable de validar que el SOQL sea SELECT cuando
    venga del LLM (ver bot tool). Acá no se valida — sf_query es genérica."""
    token, inst = sf_token(tenant, env)
    api = _api(tenant, env)
    url = f"{inst}/services/data/{api}/query/"
    params = {"q": soql}
    headers = {"Authorization": f"Bearer {token}"}
    out: list = []
    while True:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 401 and out == []:
            # Token caducado; refresh y retry una vez
            token, inst = sf_token(tenant, env, force_refresh=True)
            headers["Authorization"] = f"Bearer {token}"
            r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"SF query HTTP {r.status_code}: {r.text[:300]}")
        j = r.json()
        out.extend(j.get("records", []))
        if len(out) >= max_records:
            return out[:max_records]
        nxt = j.get("nextRecordsUrl")
        if not follow_next or not nxt:
            return out
        url = f"{inst}{nxt}"
        params = {}


def sf_describe(sobject: str, tenant: str = "reamerica", env: str = "uat") -> dict:
    """Devuelve el describe de un sObject (campos, tipos, picklists, etc).
    Útil para que el LLM sepa qué queryear."""
    token, inst = sf_token(tenant, env)
    api = _api(tenant, env)
    r = requests.get(
        f"{inst}/services/data/{api}/sobjects/{sobject}/describe/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"SF describe HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def sf_list_objects(tenant: str = "reamerica", env: str = "uat",
                    queryable_only: bool = True) -> list:
    """Lista los sObjects disponibles en la org."""
    token, inst = sf_token(tenant, env)
    api = _api(tenant, env)
    r = requests.get(
        f"{inst}/services/data/{api}/sobjects/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"SF list HTTP {r.status_code}: {r.text[:300]}")
    objs = r.json().get("sobjects", [])
    if queryable_only:
        objs = [o for o in objs if o.get("queryable")]
    return [{"name": o["name"], "label": o.get("label"),
             "custom": o.get("custom", False)} for o in objs]


def is_select_only(soql: str) -> bool:
    """True si el SOQL es solo SELECT (sin DML embebido). Para validación en LLM."""
    s = soql.strip().lower()
    if not s.startswith("select"):
        return False
    forbidden = ["insert ", "update ", "delete ", "upsert ", "merge ", ";"]
    return not any(f in s for f in forbidden)
