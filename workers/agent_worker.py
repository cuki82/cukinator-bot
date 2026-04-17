import os, subprocess, logging, time, threading
from pathlib import Path
from typing import Optional
import anthropic
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

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
_repo_lock = threading.Lock()
_current_task = None
PROTECTED_FILES = ["core/bot.py", "core/bot_core.py", "Dockerfile", "requirements.txt"]


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



WORKER_TOOLS = [
    {"name": "bash_exec", "description": "Ejecuta comando bash en el repo.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Lee archivo del repo. Usar SIEMPRE antes de modificar.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Escribe archivo. Core files bloqueados automaticamente.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "git_status", "description": "Estado del repo.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "git_commit_push", "description": "Commit y push directo a main.",
     "input_schema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}},
    {"name": "run_tests", "description": "Valida sintaxis Python. Usar siempre antes de commit.",
     "input_schema": {"type": "object", "properties": {}}}
]

WORKER_SYSTEM = (
    "Sos el Operational Agent de Cukinator. Ejecutas tareas de codigo sobre el repo del bot.\n\n"
    "REGLAS:\n"
    "1. SIEMPRE lee un archivo antes de modificarlo.\n"
    "2. SIEMPRE corre run_tests antes del commit.\n"
    "3. core/bot.py, core/bot_core.py, Dockerfile, requirements.txt NO pueden modificarse.\n"
    "4. Push directo a main - no hay branches ni PRs.\n\n"
    "FLUJO: git_status > read_file > write_file > run_tests > git_commit_push\n\n"
    "Responde en espanol rioplatense."
)


def dispatch_tool(name, inputs):
    if name == "bash_exec":
        r = bash_exec(inputs["command"], timeout=inputs.get("timeout", 60))
        return f"stdout: {r['stdout']}\nstderr: {r['stderr']}\ncode: {r['returncode']}"
    elif name == "read_file":
        r = read_file(inputs["path"])
        if r["success"]:
            return f"[{inputs['path']} - {r['lines']} lineas]\n{r['content']}"
        return f"Error: {r.get('error')}"
    elif name == "write_file":
        r = write_file(inputs["path"], inputs["content"])
        if r["success"]:
            return f"Escrito: {r['path']} ({r['bytes']} bytes)"
        return f"Error: {r.get('error')}"
    elif name == "git_status":
        return git_status()["output"]
    elif name == "git_commit_push":
        r = git_commit_push(inputs["message"])
        if r["success"]:
            return f"OK\nArchivos: {r.get('modified')}\nCommit: {r.get('commit')}"
        return f"Error: {r.get('error')}"
    elif name == "run_tests":
        r = run_tests()
        return f"{'PASS' if r['passed'] else 'FAIL'}\n{r['output']}"
    return f"Tool desconocido: {name}"



def run_agent(task):
    start = time.time()
    modified_files, git_info, errors = [], {}, []
    messages = [{"role": "user", "content": f"Tarea: {task.user_text}"}]

    for i in range(15):
        log.info(f"[{task.task_id}] iter {i+1}")
        try:
            resp = client.messages.create(
                model="claude-opus-4-5", max_tokens=4096,
                system=WORKER_SYSTEM, tools=WORKER_TOOLS, messages=messages
            )
        except Exception as e:
            errors.append(str(e))
            return WorkerResult(task_id=task.task_id, status="error",
                                summary=f"Error API: {e}", errors=errors, duration_s=time.time()-start)

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for blk in resp.content:
                if blk.type == "tool_use":
                    log.info(f"[{task.task_id}] tool={blk.name}")
                    result = dispatch_tool(blk.name, blk.input)
                    if blk.name == "write_file" and "Escrito:" in result:
                        modified_files.append(blk.input.get("path", ""))
                    elif blk.name == "git_commit_push" and "OK" in result:
                        git_info["commit"] = result
                    tool_results.append({"type": "tool_result", "tool_use_id": blk.id, "content": result})
            messages.append({"role": "user", "content": tool_results})
        else:
            parts = [b.text for b in resp.content if hasattr(b, "text") and b.text.strip()]
            return WorkerResult(task_id=task.task_id, status="ok",
                                summary="\n".join(parts) or "Listo.",
                                modified_files=modified_files, git_info=git_info,
                                errors=errors, duration_s=round(time.time()-start, 1))

    return WorkerResult(task_id=task.task_id, status="partial", summary="Limite de iteraciones.",
                        modified_files=modified_files, git_info=git_info,
                        duration_s=round(time.time()-start, 1))


@app.get("/health")
def health():
    return {"status": "ok", "repo": REPO_PATH, "repo_exists": Path(REPO_PATH).exists()}


@app.post("/task", response_model=WorkerResult)
def execute_task(task: CodingTask, x_worker_secret: str = Header(None)):
    if x_worker_secret != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    global _current_task
    if not _repo_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail=f"Worker ocupado: {_current_task}")
    _current_task = {"task_id": task.task_id, "started_at": time.time()}
    try:
        if Path(REPO_PATH).exists():
            bash_exec("git fetch origin && git checkout main && git pull origin main")
        else:
            bash_exec(f"git clone {REPO_REMOTE} {REPO_PATH}", cwd="/home/cukibot")
        return run_agent(task)
    finally:
        _repo_lock.release()
        _current_task = None


@app.get("/status")
def worker_status():
    occ = not _repo_lock.acquire(blocking=False)
    if not occ:
        _repo_lock.release()
    return {"available": not occ, "current_task": _current_task,
            "repo_path": REPO_PATH, "repo_exists": Path(REPO_PATH).exists()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3335, log_level="info")
