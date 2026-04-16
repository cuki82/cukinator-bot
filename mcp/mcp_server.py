"""
mcp_server.py — Cukinator MCP Server

Un solo servidor con 4 namespaces montados via FastMCP + ASGI.
Corre en Railway en un servicio separado al bot.

Namespaces:
  /ops/mcp      → VPS operations (docker, ssh, servicios)
  /github/mcp   → GitHub repo control (branches, files, PRs)
  /memory/mcp   → Memory & history DB
  /knowledge/mcp → Knowledge base (reaseguros, documentos)

Claude los consume así:
  mcp_servers=[
    {"type": "url", "url": "https://tu-mcp.railway.app/ops/mcp", "name": "ops"},
    {"type": "url", "url": "https://tu-mcp.railway.app/github/mcp", "name": "github"},
    ...
  ]
"""

import os
import json
import sqlite3
import urllib.request
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

# ── Configuración ──────────────────────────────────────────────────────────────

GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO   = os.environ.get("GITHUB_REPO", "cuki82/cukinator-bot")
DB_PATH       = os.environ.get("DB_PATH", "/data/memory.db")
VPS_HOST      = os.environ.get("VPS_HOST", "31.97.151.119")
VPS_USER      = os.environ.get("VPS_USER", "cukibot")
VPS_KEY       = os.environ.get("VPS_PRIVATE_KEY", "")
MCP_SECRET    = os.environ.get("MCP_SECRET", "")  # opcional: auth token


# ═══════════════════════════════════════════════════════════════════════════════
# 1. OPS SERVER — VPS operations
# ═══════════════════════════════════════════════════════════════════════════════

ops = FastMCP("cukinator-ops")


def _ssh_exec(command: str, timeout: int = 30) -> dict:
    """Ejecuta comando SSH en el VPS via paramiko."""
    try:
        import paramiko, io
        key_content = VPS_KEY.replace("\\n", "\n")
        key_file = io.StringIO(key_content)
        try:
            pkey = paramiko.Ed25519Key.from_private_key(key_file)
        except Exception:
            key_file.seek(0)
            pkey = paramiko.RSAKey.from_private_key(key_file)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(VPS_HOST, username=VPS_USER, pkey=pkey,
                      timeout=10, look_for_keys=False, allow_agent=False)
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        client.close()
        return {"stdout": out[:3000], "stderr": err[:500], "returncode": code, "success": code == 0}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "returncode": -1, "success": False}


@ops.tool()
def vps_status() -> str:
    """Estado general del VPS: uptime, disco, RAM, containers Docker."""
    r = _ssh_exec("uptime && echo '---' && df -h / | tail -1 && echo '---' && free -h | grep Mem && echo '---' && docker ps --format 'table {{.Names}}\t{{.Status}}'")
    return r["stdout"] if r["success"] else f"Error: {r['stderr']}"


@ops.tool()
def vps_exec(command: str, timeout: int = 30) -> str:
    """
    Ejecuta un comando SSH en el VPS.
    
    Args:
        command: Comando bash a ejecutar
        timeout: Timeout en segundos (default 30)
    """
    r = _ssh_exec(command, timeout=timeout)
    if r["success"]:
        return r["stdout"] or "(sin output)"
    return f"Error (exit {r['returncode']}): {r['stderr']}"


@ops.tool()
def docker_ps() -> str:
    """Lista todos los containers Docker con su estado y puertos."""
    r = _ssh_exec("docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'")
    return r["stdout"] if r["success"] else f"Error: {r['stderr']}"


@ops.tool()
def docker_logs(container: str, lines: int = 50) -> str:
    """
    Ver logs de un container Docker.
    
    Args:
        container: Nombre del container (ej: open-webui-3000, litellm)
        lines: Últimas N líneas (default 50)
    """
    r = _ssh_exec(f"docker logs {container} --tail {lines} 2>&1")
    return r["stdout"] if r["success"] else f"Error: {r['stderr']}"


@ops.tool()
def docker_restart(container: str) -> str:
    """
    Reinicia un container Docker.
    
    Args:
        container: Nombre del container a reiniciar
    """
    r = _ssh_exec(f"docker restart {container}")
    return f"Container {container} reiniciado." if r["success"] else f"Error: {r['stderr']}"


@ops.tool()
def service_health() -> str:
    """Chequea el health de los servicios principales: Open WebUI, LiteLLM, scraper."""
    checks = {
        "open-webui": f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:3000",
        "litellm":    f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:4000/health",
        "scraper":    f"curl -s http://localhost:3334/health",
    }
    results = []
    for name, cmd in checks.items():
        r = _ssh_exec(cmd, timeout=8)
        status = r["stdout"].strip() if r["success"] else "error"
        results.append(f"{name}: {status}")
    return "\n".join(results)


@ops.tool()
def read_file_vps(path: str) -> str:
    """
    Lee el contenido de un archivo en el VPS.
    
    Args:
        path: Path absoluto del archivo (ej: /opt/scraper-reservas/scraper.py)
    """
    r = _ssh_exec(f"cat {path}")
    return r["stdout"] if r["success"] else f"Error: {r['stderr']}"


@ops.tool()
def write_file_vps(path: str, content: str) -> str:
    """
    Escribe un archivo en el VPS via SSH.
    
    Args:
        path: Path absoluto del archivo
        content: Contenido completo del archivo
    """
    try:
        import paramiko, io
        key_content = VPS_KEY.replace("\\n", "\n")
        key_file = io.StringIO(key_content)
        try:
            pkey = paramiko.Ed25519Key.from_private_key(key_file)
        except Exception:
            key_file.seek(0)
            pkey = paramiko.RSAKey.from_private_key(key_file)

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(VPS_HOST, username=VPS_USER, pkey=pkey,
                      timeout=10, look_for_keys=False, allow_agent=False)
        sftp = client.open_sftp()
        with sftp.file(path, "w") as f:
            f.write(content.encode("utf-8"))
        sftp.close()
        client.close()
        return f"Archivo escrito: {path} ({len(content)} chars)"
    except Exception as e:
        return f"Error escribiendo {path}: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GITHUB SERVER — Repo control
# ═══════════════════════════════════════════════════════════════════════════════

github = FastMCP("cukinator-github")

PROTECTED_FILES = frozenset([
    "bot.py", "bot_core.py", "multi_agent.py", "orchestrator.py",
    "intent_router.py", "mcp_server.py", "handlers/message_handler.py",
    "handlers/callback_handler.py", "Dockerfile", "requirements.txt"
])


def _gh_api(method: str, endpoint: str, payload: dict = None) -> dict:
    """Llama a la GitHub API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    try:
        data = json.dumps(payload).encode() if payload else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:300], "status": e.code}
    except Exception as e:
        return {"error": str(e)}


@github.tool()
def repo_status() -> str:
    """Estado actual del repo: branch principal, último commit, branches activas."""
    # Último commit en main
    commits = _gh_api("GET", "commits?per_page=3&sha=main")
    if "error" in commits:
        return f"Error: {commits['error']}"

    lines = ["**Últimos commits en main:**"]
    if isinstance(commits, list):
        for c in commits[:3]:
            sha = c["sha"][:7]
            msg = c["commit"]["message"].split("\n")[0][:70]
            date = c["commit"]["author"]["date"][:10]
            lines.append(f"- `{sha}` {date} — {msg}")

    # Branches
    branches = _gh_api("GET", "branches")
    if isinstance(branches, list):
        lines.append(f"\n**Branches:** {', '.join(b['name'] for b in branches)}")

    return "\n".join(lines)


@github.tool()
def read_file_github(path: str, branch: str = "main") -> str:
    """
    Lee el contenido de un archivo del repo en GitHub.
    
    Args:
        path: Path del archivo (ej: bot_core.py, modules/reservas.py)
        branch: Branch (default: main)
    """
    import base64
    data = _gh_api("GET", f"contents/{path}?ref={branch}")
    if "error" in data:
        return f"Error: {data['error']}"
    if "content" in data:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")[:8000]
    return "Archivo no encontrado o es un directorio."


@github.tool()
def push_file(path: str, content: str, message: str, branch: str = "bot-changes") -> str:
    """
    Crea o actualiza un archivo en GitHub. SIEMPRE en bot-changes, nunca en main.
    Archivos core del bot están protegidos y no pueden ser modificados.
    
    Args:
        path: Path del archivo en el repo
        content: Contenido completo del archivo
        message: Mensaje del commit
        branch: Branch (default: bot-changes, forzado si se intenta main)
    """
    import base64

    # Forzar bot-changes
    if branch == "main":
        branch = "bot-changes"

    # Protección de archivos core
    if path in PROTECTED_FILES:
        return f"Bloqueado: `{path}` es un archivo core protegido. Solo se puede modificar desde la sesión de desarrollo."

    # Obtener SHA si existe
    existing = _gh_api("GET", f"contents/{path}?ref={branch}")
    sha = existing.get("sha") if "sha" in existing else None

    # Si no existe en bot-changes, buscar en main para el SHA base
    if not sha and branch != "main":
        main_file = _gh_api("GET", f"contents/{path}?ref=main")
        sha = main_file.get("sha")

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    result = _gh_api("PUT", f"contents/{path}", payload)
    if "error" in result:
        return f"Error push: {result['error']}"

    action = "updated" if sha else "created"
    commit_sha = result.get("content", {}).get("sha", "")[:7]
    return f"Push OK: `{path}` {action} en `{branch}` (sha: {commit_sha})\nUsá create_pr para abrir el Pull Request."


@github.tool()
def create_pr(title: str, body: str, head: str = "bot-changes") -> str:
    """
    Crea un Pull Request desde bot-changes → main.
    
    Args:
        title: Título del PR
        body: Descripción de los cambios
        head: Branch origen (default: bot-changes)
    """
    result = _gh_api("POST", "pulls", {
        "title": title,
        "body": body,
        "head": head,
        "base": "main",
    })
    if "error" in result:
        if "422" in str(result.get("status", "")):
            return "Ya hay un PR abierto para esta branch. Mergealo o cerralo primero."
        return f"Error: {result['error']}"

    return f"PR #{result['number']} creado: {result['html_url']}"


@github.tool()
def list_prs(state: str = "open") -> str:
    """
    Lista los Pull Requests del repo.
    
    Args:
        state: Estado del PR (open, closed, all)
    """
    prs = _gh_api("GET", f"pulls?state={state}&per_page=10")
    if "error" in prs:
        return f"Error: {prs['error']}"
    if not prs:
        return f"No hay PRs {state}."
    lines = [f"**PRs {state}:**"]
    for pr in prs:
        lines.append(f"- #{pr['number']} `{pr['head']['ref']}` → `{pr['base']['ref']}`: {pr['title']}")
        lines.append(f"  {pr['html_url']}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MEMORY SERVER — Historial y memoria
# ═══════════════════════════════════════════════════════════════════════════════

memory = FastMCP("cukinator-memory")


def _db_query(sql: str, params: tuple = ()) -> list:
    """Ejecuta una query en la DB de memoria."""
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(sql, params).fetchall()
        con.close()
        return rows
    except Exception as e:
        return []


@memory.tool()
def search_memory(query: str, chat_id: int = 8626420783, limit: int = 10) -> str:
    """
    Busca en el historial de conversaciones y hechos guardados.
    
    Args:
        query: Texto a buscar
        chat_id: ID del chat (default: owner)
        limit: Máximo de resultados
    """
    # Buscar en mensajes
    msgs = _db_query(
        "SELECT role, content, timestamp FROM messages WHERE chat_id=? AND content LIKE ? ORDER BY timestamp DESC LIMIT ?",
        (chat_id, f"%{query}%", limit)
    )
    # Buscar en memory_index
    facts = _db_query(
        "SELECT type, title, content FROM memory_index WHERE chat_id=? AND (title LIKE ? OR content LIKE ?) LIMIT ?",
        (chat_id, f"%{query}%", f"%{query}%", limit)
    )

    lines = []
    if facts:
        lines.append(f"**Hechos ({len(facts)}):**")
        for f in facts:
            lines.append(f"- [{f[0]}] {f[1]}: {f[2][:200]}")
    if msgs:
        lines.append(f"\n**Mensajes ({len(msgs)}):**")
        for m in msgs[:5]:
            lines.append(f"- {m[0]} ({m[2][:10]}): {m[1][:150]}")

    return "\n".join(lines) if lines else f"No encontré resultados para '{query}'."


@memory.tool()
def save_fact(content: str, fact_type: str = "fact", title: str = "",
              chat_id: int = 8626420783) -> str:
    """
    Guarda un hecho importante en la memoria persistente.
    
    Args:
        content: El hecho a guardar
        fact_type: Tipo (fact, preference, decision, context)
        title: Título opcional
        chat_id: ID del chat
    """
    try:
        import hashlib
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            INSERT INTO memory_index (chat_id, type, title, content, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (chat_id, fact_type, title, content, datetime.utcnow().isoformat()))
        con.commit()
        con.close()
        return f"Hecho guardado: [{fact_type}] {title or content[:50]}"
    except Exception as e:
        return f"Error guardando: {e}"


@memory.tool()
def get_recent_history(chat_id: int = 8626420783, limit: int = 20) -> str:
    """
    Devuelve el historial reciente de conversaciones.
    
    Args:
        chat_id: ID del chat
        limit: Últimos N mensajes
    """
    rows = _db_query(
        "SELECT role, content, timestamp FROM messages WHERE chat_id=? ORDER BY timestamp DESC LIMIT ?",
        (chat_id, limit)
    )
    if not rows:
        return "No hay historial."
    lines = [f"**Últimos {limit} mensajes:**"]
    for role, content, ts in reversed(rows):
        lines.append(f"[{ts[:16]}] {role}: {content[:200]}")
    return "\n".join(lines)


@memory.tool()
def memory_stats(chat_id: int = 8626420783) -> str:
    """Estadísticas de memoria: mensajes, sesiones, hechos guardados."""
    msgs = _db_query("SELECT COUNT(*) FROM messages WHERE chat_id=?", (chat_id,))
    sessions = _db_query("SELECT COUNT(*) FROM sessions WHERE chat_id=?", (chat_id,))
    facts = _db_query("SELECT COUNT(*) FROM memory_index WHERE chat_id=?", (chat_id,))
    return (f"Memoria del usuario:\n"
            f"- Mensajes: {msgs[0][0] if msgs else 0}\n"
            f"- Sesiones: {sessions[0][0] if sessions else 0}\n"
            f"- Hechos guardados: {facts[0][0] if facts else 0}")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. KNOWLEDGE SERVER — Knowledge base
# ═══════════════════════════════════════════════════════════════════════════════

knowledge = FastMCP("cukinator-knowledge")


@knowledge.tool()
def search_knowledge(query: str, limit: int = 5) -> str:
    """
    Busca en la knowledge base interna (reaseguros, documentos técnicos).
    
    Args:
        query: Término o concepto a buscar
        limit: Máximo de resultados por categoría
    """
    concepts = _db_query(
        "SELECT term, definition, domain FROM reinsurance_concepts WHERE term LIKE ? OR definition LIKE ? LIMIT ?",
        (f"%{query}%", f"%{query}%", limit)
    )
    chunks = _db_query(
        "SELECT content, source FROM reinsurance_chunks WHERE content LIKE ? LIMIT ?",
        (f"%{query}%", limit)
    )
    qa = _db_query(
        "SELECT question, answer FROM reinsurance_qa WHERE question LIKE ? OR answer LIKE ? LIMIT ?",
        (f"%{query}%", f"%{query}%", limit)
    )

    lines = []
    if concepts:
        lines.append(f"**Conceptos ({len(concepts)}):**")
        for term, definition, domain in concepts:
            lines.append(f"- **{term}** ({domain}): {definition[:300]}")
    if qa:
        lines.append(f"\n**Q&A ({len(qa)}):**")
        for question, answer in qa[:3]:
            lines.append(f"- Q: {question}\n  A: {answer[:200]}")
    if chunks:
        lines.append(f"\n**Fragmentos ({len(chunks)}):**")
        for content, source in chunks[:2]:
            lines.append(f"- [{source}] {content[:300]}")

    return "\n".join(lines) if lines else f"No encontré nada sobre '{query}' en la KB."


@knowledge.tool()
def list_documents(doc_type: Optional[str] = None) -> str:
    """
    Lista los documentos disponibles en la knowledge base.
    
    Args:
        doc_type: Filtrar por tipo (ej: 'wording', 'normativa', 'tecnico')
    """
    if doc_type:
        rows = _db_query(
            "SELECT title, type, organization, created_at FROM reinsurance_documents WHERE type=? ORDER BY created_at DESC",
            (doc_type,)
        )
    else:
        rows = _db_query(
            "SELECT title, type, organization, created_at FROM reinsurance_documents ORDER BY created_at DESC LIMIT 20"
        )

    if not rows:
        return "No hay documentos en la KB." + (f" (tipo: {doc_type})" if doc_type else "")

    lines = [f"**Documentos en KB ({len(rows)}):**"]
    for title, dtype, org, created in rows:
        lines.append(f"- [{dtype}] {title} ({org or 'sin org'}) — {created[:10]}")
    return "\n".join(lines)


@knowledge.tool()
def kb_stats() -> str:
    """Estadísticas de la knowledge base: documentos, conceptos, QA, chunks."""
    docs     = _db_query("SELECT COUNT(*) FROM reinsurance_documents")
    concepts = _db_query("SELECT COUNT(*) FROM reinsurance_concepts")
    qa       = _db_query("SELECT COUNT(*) FROM reinsurance_qa")
    chunks   = _db_query("SELECT COUNT(*) FROM reinsurance_chunks")
    return (f"Knowledge Base:\n"
            f"- Documentos: {docs[0][0] if docs else 0}\n"
            f"- Conceptos: {concepts[0][0] if concepts else 0}\n"
            f"- Q&A pairs: {qa[0][0] if qa else 0}\n"
            f"- Chunks: {chunks[0][0] if chunks else 0}")


@knowledge.tool()
def add_document(title: str, content: str, doc_type: str = "general",
                 organization: str = "") -> str:
    """
    Indexa un nuevo documento en la knowledge base.
    
    Args:
        title: Título del documento
        content: Contenido completo
        doc_type: Tipo (wording, normativa, tecnico, general)
        organization: Organización de origen (ej: Lloyd's, SSN)
    """
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.execute("""
            INSERT INTO reinsurance_documents (title, type, organization, created_at)
            VALUES (?, ?, ?, ?)
        """, (title, doc_type, organization, datetime.utcnow().isoformat()))
        doc_id = cur.lastrowid

        # Chunking básico — cada 500 palabras
        words = content.split()
        chunks = [" ".join(words[i:i+500]) for i in range(0, len(words), 500)]
        for i, chunk in enumerate(chunks):
            con.execute("""
                INSERT INTO reinsurance_chunks (document_id, content, chunk_index, source)
                VALUES (?, ?, ?, ?)
            """, (doc_id, chunk, i, title))

        con.commit()
        con.close()
        return f"Documento '{title}' indexado: {len(chunks)} chunks. Doc ID: {doc_id}"
    except Exception as e:
        return f"Error indexando: {e}"


# ═══════════════════════════════════════════════════════════════════════════════
# ASGI APP — Monta los 4 servidores en paths distintos
# ═══════════════════════════════════════════════════════════════════════════════

ops_app       = ops.streamable_http_app()
github_app    = github.streamable_http_app()
memory_app    = memory.streamable_http_app()
knowledge_app = knowledge.streamable_http_app()

app = Starlette(routes=[
    Mount("/ops",       app=ops_app),
    Mount("/github",    app=github_app),
    Mount("/memory",    app=memory_app),
    Mount("/knowledge", app=knowledge_app),
])


# ── Health check raíz ──────────────────────────────────────────────────────────
from starlette.requests import Request
from starlette.responses import JSONResponse

async def health(request: Request):
    return JSONResponse({
        "status": "ok",
        "servers": ["ops", "github", "memory", "knowledge"],
        "endpoints": {
            "ops":       "/ops/mcp",
            "github":    "/github/mcp",
            "memory":    "/memory/mcp",
            "knowledge": "/knowledge/mcp",
        }
    })

from starlette.routing import Route
app.routes.insert(0, Route("/health", health))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 3350))
    uvicorn.run(app, host="0.0.0.0", port=port)
