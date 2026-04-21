"""
services/onedrive.py — Cliente OneDrive / SharePoint vía Microsoft Graph API.

Reutiliza las credenciales OUTLOOK_<TENANT>_* del vault (mismo app registration).
La app debe tener permisos Files.Read.All y Sites.Read.All con admin consent.

Variable de entorno opcional:
  ONEDRIVE_REAMERICA_USER    — email del usuario cuyo drive se usa por defecto

Uso típico:
    from services.onedrive import onedrive_sync_folder
    files = onedrive_sync_folder("marketing", "/tmp/brand",
                                 user="mromanelli@reamerica-re.com")
"""
import logging
import os
import requests
from pathlib import Path
from typing import List, Optional

from services.outlook import _creds, outlook_token

log = logging.getLogger(__name__)


def _graph_get(path: str, tenant: str = "reamerica", params: Optional[dict] = None) -> dict:
    """GET Graph con retry en 401. Igual que el de outlook.py pero local."""
    c = _creds(tenant)
    token = outlook_token(tenant)
    url = path if path.startswith("http") else f"{c['graph_base']}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code == 401:
        token = outlook_token(tenant, force_refresh=True)
        headers["Authorization"] = f"Bearer {token}"
        r = requests.get(url, headers=headers, params=params or {}, timeout=30)
    if r.status_code == 404:
        return {}
    if r.status_code >= 400:
        raise RuntimeError(f"Graph GET {path} HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def _graph_get_all(path: str, tenant: str = "reamerica", params: Optional[dict] = None) -> List[dict]:
    """GET con paginación automática vía @odata.nextLink."""
    items: List[dict] = []
    data = _graph_get(path, tenant=tenant, params=params)
    items.extend(data.get("value", []))
    while "@odata.nextLink" in data:
        data = _graph_get(data["@odata.nextLink"], tenant=tenant)
        items.extend(data.get("value", []))
    return items


def _download_bytes(url: str, tenant: str = "reamerica") -> bytes:
    """Descarga bytes. Si la URL es relativa, agrega auth header."""
    if url.startswith("http"):
        r = requests.get(url, timeout=120, stream=True)
    else:
        c = _creds(tenant)
        token = outlook_token(tenant)
        full_url = f"{c['graph_base']}{url}"
        r = requests.get(full_url, headers={"Authorization": f"Bearer {token}"}, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Download HTTP {r.status_code}")
    return r.content


# ── OneDrive personal del usuario ─────────────────────────────────────────────

def onedrive_list_folder(folder_path: str, user: str, tenant: str = "reamerica") -> List[dict]:
    """Lista el contenido de una carpeta en el OneDrive del usuario.
    folder_path: ruta relativa desde root, ej. 'marketing' o 'documentos/marca'
    """
    encoded = requests.utils.quote(folder_path, safe="/")
    params = {
        "$select": "id,name,file,folder,size,@microsoft.graph.downloadUrl",
        "$top": "100",
    }
    return _graph_get_all(f"/users/{user}/drive/root:/{encoded}:/children",
                          tenant=tenant, params=params)


def onedrive_search(query: str, user: str, tenant: str = "reamerica") -> List[dict]:
    """Busca archivos/carpetas en el drive del usuario por nombre."""
    encoded = requests.utils.quote(query)
    params = {"$select": "id,name,file,folder,parentReference,@microsoft.graph.downloadUrl",
              "$top": "50"}
    return _graph_get_all(f"/users/{user}/drive/search(q='{encoded}')",
                          tenant=tenant, params=params)


def onedrive_sync_folder(
    folder_path: str,
    dest_dir: str,
    user: str,
    tenant: str = "reamerica",
    extensions: Optional[List[str]] = None,
) -> List[str]:
    """Descarga recursivamente una carpeta de OneDrive al disco local.
    extensions: filtro ej. ['.pdf', '.png']. None = todos los archivos.
    Retorna lista de rutas locales descargadas.
    """
    downloaded: List[str] = []
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    def _recurse(remote_path: str, local_dir: Path):
        items = onedrive_list_folder(remote_path, user=user, tenant=tenant)
        if not items:
            log.warning(f"Carpeta vacía o no encontrada en OneDrive: {remote_path}")
            return
        for item in items:
            name = item["name"]
            if "folder" in item:
                sub = local_dir / name
                sub.mkdir(exist_ok=True)
                _recurse(f"{remote_path}/{name}", sub)
            elif "file" in item:
                if extensions and Path(name).suffix.lower() not in extensions:
                    continue
                local_path = local_dir / name
                if local_path.exists():
                    log.info(f"Ya existe, skip: {local_path.name}")
                    downloaded.append(str(local_path))
                    continue
                dl_url = item.get("@microsoft.graph.downloadUrl") or \
                         f"/users/{user}/drive/items/{item['id']}/content"
                try:
                    size_kb = item.get("size", 0) // 1024
                    log.info(f"Descargando: {name} ({size_kb} KB)")
                    local_path.write_bytes(_download_bytes(dl_url, tenant=tenant))
                    downloaded.append(str(local_path))
                except Exception as e:
                    log.error(f"Error descargando {name}: {e}")

    _recurse(folder_path, dest)
    return downloaded


# ── SharePoint sites ───────────────────────────────────────────────────────────

def sharepoint_list_sites(tenant: str = "reamerica", top: int = 20) -> List[dict]:
    """Lista los sites de SharePoint del tenant."""
    params = {"search": "*", "$select": "id,displayName,webUrl", "$top": str(top)}
    return _graph_get_all("/sites", tenant=tenant, params=params)


def sharepoint_list_folder(
    site_id: str, folder_path: str, tenant: str = "reamerica"
) -> List[dict]:
    """Lista carpeta en el drive raíz de un site de SharePoint."""
    encoded = requests.utils.quote(folder_path, safe="/")
    params = {"$select": "id,name,file,folder,size,@microsoft.graph.downloadUrl", "$top": "100"}
    return _graph_get_all(f"/sites/{site_id}/drive/root:/{encoded}:/children",
                          tenant=tenant, params=params)


def sharepoint_sync_folder(
    site_id: str,
    folder_path: str,
    dest_dir: str,
    tenant: str = "reamerica",
    extensions: Optional[List[str]] = None,
) -> List[str]:
    """Descarga recursivamente una carpeta de SharePoint al disco local."""
    downloaded: List[str] = []
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)

    def _recurse(remote_path: str, local_dir: Path):
        items = sharepoint_list_folder(site_id, remote_path, tenant=tenant)
        if not items:
            log.warning(f"Carpeta vacía o no encontrada en SharePoint: {remote_path}")
            return
        for item in items:
            name = item["name"]
            if "folder" in item:
                sub = local_dir / name
                sub.mkdir(exist_ok=True)
                _recurse(f"{remote_path}/{name}", sub)
            elif "file" in item:
                if extensions and Path(name).suffix.lower() not in extensions:
                    continue
                local_path = local_dir / name
                if local_path.exists():
                    downloaded.append(str(local_path))
                    continue
                dl_url = item.get("@microsoft.graph.downloadUrl") or \
                         f"/sites/{site_id}/drive/items/{item['id']}/content"
                try:
                    log.info(f"Descargando SharePoint: {name}")
                    local_path.write_bytes(_download_bytes(dl_url, tenant=tenant))
                    downloaded.append(str(local_path))
                except Exception as e:
                    log.error(f"Error descargando {name}: {e}")

    _recurse(folder_path, dest)
    return downloaded
