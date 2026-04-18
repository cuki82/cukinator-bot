import os, sys, subprocess, logging, time, threading, json, re
from pathlib import Path
from typing import Optional
import requests as _req

# Load vault secrets before any API client initialization
try:
    sys.path.insert(0, os.environ.get("REPO_PATH", "/home/cukibot/cukinator-bot"))
    from services.vault import load_all_to_env
    load_all_to_env()
except Exception as _ve:
    pass  # fallback to env vars

# Diagnóstico al startup — no loguear valores, solo presencia.
import logging as _logging_boot
_logging_boot.basicConfig(level=_logging_boot.INFO)
_boot_log = _logging_boot.getLogger("worker.boot")
_boot_log.info(
    "vault status: OPENAI_API_KEY=%s · TELEGRAM_TOKEN=%s · ANTHROPIC_KEY=%s · WORKER_SECRET=%s",
    "set" if os.environ.get("OPENAI_API_KEY") else "MISSING",
    "set" if os.environ.get("TELEGRAM_TOKEN") else "MISSING",
    "set" if os.environ.get("ANTHROPIC_KEY") else "MISSING",
    "set" if os.environ.get("WORKER_SECRET") else "MISSING",
)

import anthropic
try:
    import openai
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app = FastAPI(title="Cukinator Agent Worker")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
WORKER_SECRET = os.environ.get("WORKER_SECRET", "cuki-worker-secret")
REPO_PATH     = os.environ.get("REPO_PATH", "/home/cukibot/cukinator-bot")
REPO_REMOTE   = f"https://{GITHUB_TOKEN}@github.com/cuki82/cukinator-bot.git"

# Exclusión mutua: una tarea a la vez sobre el repo. Si llega otra mientras
# el worker está ocupado, /task responde 409 con el task_id en curso.
_repo_lock: threading.Lock = threading.Lock()
_current_task: Optional[dict] = None

# ── Streaming del progreso a Telegram ─────────────────────────────────────────
# El worker postea un mensaje al chat del usuario y lo va editando en vivo
# a medida que Claude Code CLI ejecuta tools. Esto evita el "cacho de texto"
# final sin visibilidad de qué está pasando.

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")

_TOOL_EMOJI = {
    "Bash": "⚡", "Read": "📖", "Edit": "✏️", "Write": "📝", "MultiEdit": "✏️",
    "Grep": "🔍", "Glob": "🔎", "Task": "🧩", "TodoWrite": "📋",
    "WebFetch": "🌐", "WebSearch": "🌐", "NotebookEdit": "📓",
}


def _tg_send(chat_id: int, text: str) -> Optional[int]:
    if not TELEGRAM_TOKEN or not chat_id:
        return None
    try:
        r = _req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        log.warning(f"tg_send failed: {e}")
    return None


def _tg_edit(chat_id: int, message_id: int, text: str) -> bool:
    if not TELEGRAM_TOKEN or not chat_id or not message_id:
        return False
    try:
        r = _req.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id,
                  "text": text[:4000], "parse_mode": "Markdown"},
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"tg_edit failed: {e}")
        return False


def _format_tool_line(name: str, inp: dict) -> str:
    """Una sola línea estilo 'emoji Tool `arg-corto`'. Markdown-safe."""
    emoji = _TOOL_EMOJI.get(name, "🔧")
    desc = ""
    if name == "Bash":
        cmd = (inp.get("command") or "")[:70]
        desc = f"`{cmd}`"
    elif name in ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = (inp.get("file_path") or inp.get("path") or "")[:70]
        desc = f"`{path}`"
    elif name in ("Grep", "Glob"):
        pat = (inp.get("pattern") or "")[:50]
        desc = f"`{pat}`"
    elif name in ("WebSearch", "WebFetch"):
        q = (inp.get("query") or inp.get("url") or "")[:50]
        desc = f"`{q}`"
    elif name == "TodoWrite":
        count = len(inp.get("todos") or [])
        desc = f"_({count} ítems)_"
    else:
        desc = ""
    return f"{emoji} *{name}* {desc}".strip()


def _compose_progress(title: str, steps: list, footer: str = "") -> str:
    """Arma el mensaje con header + pasos estilo árbol + footer."""
    parts = [title, ""]
    if steps:
        for i, s in enumerate(steps):
            prefix = "└─" if i == len(steps) - 1 and not footer else "├─"
            parts.append(f"{prefix} {s}")
    if footer:
        parts.append(f"└─ {footer}")
    return "\n".join(parts)


def _run_claude_cli_streaming(prompt: str, chat_id: int, task_id: str) -> dict:
    """
    Lanza Claude Code CLI con --output-format stream-json y postea/edita
    un mensaje en Telegram con el progreso en vivo. Retorna:
      {final_text, tool_names, stdout_raw, returncode, errors}
    """
    env = dict(os.environ)
    ak = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if ak:
        env["ANTHROPIC_API_KEY"] = ak

    title = "⚡ *Agent Worker — Ejecutando*"
    steps: list = []
    msg_id = _tg_send(chat_id, _compose_progress(title, steps, "_iniciando..._"))

    final_text = ""
    tool_names: list = []
    raw_lines: list = []
    errors: list = []
    last_edit = 0.0
    EDIT_THROTTLE = 1.5  # segundos entre edits para no pegar rate limit

    try:
        proc = subprocess.Popen(
            ["claude", "-p", prompt,
             "--output-format", "stream-json", "--verbose",
             "--dangerously-skip-permissions"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=REPO_PATH, env=env, bufsize=1,
        )
    except FileNotFoundError:
        errors.append("claude CLI no encontrado en PATH")
        _tg_edit(chat_id, msg_id, _compose_progress(title, steps, "❌ claude CLI no está instalado"))
        return {"final_text": "", "tool_names": [], "stdout_raw": "", "returncode": -1, "errors": errors, "msg_id": msg_id}
    except Exception as e:
        errors.append(f"spawn error: {e}")
        _tg_edit(chat_id, msg_id, _compose_progress(title, steps, f"❌ spawn error: {e}"))
        return {"final_text": "", "tool_names": [], "stdout_raw": "", "returncode": -1, "errors": errors, "msg_id": msg_id}

    try:
        for line in proc.stdout:
            raw_lines.append(line)
            line_s = line.strip()
            if not line_s:
                continue
            try:
                evt = json.loads(line_s)
            except json.JSONDecodeError:
                continue

            etype = evt.get("type")
            if etype == "assistant":
                msg = evt.get("message") or {}
                for block in (msg.get("content") or []):
                    btype = block.get("type")
                    if btype == "tool_use":
                        nm = block.get("name", "")
                        tool_names.append(nm)
                        steps.append(_format_tool_line(nm, block.get("input") or {}))
                        now = time.time()
                        if now - last_edit > EDIT_THROTTLE:
                            _tg_edit(chat_id, msg_id, _compose_progress(title, steps, "_en progreso..._"))
                            last_edit = now
                    elif btype == "text":
                        txt = (block.get("text") or "").strip()
                        if txt:
                            final_text = txt
            elif etype == "result":
                rt = evt.get("result") or evt.get("content") or ""
                if rt:
                    final_text = rt if isinstance(rt, str) else str(rt)

        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        errors.append("timeout esperando a que cierre claude CLI")
        proc.terminate()
    except Exception as e:
        errors.append(f"stream error: {e}")

    stderr_raw = ""
    try:
        stderr_raw = proc.stderr.read() or ""
    except Exception:
        pass
    if proc.returncode and proc.returncode != 0:
        errors.append(f"claude rc={proc.returncode}: {stderr_raw[:300]}")

    footer = f"✅ *Listo* · {len(tool_names)} tools usadas" if not errors else f"⚠️ terminó con errores ({len(errors)})"
    _tg_edit(chat_id, msg_id, _compose_progress(title, steps, footer))

    return {
        "final_text": final_text,
        "tool_names": tool_names,
        "stdout_raw": "".join(raw_lines),
        "returncode": proc.returncode,
        "errors": errors,
        "msg_id": msg_id,
    }

class CodingTask(BaseModel):
    task_id: str
    user_text: str
    chat_id: int


class WorkerResult(BaseModel):
    task_id: str
    status: str
    summary: str
    modified_files: list = []
    git_info: dict = {}
    errors: list = []
    duration_s: float = 0


def bash_exec(command, cwd=None, timeout=60):
    work_dir = cwd or REPO_PATH
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=work_dir)
        return {"stdout": r.stdout[:4000], "stderr": r.stderr[:2000], "returncode": r.returncode, "success": r.returncode == 0}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timeout ({timeout}s)", "returncode": -1, "success": False}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}


def read_file(path):
    fp = Path(REPO_PATH) / path
    try:
        c = fp.read_text(encoding="utf-8")
        return {"content": c[:8000], "lines": len(c.split("\n")), "success": True}
    except FileNotFoundError:
        return {"content": "", "error": f"No encontrado: {path}", "success": False}
    except Exception as e:
        return {"content": "", "error": str(e), "success": False}


def write_file(path, content):
    if path in PROTECTED_FILES:
        return {"success": False, "error": f"`{path}` es archivo protegido."}
    fp = Path(REPO_PATH) / path
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return {"success": True, "path": path, "bytes": len(content.encode())}
    except Exception as e:
        return {"success": False, "error": str(e)}


def git_status():
    r = bash_exec("git status --short && git branch --show-current && git log --oneline -3")
    return {"output": r["stdout"], "success": r["success"]}


def git_commit_push(message):
    bash_exec("git config user.email bot@cukinator.com")
    bash_exec("git config user.name CukinatorBot")
    bash_exec("git checkout main && git pull origin main --rebase")
    bash_exec("git add -A")
    r_diff = bash_exec("git diff --cached --name-only")
    if not r_diff["stdout"].strip():
        return {"success": False, "error": "No hay cambios para commitear"}
    modified = r_diff["stdout"].strip().split("\n")
    r_commit = bash_exec("git commit -m " + repr(message))
    if not r_commit["success"]:
        return {"success": False, "error": r_commit["stderr"], "modified": modified}
    bash_exec(f"git remote set-url origin {REPO_REMOTE}")
    r_push = bash_exec("git push origin main")
    return {
        "success": r_push["success"],
        "modified": modified,
        "commit": bash_exec("git log --oneline -1")["stdout"].strip(),
        "branch": "main",
        "error": r_push["stderr"] if not r_push["success"] else ""
    }


def run_tests():
    r = bash_exec("find . -name '*.py' -not -path './.git/*' | head -20 | xargs python3 -m py_compile 2>&1 && echo SYNTAX_OK")
    return {"passed": "SYNTAX_OK" in r["stdout"], "output": (r["stdout"] + r["stderr"])[:2000]}


def _codex_client():
    if not _HAS_OPENAI:
        return None
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
    if not key:
        return None
    return openai.OpenAI(api_key=key)


CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5-codex")
# gpt-5-codex requiere el endpoint /v1/responses (API nueva de OpenAI). No
# funciona con /v1/chat/completions (da 404). Todo el flujo Codex del worker
# usa _openai_responses() que llama a /v1/responses via HTTP directo — así
# nos independizamos de la versión del cliente openai-python.


def _openai_responses(instructions: str, user_input: str,
                     model: Optional[str] = None, max_tokens: int = 800) -> str:
    """Llama al endpoint /v1/responses de OpenAI y devuelve el texto generado.
    Retorna "" si falla (caller hace fallback al prompt raw)."""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
    if not key:
        log.warning("OPENAI_API_KEY no configurada — Codex desactivado")
        return ""
    try:
        r = _req.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model or CODEX_MODEL,
                "instructions": instructions,
                "input": user_input,
                "max_output_tokens": max_tokens,
            },
            timeout=60,
        )
        if r.status_code != 200:
            log.error(f"OpenAI /responses {r.status_code}: {r.text[:300]}")
            return ""
        data = r.json()
        # Shortcut si la SDK lo provee
        if "output_text" in data and isinstance(data["output_text"], str):
            return data["output_text"].strip()
        # Parseo manual del output structured
        for item in data.get("output", []) or []:
            if item.get("type") == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text"):
                        return (c.get("text") or "").strip()
        return ""
    except Exception as e:
        log.error(f"OpenAI /responses error: {e}")
        return ""


def codex_plan(user_text: str) -> str:
    """gpt-5-codex transforma el mensaje del user en un prompt técnico para Claude Code CLI."""
    system = (
        "Sos un prompt engineer experto en tareas DevOps/coding sobre un bot Python. "
        "El usuario manda una descripción informal. Convertila en un prompt técnico detallado "
        "para un agente ejecutor (Claude Code CLI) que tiene tools reales de filesystem, "
        "bash, git, grep. El prompt debe incluir: objetivo claro, archivos relevantes a "
        "leer antes de editar, pasos concretos, criterio de éxito. Respondeme SOLO con el "
        "prompt final en español rioplatense, sin preámbulos."
    )
    plan = _openai_responses(system, user_text, max_tokens=800)
    if not plan:
        log.warning("codex_plan fallback a prompt raw (sin expandir)")
        return user_text
    log.info(f"[codex_plan] {len(plan)} chars")
    return plan


def codex_summarize(user_text: str, raw_summary: str, modified_files: list, git_info: dict) -> str:
    """gpt-5-codex formatea el resultado de Claude Code para mandarlo a Telegram."""
    context = f"Pedido: {user_text}\n\nResultado técnico: {raw_summary}\n"
    if modified_files:
        context += f"Archivos modificados: {modified_files}\n"
    if git_info.get("commit"):
        context += f"Git: {git_info['commit']}\n"
    system = (
        "Sos un formateador de respuestas técnicas para Telegram (rioplatense). Recibís "
        "el pedido original + resultado técnico de un agente. Generá una respuesta clara, "
        "concisa, con markdown básico (bullets, bold). Máximo 1500 caracteres. Sin preámbulos. "
        "Arrancá directo con lo relevante."
    )
    out = _openai_responses(system, context, max_tokens=600)
    return out or raw_summary


def run_agent(task):
    """Pipeline: Codex planner -> Claude Code CLI executor -> Codex summarizer."""
    start = time.time()
    modified_files, git_info, errors = [], {}, []

    # Paso 1: plan + mensaje inicial de "planificando"
    _tg_plan_id = _tg_send(task.chat_id, "🧠 *Agent Worker — Planificando*\n_armando prompt técnico con Codex..._")
    log.info(f"[{task.task_id}] codex_plan: building prompt...")
    enhanced = codex_plan(task.user_text)
    log.info(f"[{task.task_id}] codex_plan OK ({len(enhanced)} chars)")
    if _tg_plan_id:
        _tg_edit(
            task.chat_id, _tg_plan_id,
            f"🧠 *Planificado* · _{len(enhanced)} chars_\n└─ paso al executor",
        )

    # Paso 2: executor con streaming en vivo
    log.info(f"[{task.task_id}] Launching Claude Code CLI with stream...")
    stream_res = _run_claude_cli_streaming(enhanced, task.chat_id, task.task_id)
    raw_output = stream_res.get("final_text", "") or stream_res.get("stdout_raw", "")
    errors.extend(stream_res.get("errors", []))

    # Detect files modified via git diff
    try:
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=10, cwd=REPO_PATH
        )
        if diff_result.stdout.strip():
            modified_files = diff_result.stdout.strip().splitlines()
        # Latest commit info
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            capture_output=True, text=True, timeout=10, cwd=REPO_PATH
        )
        if log_result.stdout:
            git_info["commit"] = log_result.stdout.strip()
    except Exception as e:
        log.warning(f"git status check failed: {e}")

    log.info(f"[{task.task_id}] codex_summarize...")
    final_summary = codex_summarize(task.user_text, raw_output or "(sin output)", modified_files, git_info)

    return WorkerResult(
        task_id=task.task_id,
        status="ok" if not errors else ("partial" if raw_output else "error"),
        summary=final_summary,
        modified_files=modified_files,
        git_info=git_info,
        errors=errors,
        duration_s=round(time.time() - start, 1)
    )




# ── FastAPI routes ──────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "repo": REPO_PATH,
        "repo_exists": os.path.exists(REPO_PATH),
        "architecture": "Codex(planner) + ClaudeCode(executor) + Codex(summarizer)",
        "codex_available": _HAS_OPENAI and bool(os.environ.get("OPENAI_API_KEY")),
    }


@app.post("/task", response_model=WorkerResult)
def execute_task(task: CodingTask, x_worker_secret: str = Header(None)):
    if x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    global _current_task
    if not _repo_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Worker ocupado: {_current_task}"
        )
    _current_task = {"task_id": task.task_id, "started_at": time.time()}
    try:
        if os.path.exists(REPO_PATH):
            subprocess.run(
                "git fetch origin && git checkout main && git pull origin main",
                shell=True, cwd=REPO_PATH, timeout=30, capture_output=True
            )
        else:
            subprocess.run(
                f"git clone {REPO_REMOTE} {REPO_PATH}",
                shell=True, cwd="/home/cukibot", timeout=120, capture_output=True
            )
        return run_agent(task)
    finally:
        _repo_lock.release()
        _current_task = None


@app.get("/status")
def worker_status():
    occ = not _repo_lock.acquire(blocking=False)
    if not occ:
        _repo_lock.release()
    return {
        "available": not occ,
        "current_task": _current_task,
        "repo_path": REPO_PATH,
        "repo_exists": os.path.exists(REPO_PATH),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3335, log_level="info")
