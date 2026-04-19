"""
agents/designer_client.py — cliente HTTP al Agent Designer (VPS :3340).

Permite que el bot (u otro agente, ej. agent_worker de reaseguros) delegue
tareas de diseño — HTML, PDFs con branding, PPTX, critique.
"""
import os
import uuid
import logging
import httpx

log = logging.getLogger(__name__)

DESIGNER_URL = os.environ.get("AGENT_DESIGNER_URL", "http://127.0.0.1:3340")
DESIGNER_SECRET = os.environ.get("DESIGNER_SECRET") or os.environ.get("WORKER_SECRET", "cuki-designer-secret")
TIMEOUT = 180


async def send_design_task(brief: str, type_: str = "html",
                           tenant: str = "reamerica",
                           chat_id: int = None,
                           target: str = None,
                           reference: str = None,
                           model: str = None) -> dict:
    """Llama al designer y retorna DesignResult dict."""
    task_id = str(uuid.uuid4())[:8]
    payload = {
        "task_id": task_id,
        "type":    type_,
        "tenant":  tenant,
        "chat_id": chat_id,
        "brief":   brief,
        "target":  target,
        "reference": reference,
        "model":   model,
    }
    headers = {
        "X-Designer-Secret": DESIGNER_SECRET,
        "Content-Type":      "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(f"{DESIGNER_URL}/design", json=payload, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 401:
            return {"status": "error", "summary": "Auth del designer falló.", "task_id": task_id}
        else:
            return {"status": "error", "summary": f"designer HTTP {resp.status_code}", "task_id": task_id}
    except httpx.ConnectError:
        return {"status": "error", "summary": "No puedo conectar al Agent Designer (:3340).", "task_id": task_id}
    except httpx.TimeoutException:
        return {"status": "timeout", "summary": f"Designer tardó más de {TIMEOUT}s.", "task_id": task_id}
    except Exception as e:
        log.error(f"designer client error: {e}")
        return {"status": "error", "summary": str(e), "task_id": task_id}
