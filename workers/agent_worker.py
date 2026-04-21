import os, sys, subprocess, logging, time, threading, json, re, asyncio
from pathlib import Path
from typing import Optional, AsyncGenerator
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
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
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


def _resolve_tenant_safe(chat_id: int) -> str:
    """Devuelve el tenant del chat_id. Falla silencioso a 'reamerica'."""
    try:
        from services.tenants import resolve_tenant  # type: ignore
        return resolve_tenant(chat_id) or "reamerica"
    except Exception:
        return "reamerica"


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
    # Env para los hooks .claude/hooks/*.py — así saben en qué tenant/chat
    # indexar el RAG y a quién mandarle el resumen de session-end.
    env["CUKI_TENANT"] = _resolve_tenant_safe(chat_id)
    env["CUKI_CHAT_ID"] = str(chat_id or 0)
    env["CUKI_TASK_ID"] = task_id or ""
    env["REPO_PATH"] = REPO_PATH

    title = "⚡ *Agent Worker — Ejecutando*"
    steps: list = []
    msg_id = _tg_send(chat_id, _compose_progress(title, steps, "_iniciando..._"))

    final_text = ""
    tool_names: list = []
    raw_lines: list = []
    errors: list = []
    last_edit = 0.0
    latest_thought = "_iniciando..._"
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
                            _tg_edit(chat_id, msg_id, _compose_progress(title, steps, f"_{latest_thought}_"))
                            last_edit = now
                    elif btype == "text":
                        txt = (block.get("text") or "").strip()
                        if txt:
                            final_text = txt
                            # Mostrar lo que Claude está "diciendo" ahora como footer
                            short = txt[:120].replace("\n", " ")
                            if len(txt) > 120:
                                short += "…"
                            latest_thought = short
                            now = time.time()
                            if now - last_edit > EDIT_THROTTLE:
                                _tg_edit(chat_id, msg_id, _compose_progress(title, steps, f"_{latest_thought}_"))
                                last_edit = now
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
    context = f"Pedido original del usuario: {user_text}\n\nResultado técnico del agente: {raw_summary}\n"
    if modified_files:
        context += f"Archivos tocados: {', '.join(modified_files)}\n"
    if git_info.get("commit"):
        context += f"Commit: {git_info['commit']}\n"
    system = (
        "Sos un asistente técnico que le explica a un usuario no-developer qué hizo un agente de código. "
        "Usá lenguaje claro, rioplatense, sin jerga técnica innecesaria. Markdown básico para Telegram. "
        "SIEMPRE incluí estas tres secciones (omití las que no apliquen, pero si aplican no las saltes):\n"
        "✅ *Qué se hizo* — explicá qué cambios concretos se implementaron, en criollo\n"
        "⏳ *Qué falta* — si quedó algo pendiente del lado del agente, listalo\n"
        "🙋 *Qué necesito de vos* — si hay decisiones o info que el agente necesita del usuario para continuar\n"
        "Máximo 1500 caracteres. Sin preámbulos. Arrancá directo con la primera sección."
    )
    out = _openai_responses(system, context, max_tokens=700)
    return out or raw_summary


# ── SPARC-lite ─────────────────────────────────────────────────────────────
# Para briefs grandes (>~500 tokens), insertamos dos fases previas al exec:
# Spec (Haiku: qué hay que lograr) → Arch (Haiku: qué archivos/cambios).
# Post-exec, si el diff es grande, corre un Review (Opus).
# Los artifacts quedan en `.sparc/<task_id>/` dentro del repo para auditoría.

SPARC_TOKEN_THRESHOLD = int(os.environ.get("SPARC_THRESHOLD_TOKENS", "500"))
SPARC_REVIEW_DIFF_LINES = int(os.environ.get("SPARC_REVIEW_LINES", "300"))
SPARC_MODEL_SPEC = os.environ.get("SPARC_MODEL_SPEC", "claude-haiku-4-5-20251001")
SPARC_MODEL_ARCH = os.environ.get("SPARC_MODEL_ARCH", "claude-haiku-4-5-20251001")
SPARC_MODEL_REVIEW = os.environ.get("SPARC_MODEL_REVIEW", "claude-opus-4-7")


def _approx_tokens(text: str) -> int:
    """Heurística chars/4. Rápida y suficiente para decidir si activar SPARC."""
    return max(1, len(text or "") // 4)


_anthropic_client = None
def _anthropic_client_lazy():
    global _anthropic_client
    if _anthropic_client is None and ANTHROPIC_KEY:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic_client


def _claude_api_call(model: str, system: str, user: str, max_tokens: int = 2000) -> str:
    """Llamada simple a la API de Anthropic (no CLI). Usado para las fases
    Spec/Arch/Review. Retorna "" si falla."""
    cli = _anthropic_client_lazy()
    if not cli:
        return ""
    try:
        resp = cli.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        out = ""
        for block in resp.content:
            if hasattr(block, "text"):
                out += block.text
        return out.strip()
    except Exception as e:
        log.warning(f"[sparc] claude api {model} fail: {e}")
        return ""


def _sparc_dir(task_id: str) -> Path:
    d = Path(REPO_PATH) / ".sparc" / (task_id or "unknown")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sparc_spec(user_text: str, task_id: str) -> str:
    system = (
        "Sos un analista técnico. Dado el pedido informal de un usuario, producí un "
        "Specification breve y accionable: qué problema hay que resolver, qué entradas/"
        "salidas, qué criterios de aceptación, qué NO está en scope. Markdown, 6-12 "
        "bullets máximo, sin preámbulo."
    )
    spec = _claude_api_call(SPARC_MODEL_SPEC, system, user_text, max_tokens=1200)
    if spec:
        try:
            (_sparc_dir(task_id) / "spec.md").write_text(spec, encoding="utf-8")
        except Exception as e:
            log.warning(f"[sparc] spec save fail: {e}")
    return spec


def _sparc_arch(spec: str, user_text: str, task_id: str) -> str:
    system = (
        "Sos un architect senior del repo cukinator-bot (Python/FastAPI, Supabase, "
        "multi-tenant). Dado el Spec, producí un Architecture plan: qué archivos "
        "concretos del repo hay que leer, qué archivos nuevos crear, qué servicios/"
        "tools del repo reutilizar, riesgos y dependencias. Markdown, orientado a "
        "que un ejecutor (Claude Code CLI) sepa por dónde arrancar. Sin código."
    )
    user = f"Pedido original: {user_text}\n\nSpec:\n{spec}"
    arch = _claude_api_call(SPARC_MODEL_ARCH, system, user, max_tokens=1500)
    if arch:
        try:
            (_sparc_dir(task_id) / "arch.md").write_text(arch, encoding="utf-8")
        except Exception as e:
            log.warning(f"[sparc] arch save fail: {e}")
    return arch


def _sparc_review(user_text: str, spec: str, arch: str, diff_text: str, task_id: str) -> str:
    system = (
        "Sos un code reviewer senior. Revisá el diff contra el Spec/Arch originales. "
        "Devolvé: 1) qué del spec quedó cubierto y qué no, 2) riesgos concretos "
        "(regresiones, seguridad, edge cases), 3) 3-5 acciones priorizadas. Markdown, "
        "directo, sin rodeos. Si el diff cumple, decílo corto."
    )
    user = (
        f"Pedido: {user_text}\n\n# Spec\n{spec[:3000]}\n\n# Arch\n{arch[:3000]}\n\n"
        f"# Diff\n```diff\n{diff_text[:12000]}\n```"
    )
    review = _claude_api_call(SPARC_MODEL_REVIEW, system, user, max_tokens=2000)
    if review:
        try:
            (_sparc_dir(task_id) / "review.md").write_text(review, encoding="utf-8")
        except Exception as e:
            log.warning(f"[sparc] review save fail: {e}")
    return review


def _git_diff_staged_and_working() -> str:
    try:
        r = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True, text=True, timeout=20, cwd=REPO_PATH,
        )
        return r.stdout or ""
    except Exception as e:
        log.warning(f"[sparc] git diff fail: {e}")
        return ""


def run_agent(task):
    """Pipeline: Codex planner -> (SPARC-lite si brief grande) -> Claude Code CLI -> Review opcional -> Codex summarizer."""
    start = time.time()
    modified_files, git_info, errors = [], {}, []
    sparc_spec_md = ""
    sparc_arch_md = ""
    sparc_review_md = ""
    sparc_active = False

    # Paso 1: plan + mensaje inicial de "planificando"
    _tg_plan_id = _tg_send(task.chat_id, "🧠 *Agent Worker — Planificando*\n_armando prompt técnico con Codex..._")
    log.info(f"[{task.task_id}] codex_plan: building prompt...")
    enhanced = codex_plan(task.user_text)
    log.info(f"[{task.task_id}] codex_plan OK ({len(enhanced)} chars)")

    # Paso 1b: SPARC-lite si el brief es grande (>500 tokens aprox)
    combined_tokens = _approx_tokens(task.user_text) + _approx_tokens(enhanced)
    if combined_tokens >= SPARC_TOKEN_THRESHOLD:
        sparc_active = True
        log.info(f"[{task.task_id}] SPARC-lite ON (~{combined_tokens} tokens)")
        if _tg_plan_id:
            _tg_edit(task.chat_id, _tg_plan_id,
                     f"🧠 *Brief grande* (~{combined_tokens} tokens)\n└─ SPARC-lite: generando Spec…")
        sparc_spec_md = _sparc_spec(task.user_text, task.task_id)
        if sparc_spec_md and _tg_plan_id:
            _tg_edit(task.chat_id, _tg_plan_id,
                     f"🧠 *SPARC* · Spec OK ({len(sparc_spec_md)} chars)\n└─ generando Arch…")
        sparc_arch_md = _sparc_arch(sparc_spec_md, task.user_text, task.task_id)
        if sparc_arch_md and _tg_plan_id:
            _tg_edit(task.chat_id, _tg_plan_id,
                     f"🧠 *SPARC* · Spec+Arch OK\n└─ paso al executor")
        # Enriquecer el prompt del executor con Spec+Arch
        if sparc_spec_md or sparc_arch_md:
            enhanced = (
                f"{enhanced}\n\n"
                f"── SPEC ──\n{sparc_spec_md}\n\n"
                f"── ARCH ──\n{sparc_arch_md}\n\n"
                f"Seguí el Spec y la Arch. Si detectás que algo del Spec no se puede "
                f"cumplir, decílo explícitamente al final en vez de ignorarlo."
            )
    else:
        if _tg_plan_id:
            _tg_edit(
                task.chat_id, _tg_plan_id,
                f"🧠 *Planificado* · _{len(enhanced)} chars_\n└─ paso al executor",
            )

    # Paso 2: executor con streaming en vivo
    log.info(f"[{task.task_id}] Launching Claude Code CLI with stream (sparc={sparc_active})...")
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

    # Paso 2b: SPARC Review si el diff es sustancioso y SPARC está activo
    if sparc_active and modified_files:
        diff_text = _git_diff_staged_and_working()
        diff_lines = diff_text.count("\n")
        if diff_lines >= SPARC_REVIEW_DIFF_LINES:
            log.info(f"[{task.task_id}] SPARC Review ON (diff {diff_lines} líneas, modelo={SPARC_MODEL_REVIEW})")
            _tg_send(task.chat_id,
                     f"🔎 *SPARC Review* — diff {diff_lines} líneas, corriendo Opus…")
            sparc_review_md = _sparc_review(
                task.user_text, sparc_spec_md, sparc_arch_md, diff_text, task.task_id,
            )
            if sparc_review_md:
                # Mandar el review como mensaje separado — el usuario lo puede leer
                # antes del summary final de Codex.
                _tg_send(task.chat_id, f"🔎 *Review del diff*\n\n{sparc_review_md[:3500]}")

    log.info(f"[{task.task_id}] codex_summarize...")
    final_summary = codex_summarize(task.user_text, raw_output or "(sin output)", modified_files, git_info)

    # Mandar el resumen final como mensaje nuevo (no edit), para que quede
    # separado del bloque de progreso y sea fácil de leer.
    _tg_send(task.chat_id, final_summary)

    return WorkerResult(
        task_id=task.task_id,
        status="ok" if not errors else ("partial" if raw_output else "error"),
        summary=final_summary,
        modified_files=modified_files,
        git_info={**git_info, "sparc": sparc_active, "sparc_review": bool(sparc_review_md)},
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


@app.get("/context")
def repo_context(limit: int = 20):
    """Snapshot del repo cukinator-bot: branch, head, log reciente, estado.
    Usado por el panel web para alimentar el system prompt del Cukinator Bot
    (y que pueda responder qué se vino trabajando aunque la sesión arranque vacía).
    """
    def _run(cmd: str, timeout: int = 8) -> str:
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=timeout, cwd=REPO_PATH)
            return (r.stdout or "").strip()
        except Exception as e:
            return f"(error: {e})"

    return {
        "repo": REPO_PATH,
        "branch": _run("git rev-parse --abbrev-ref HEAD"),
        "head": _run("git rev-parse --short HEAD"),
        "head_full": _run("git rev-parse HEAD"),
        "head_msg": _run("git log -1 --pretty=%s"),
        "head_date": _run("git log -1 --pretty=%ci"),
        "log": _run(f"git log --oneline -{max(1, min(limit, 100))}"),
        "log_detailed": _run(
            f"git log -{max(1, min(limit, 30))} --pretty=format:'%h|%ci|%an|%s' "
        ),
        "status_short": _run("git status -s"),
        "files_changed_30d": _run(
            "git log --since='30 days ago' --pretty=format: --name-only | "
            "sort | uniq -c | sort -rn | head -20"
        ),
        "branches": _run("git branch -a --sort=-committerdate | head -10"),
    }


# ── /task/stream ─────────────────────────────────────────────────────────
# Endpoint SSE para que el panel web (cukinator-web) consuma el progreso del
# Claude Code CLI en vivo, igual que se ve en VS Code.
# Eventos enviados (cada uno como `data: {json}\n\n`):
#   {type: "status",    data: {phase, msg}}
#   {type: "plan",      data: {enhanced_prompt, tokens}}      (tras Codex planner)
#   {type: "claude",    data: <json crudo del CLI>}           (tool_use, text, etc.)
#   {type: "git",       data: {modified_files, commit, branch}}
#   {type: "summary",   data: {text}}                         (resumen Codex)
#   {type: "done",      data: {duration_s, status, errors}}
#   {type: "error",     data: {message}}

class StreamTask(BaseModel):
    task_id: str
    user_text: str
    chat_id: int = 0          # 0 = sin Telegram, solo SSE
    skip_codex_plan: bool = False
    skip_codex_summary: bool = False


def _sse(event_type: str, data) -> bytes:
    payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def _build_repo_context_preamble() -> str:
    """Snapshot del repo precargado en el prompt — para que Claude pueda
    responder 'qué venimos haciendo' sin gastar tool calls explorando."""
    def _run(cmd: str, timeout: int = 6) -> str:
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=timeout, cwd=REPO_PATH)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    branch = _run("git rev-parse --abbrev-ref HEAD") or "?"
    head_oneliner = _run("git log -1 --oneline") or "?"
    log = _run("git log --oneline -15")
    status = _run("git status -s")
    files_30d = _run(
        "git log --since='30 days ago' --pretty=format: --name-only | "
        "sort | uniq -c | sort -rn | head -10"
    )
    return (
        "── CONTEXTO ACTUAL DEL REPO cukinator-bot (precargado, no hace falta git log) ──\n"
        f"Branch: {branch}\n"
        f"HEAD: {head_oneliner}\n\n"
        f"Últimos commits:\n{log}\n\n"
        f"Estado uncommitted (git status -s):\n{status or '(limpio)'}\n\n"
        f"Archivos más tocados últimos 30 días:\n{files_30d or '(sin actividad)'}\n"
        "── FIN CONTEXTO ──\n\n"
    )


async def _stream_task(task: StreamTask, request: Request) -> AsyncGenerator[bytes, None]:
    started = time.time()
    errors: list = []
    modified_files: list = []
    git_info: dict = {}
    summary_text: str = ""

    yield _sse("status", {"phase": "starting", "msg": "Iniciando agente"})

    # 1) Pull repo (ANTES del context preamble para tener data fresca)
    yield _sse("status", {"phase": "git_pull", "msg": "git fetch + checkout main"})
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                "git fetch origin && git checkout main && git pull origin main",
                shell=True, cwd=REPO_PATH, timeout=30, capture_output=True,
            ),
        )
    except Exception as e:
        errors.append(f"git pull: {e}")

    # 2) Codex planner (opcional)
    enhanced = task.user_text
    if not task.skip_codex_plan:
        yield _sse("status", {"phase": "planning", "msg": "Codex armando plan técnico (gpt-5-codex)"})
        loop = asyncio.get_event_loop()
        enhanced = await loop.run_in_executor(None, codex_plan, task.user_text)
        yield _sse("plan", {"enhanced_prompt": enhanced[:4000], "len": len(enhanced)})

    # 3) Pre-cargar contexto del repo en el prompt para Claude (responde
    # "qué venimos trabajando" al toque sin gastar tool calls).
    preamble = _build_repo_context_preamble()
    enhanced = preamble + enhanced

    # 3) Spawn claude CLI con stream-json
    yield _sse("status", {"phase": "executing", "msg": "Claude Code CLI ejecutando con tools reales"})

    env = dict(os.environ)
    ak = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if ak:
        env["ANTHROPIC_API_KEY"] = ak
    env["CUKI_TENANT"] = _resolve_tenant_safe(task.chat_id)
    env["CUKI_CHAT_ID"] = str(task.chat_id or 0)
    env["CUKI_TASK_ID"] = task.task_id or ""
    env["REPO_PATH"] = REPO_PATH

    proc = None
    final_text = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", enhanced,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=REPO_PATH,
            env=env,
        )
    except FileNotFoundError:
        errors.append("claude CLI no encontrado en PATH")
        yield _sse("error", {"message": "Claude CLI no instalado en el VPS"})
        yield _sse("done", {"duration_s": round(time.time() - started, 1),
                            "status": "error", "errors": errors})
        return

    # Stream stdout
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace").strip()
        if not s:
            continue
        try:
            evt = json.loads(s)
        except json.JSONDecodeError:
            continue
        # Capturar texto final
        if evt.get("type") == "assistant":
            for block in (evt.get("message") or {}).get("content", []) or []:
                if block.get("type") == "text" and block.get("text"):
                    final_text = block["text"]
        if evt.get("type") == "result":
            rt = evt.get("result") or evt.get("content") or ""
            if rt:
                final_text = rt if isinstance(rt, str) else str(rt)
        yield _sse("claude", evt)
        # Si el cliente cierra la conexión, abortamos el subprocess
        if await request.is_disconnected():
            log.info(f"[{task.task_id}] client disconnected, killing claude CLI")
            try:
                proc.terminate()
            except Exception:
                pass
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass

    if proc.returncode and proc.returncode != 0:
        try:
            stderr_raw = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
        except Exception:
            stderr_raw = ""
        errors.append(f"claude rc={proc.returncode}: {stderr_raw[:300]}")

    # 4) Git status / commit info
    yield _sse("status", {"phase": "git_check", "msg": "Detectando cambios"})
    try:
        diff = subprocess.run(["git", "diff", "--name-only", "HEAD"],
                              capture_output=True, text=True, timeout=10, cwd=REPO_PATH)
        if diff.stdout.strip():
            modified_files = diff.stdout.strip().splitlines()
        log_r = subprocess.run(["git", "log", "--oneline", "-1"],
                               capture_output=True, text=True, timeout=10, cwd=REPO_PATH)
        if log_r.stdout:
            git_info["commit"] = log_r.stdout.strip()
        branch_r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                  capture_output=True, text=True, timeout=10, cwd=REPO_PATH)
        if branch_r.stdout:
            git_info["branch"] = branch_r.stdout.strip()
    except Exception as e:
        errors.append(f"git_check: {e}")

    yield _sse("git", {"modified_files": modified_files, **git_info})

    # 5) Codex summary (opcional)
    if not task.skip_codex_summary:
        yield _sse("status", {"phase": "summarizing", "msg": "Codex resumiendo en criollo"})
        loop = asyncio.get_event_loop()
        summary_text = await loop.run_in_executor(
            None,
            codex_summarize,
            task.user_text, final_text or "(sin output)", modified_files, git_info,
        )
        yield _sse("summary", {"text": summary_text})

    yield _sse("done", {
        "duration_s": round(time.time() - started, 1),
        "status": "ok" if not errors else ("partial" if (final_text or modified_files) else "error"),
        "errors": errors,
        "task_id": task.task_id,
    })


@app.post("/task/stream")
async def task_stream(task: StreamTask, request: Request,
                       x_worker_secret: str = Header(None)):
    if x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    global _current_task
    if not _repo_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail=f"Worker ocupado: {_current_task}",
        )
    _current_task = {"task_id": task.task_id, "started_at": time.time(), "stream": True}

    async def generator():
        global _current_task
        try:
            async for chunk in _stream_task(task, request):
                yield chunk
        finally:
            _repo_lock.release()
            _current_task = None

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable nginx buffering for SSE
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3335, log_level="info")
