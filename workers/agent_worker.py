import os, sys, subprocess, logging, time, threading
from pathlib import Path
from typing import Optional

# Load vault secrets before any API client initialization
try:
    sys.path.insert(0, os.environ.get("REPO_PATH", "/home/cukibot/cukinator-bot"))
    from services.vault import load_all_to_env
    load_all_to_env()
except Exception as _ve:
    pass  # fallback to env vars

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


def codex_plan(user_text: str) -> str:
    """Codex transforma el mensaje del usuario en un prompt detallado para Claude."""
    client = _codex_client()
    if not client:
        return user_text
    try:
        system = "Sos un prompt engineer experto en tareas DevOps/coding sobre un bot Python. El usuario manda una descripcion informal. Convertila en un prompt tecnico detallado para un agente ejecutor (Claude) que tiene estos tools: bash_exec, read_file, write_file, git_status, git_commit_push, run_tests. El prompt debe incluir: objetivo claro, archivos relevantes a leer, pasos concretos, criterio de exito. Respondeme SOLO con el prompt final en espanol rioplatense, sin preambulos."
        r = client.chat.completions.create(
            model=CODEX_MODEL, max_tokens=800,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_text}]
        )
        plan = r.choices[0].message.content.strip()
        log.info(f"[codex_plan] {len(plan)} chars")
        return plan
    except Exception as e:
        log.error(f"codex_plan error: {e}")
        return user_text


def codex_summarize(user_text: str, raw_summary: str, modified_files: list, git_info: dict) -> str:
    """Codex formatea el resultado de Claude para el usuario."""
    client = _codex_client()
    if not client:
        return raw_summary
    try:
        context = f"Pedido: {user_text}" + chr(10)*2 + f"Resultado tecnico: {raw_summary}" + chr(10)
        if modified_files:
            context += f"Archivos modificados: {modified_files}" + chr(10)
        if git_info.get("commit"):
            context += f"Git: {git_info['commit']}" + chr(10)
        system = "Sos un formateador de respuestas tecnicas para Telegram (rioplatense). Recibis el pedido original + resultado tecnico de un agente. Genera una respuesta clara, concisa, con markdown basico (bullets, bold). Maximo 1500 caracteres. Sin preambulos. Arranca directo con lo relevante."
        r = client.chat.completions.create(
            model=CODEX_MODEL, max_tokens=600,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": context}]
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"codex_summarize error: {e}")
        return raw_summary


def run_agent(task):
    """Pipeline: Codex planner -> Claude Code CLI executor -> Codex summarizer."""
    start = time.time()
    modified_files, git_info, errors = [], {}, []

    log.info(f"[{task.task_id}] codex_plan: building prompt...")
    enhanced = codex_plan(task.user_text)
    log.info(f"[{task.task_id}] codex_plan OK ({len(enhanced)} chars)")

    # Ensure claude CLI has access to the Anthropic key
    env = dict(os.environ)
    ak = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if ak:
        env["ANTHROPIC_API_KEY"] = ak

    log.info(f"[{task.task_id}] Launching Claude Code CLI...")
    try:
        proc = subprocess.run(
            ["claude", "-p", enhanced, "--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=600,
            cwd=REPO_PATH, env=env
        )
        raw_output = proc.stdout
        raw_err = proc.stderr
        log.info(f"[{task.task_id}] Claude Code done, rc={proc.returncode}, stdout={len(raw_output)} chars")
        if proc.returncode != 0:
            errors.append(f"claude CLI rc={proc.returncode}: {raw_err[:500]}")
    except subprocess.TimeoutExpired:
        errors.append("claude CLI timeout (600s)")
        raw_output = ""
    except FileNotFoundError:
        errors.append("claude CLI no encontrado en PATH")
        raw_output = ""
    except Exception as e:
        errors.append(f"claude CLI error: {e}")
        raw_output = ""

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
