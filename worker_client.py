"""
worker_client.py — Cliente HTTP para llamar al Agent Worker desde el bot.

El bot en Railway usa esto para delegar coding tasks al worker en el VPS.
"""

import os
import uuid
import logging
import httpx

log = logging.getLogger(__name__)

WORKER_URL    = os.environ.get("AGENT_WORKER_URL", "http://31.97.151.119:3335")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "cuki-worker-secret")
TIMEOUT       = 300  # 5 minutos para tareas de código


async def send_coding_task(user_text: str, chat_id: int) -> dict:
    """
    Envía una coding task al Agent Worker en el VPS.
    Devuelve el WorkerResult como dict.
    """
    task_id = str(uuid.uuid4())[:8]
    payload = {
        "task_id": task_id,
        "user_text": user_text,
        "chat_id": chat_id,
        "branch": "bot-changes"
    }
    headers = {
        "X-Worker-Secret": WORKER_SECRET,
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # Verificar disponibilidad
            status = await client.get(f"{WORKER_URL}/status", headers=headers)
            if status.status_code == 200:
                st = status.json()
                if not st.get("available"):
                    return {
                        "status": "busy",
                        "summary": f"El Agent Worker está ocupado con otra tarea. Intentá en un momento.",
                        "task_id": task_id
                    }

            # Enviar tarea
            resp = await client.post(
                f"{WORKER_URL}/task",
                json=payload,
                headers=headers,
                timeout=TIMEOUT
            )

            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 409:
                return {
                    "status": "busy",
                    "summary": "Worker ocupado. Esperá a que termine la tarea actual.",
                    "task_id": task_id
                }
            elif resp.status_code == 401:
                return {"status": "error", "summary": "Error de autenticación con el worker.", "task_id": task_id}
            else:
                return {
                    "status": "error",
                    "summary": f"Error del worker: HTTP {resp.status_code}",
                    "task_id": task_id
                }

    except httpx.ConnectError:
        return {
            "status": "error",
            "summary": "No puedo conectar al Agent Worker en el VPS. ¿Está corriendo?",
            "task_id": task_id
        }
    except httpx.TimeoutException:
        return {
            "status": "timeout",
            "summary": f"La tarea tardó más de {TIMEOUT//60} minutos. Revisá el VPS para ver el estado.",
            "task_id": task_id
        }
    except Exception as e:
        log.error(f"worker_client error: {e}")
        return {"status": "error", "summary": str(e), "task_id": task_id}


def format_worker_result(result: dict) -> str:
    """
    Formatea el WorkerResult para mostrar en Telegram.
    """
    status = result.get("status", "error")
    summary = result.get("summary", "Sin respuesta del worker.")
    modified = result.get("modified_files", [])
    git_info = result.get("git_info", {})
    errors = result.get("errors", [])
    duration = result.get("duration_s", 0)

    if status in ("busy", "error", "timeout"):
        return f"**Agent Worker**\n\n{summary}"

    lines = [summary]

    if modified:
        lines.append(f"\n**Archivos modificados**")
        for f in modified:
            lines.append(f"- `{f}`")

    if git_info:
        lines.append(f"\n**Git**")
        if git_info.get("commit"):
            lines.append(f"- commit: `{git_info['commit'][:60]}`")
        if git_info.get("pr"):
            lines.append(f"- {git_info['pr']}")

    if errors:
        lines.append(f"\n**Errores**")
        for e in errors[:3]:
            lines.append(f"- {e}")

    if duration:
        lines.append(f"\n_Tiempo: {duration}s_")

    return "\n".join(lines)
