"""
agent_worker.py — Claude Code Worker

Corre en el VPS como servicio FastAPI.
Recibe tareas de coding del bot de Railway.
Ejecuta Claude con tools reales de bash/git/archivos.
Devuelve resultado estructurado.

Arrancar con:
    uvicorn agent_worker:app --host 0.0.0.0 --port 3335

Variables de entorno requeridas:
    ANTHROPIC_KEY
    GITHUB_TOKEN
    WORKER_SECRET  (token para autenticar requests del bot)
    REPO_PATH      (path al repo clonado, default: /opt/cukinator-bot)
"""

import os
import subprocess
import logging
import asyncio
import uuid
import time
import threading
from pathlib import Path
from typing import Optional

import anthropic
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Cukinator Agent Worker")

ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY", "")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
WORKER_SECRET   = os.environ.get("WORKER_SECRET", "cuki-worker-secret")
REPO_PATH       = os.environ.get("REPO_PATH", "/opt/cukinator-bot")
REPO_URL        = "https://github.com/cuki82/cukinator-bot"
REPO_REMOTE     = f"https://{GITHUB_TOKEN}@github.com/cuki82/cukinator-bot.git"

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ── Repo Lock ─────────────────────────────────────────────────────────────────

_repo_lock = threading.Lock()
_current_task: Optional[dict] = None


# ── Models ────────────────────────────────────────────────────────────────────

class CodingTask(BaseModel):
    task_id: str
    user_text: str
    chat_id: int
    branch: str = "bot-changes"


class WorkerResult(BaseModel):
    task_id: str
    status: str          # ok | error | partial
    summary: str
    modified_files: list = []
    git_info: dict = {}
    errors: list = []
    duration_s: float = 0


# ── Tools de ejecución real ───────────────────────────────────────────────────

PROTECTED_FILES = [
    "bot.py", "bot_core.py", "orchestrator.py", "intent_router.py",
    "handlers/message_handler.py", "handlers/callback_handler.py",
    "Dockerfile", "requirements.txt"
]


def bash_exec(command: str, cwd: str = None, timeout: int = 60) -> dict:
    """Ejecuta un comando bash y devuelve stdout/stderr/returncode."""
    work_dir = cwd or REPO_PATH
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=timeout, cwd=work_dir
        )
        return {
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
            "returncode": result.returncode,
            "success": result.returncode == 0
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timeout ({timeout}s)", "returncode": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}


def read_file(path: str) -> dict:
    """Lee un archivo del repo."""
    full_path = Path(REPO_PATH) / path
    try:
        content = full_path.read_text(encoding="utf-8")
        return {"content": content[:8000], "lines": len(content.split("\n")), "success": True}
    except FileNotFoundError:
        return {"content": "", "error": f"Archivo no encontrado: {path}", "success": False}
    except Exception as e:
        return {"content": "", "error": str(e), "success": False}


def write_file(path: str, content: str) -> dict:
    """Escribe un archivo en el repo. Bloquea archivos protegidos."""
    if path in PROTECTED_FILES:
        return {"success": False, "error": f"`{path}` es un archivo core protegido. No puede ser modificado por el worker."}
    full_path = Path(REPO_PATH) / path
    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return {"success": True, "path": path, "bytes": len(content.encode())}
    except Exception as e:
        return {"success": False, "error": str(e)}


def git_status() -> dict:
    """Estado actual del repo."""
    r = bash_exec("git status --short && git branch --show-current && git log --oneline -3")
    return {"output": r["stdout"], "success": r["success"]}


def git_checkout_branch(branch: str) -> dict:
    """Crea o cambia a una branch."""
    # Primero pull de main
    bash_exec("git fetch origin")
    bash_exec("git checkout main && git pull origin main")
    # Crear/cambiar a branch
    r = bash_exec(f"git checkout -B {branch} origin/main 2>/dev/null || git checkout -B {branch}")
    return {"branch": branch, "success": r["success"], "output": r["stdout"] + r["stderr"]}


def git_commit_push(message: str, branch: str) -> dict:
    """Hace commit y push de los cambios."""
    # Configurar git
    bash_exec('git config user.email "bot@cukinator.com"')
    bash_exec('git config user.name "Cukinator Bot"')
    # Add all changes
    r_add = bash_exec("git add -A")
    # Check si hay cambios
    r_diff = bash_exec("git diff --cached --name-only")
    if not r_diff["stdout"].strip():
        return {"success": False, "error": "No hay cambios para commitear"}
    modified = r_diff["stdout"].strip().split("\n")
    # Commit
    r_commit = bash_exec(f'git commit -m "{message}"')
    if not r_commit["success"]:
        return {"success": False, "error": r_commit["stderr"], "modified": modified}
    # Push
    bash_exec(f"git remote set-url origin {REPO_REMOTE}")
    r_push = bash_exec(f"git push origin {branch} --force-with-lease")
    return {
        "success": r_push["success"],
        "modified": modified,
        "commit": bash_exec("git log --oneline -1")["stdout"].strip(),
        "branch": branch,
        "error": r_push["stderr"] if not r_push["success"] else ""
    }


def create_pr(title: str, body: str, branch: str = "bot-changes") -> dict:
    """Crea un Pull Request via GitHub API."""
    import urllib.request, json
    url = "https://api.github.com/repos/cuki82/cukinator-bot/pulls"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json"
    }
    payload = json.dumps({"title": title, "body": body, "head": branch, "base": "main"}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        data = json.loads(r.read())
        return {"success": True, "pr_url": data["html_url"], "pr_number": data["number"]}
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        if "already exists" in err:
            return {"success": False, "error": "Ya hay un PR abierto para esta branch. Mergealo o cerralo primero."}
        return {"success": False, "error": err[:200]}
    except Exception as e:
        return {"success": False, "error": str(e)}


def run_tests() -> dict:
    """Corre validación de sintaxis Python en el repo."""
    r = bash_exec("python3 -m py_compile bot.py bot_core.py orchestrator.py intent_router.py 2>&1 && echo 'SYNTAX OK'")
    return {
        "passed": "SYNTAX OK" in r["stdout"],
        "output": r["stdout"] + r["stderr"],
        "success": r["success"]
    }


# ── Tool definitions para Claude ──────────────────────────────────────────────

WORKER_TOOLS = [
    {
        "name": "bash_exec",
        "description": "Ejecuta un comando bash en el directorio del repo. Usá para inspeccionar, instalar deps, correr scripts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Comando bash a ejecutar"},
                "timeout": {"type": "integer", "description": "Timeout en segundos (default 60)"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Lee el contenido de un archivo del repo. Usá SIEMPRE antes de modificar un archivo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relativo al repo (ej: modules/reservas.py)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Escribe o sobreescribe un archivo en el repo. Archivos core protegidos son bloqueados automáticamente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relativo al repo"},
                "content": {"type": "string", "description": "Contenido completo del archivo"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "git_status",
        "description": "Ver estado actual del repo: archivos modificados, branch actual, últimos commits.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "git_checkout_branch",
        "description": "Crea o cambia a una branch. SIEMPRE usá 'bot-changes' o 'feature/nombre'. Nunca trabajes en main.",
        "input_schema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Nombre de la branch (ej: bot-changes, feature/nuevo-modulo)"}
            },
            "required": ["branch"]
        }
    },
    {
        "name": "git_commit_push",
        "description": "Hace commit de todos los cambios y push a la branch. Usá después de write_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Mensaje del commit"},
                "branch": {"type": "string", "description": "Branch a pushear (default: bot-changes)"}
            },
            "required": ["message"]
        }
    },
    {
        "name": "create_pr",
        "description": "Crea un Pull Request en GitHub para revisión humana. Usá SIEMPRE después del push.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Título del PR"},
                "body": {"type": "string", "description": "Descripción de los cambios"},
                "branch": {"type": "string", "description": "Branch origen (default: bot-changes)"}
            },
            "required": ["title", "body"]
        }
    },
    {
        "name": "run_tests",
        "description": "Corre validación de sintaxis Python. Usá SIEMPRE antes de commit.",
        "input_schema": {"type": "object", "properties": {}}
    }
]

# ── System prompt del worker ───────────────────────────────────────────────────

WORKER_SYSTEM = """Sos el Operational Agent de Cukinator. Tu trabajo es ejecutar tareas de código sobre el repo.

REGLAS ABSOLUTAS:
1. NUNCA trabajes en la branch main. Siempre usá bot-changes o feature/nombre-descriptivo.
2. SIEMPRE leé un archivo antes de modificarlo (read_file).
3. SIEMPRE corré run_tests antes de hacer commit.
4. SIEMPRE creá un PR después del push (create_pr).
5. Archivos protegidos (bot.py, bot_core.py, Dockerfile, etc.) NO pueden ser modificados — el sistema los bloquea.
6. Si un archivo protegido necesita cambios, explicalo en el PR body para revisión manual.

FLUJO CORRECTO para cualquier tarea de código:
1. git_status → entender el estado actual
2. git_checkout_branch → crear/cambiar a branch
3. read_file → leer los archivos relevantes
4. write_file → hacer los cambios
5. run_tests → validar sintaxis
6. git_commit_push → commitear y pushear
7. create_pr → abrir PR para revisión

Devolvé un resumen claro de qué hiciste, qué archivos tocaste, y el link del PR."""


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def dispatch_tool(name: str, inputs: dict) -> str:
    if name == "bash_exec":
        r = bash_exec(inputs["command"], timeout=inputs.get("timeout", 60))
        return f"stdout: {r['stdout']}\nstderr: {r['stderr']}\nreturncode: {r['returncode']}"
    elif name == "read_file":
        r = read_file(inputs["path"])
        if r["success"]:
            return f"[{inputs['path']} — {r['lines']} líneas]\n{r['content']}"
        return f"Error: {r.get('error')}"
    elif name == "write_file":
        r = write_file(inputs["path"], inputs["content"])
        if r["success"]:
            return f"Archivo escrito: {r['path']} ({r['bytes']} bytes)"
        return f"Error: {r.get('error')}"
    elif name == "git_status":
        r = git_status()
        return r["output"]
    elif name == "git_checkout_branch":
        r = git_checkout_branch(inputs["branch"])
        return f"Branch: {r['branch']}\n{r['output']}"
    elif name == "git_commit_push":
        r = git_commit_push(inputs["message"], inputs.get("branch", "bot-changes"))
        if r["success"]:
            return f"Commit+Push OK\nArchivos: {r.get('modified', [])}\nCommit: {r.get('commit')}"
        return f"Error: {r.get('error')}"
    elif name == "create_pr":
        r = create_pr(inputs["title"], inputs["body"], inputs.get("branch", "bot-changes"))
        if r["success"]:
            return f"PR creado: {r['pr_url']} (#{r['pr_number']})"
        return f"Error: {r.get('error')}"
    elif name == "run_tests":
        r = run_tests()
        return f"{'PASS' if r['passed'] else 'FAIL'}\n{r['output']}"
    return f"Tool desconocido: {name}"


# ── Agent execution loop ──────────────────────────────────────────────────────

def run_agent(task: CodingTask) -> WorkerResult:
    """Ejecuta el agente Claude con tools reales. Loop hasta end_turn."""
    start = time.time()
    modified_files = []
    git_info = {}
    errors = []

    messages = [{"role": "user", "content": f"Tarea de coding: {task.user_text}"}]

    max_iter = 15
    iteration = 0

    while iteration < max_iter:
        iteration += 1
        log.info(f"[{task.task_id}] Iteración {iteration}")

        try:
            response = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=4096,
                system=WORKER_SYSTEM,
                tools=WORKER_TOOLS,
                messages=messages
            )
        except Exception as e:
            errors.append(str(e))
            return WorkerResult(
                task_id=task.task_id, status="error",
                summary=f"Error de API: {e}", errors=errors,
                duration_s=time.time() - start
            )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    log.info(f"[{task.task_id}] Tool: {block.name}")
                    result = dispatch_tool(block.name, block.input)

                    # Trackear archivos modificados y git info
                    if block.name == "write_file" and "Archivo escrito" in result:
                        modified_files.append(block.input.get("path", ""))
                    elif block.name == "git_commit_push" and "OK" in result:
                        git_info["commit"] = result
                    elif block.name == "create_pr" and "PR creado" in result:
                        git_info["pr"] = result

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            # Respuesta final
            summary_parts = [b.text for b in response.content if hasattr(b, "text") and b.text.strip()]
            summary = "\n".join(summary_parts) or "Tarea completada."

            return WorkerResult(
                task_id=task.task_id,
                status="ok",
                summary=summary,
                modified_files=modified_files,
                git_info=git_info,
                errors=errors,
                duration_s=round(time.time() - start, 1)
            )

    return WorkerResult(
        task_id=task.task_id, status="partial",
        summary="Alcancé el límite de iteraciones.",
        modified_files=modified_files, git_info=git_info,
        duration_s=round(time.time() - start, 1)
    )


# ── FastAPI endpoints ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "repo": REPO_PATH, "repo_exists": Path(REPO_PATH).exists()}


@app.post("/task", response_model=WorkerResult)
def execute_task(task: CodingTask, x_worker_secret: str = Header(None)):
    """Recibe una tarea de coding y la ejecuta con el agente."""
    # Auth
    if x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Repo lock — una tarea a la vez
    global _current_task
    if not _repo_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Worker ocupado con tarea: {_current_task.get('task_id') if _current_task else 'unknown'}"
        )

    _current_task = {"task_id": task.task_id, "started_at": time.time()}

    try:
        # Asegurar que el repo está actualizado
        if Path(REPO_PATH).exists():
            bash_exec("git fetch origin && git checkout main && git pull origin main", cwd=REPO_PATH)
        else:
            bash_exec(f"git clone {REPO_REMOTE} {REPO_PATH}")

        return run_agent(task)
    finally:
        _repo_lock.release()
        _current_task = None


@app.get("/status")
def worker_status():
    """Estado del worker: libre u ocupado."""
    occupied = not _repo_lock.acquire(blocking=False)
    if not occupied:
        _repo_lock.release()
    return {
        "available": not occupied,
        "current_task": _current_task,
        "repo_path": REPO_PATH,
        "repo_exists": Path(REPO_PATH).exists()
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3335, log_level="info")
