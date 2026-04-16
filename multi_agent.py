"""
multi_agent.py — Sistema multi-agente interno para Cukinator.

Arquitectura: todo corre dentro del bot. Los agentes son llamadas
separadas a Claude API con roles, tools y system prompts distintos.

Capas:
  1. Intent Router    — clasifica la intención
  2. Orchestrator     — decide, delega, consolida, responde
  3. Agent Teams      — ejecutan tareas especializadas
  4. MCP Abstraction  — interfaz estándar a tools y sistemas
  5. Repo Safety      — lock, branch forzada, validaciones

Regla central:
  El Orchestrator NUNCA toca código o infraestructura directamente.
  El Operational Agent es el ÚNICO con permisos operativos.
"""

import os
import json
import time
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional
import anthropic

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════════════

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
MODEL_ORCHESTRATOR  = "claude-opus-4-5"    # cerebro principal
MODEL_OPERATIONAL   = "claude-opus-4-5"    # agente operativo
MODEL_ROUTER        = "claude-haiku-4-5"   # clasificador rápido y barato
MODEL_SPECIALIZED   = "claude-opus-4-5"    # agentes especializados

_client = None
def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _client


# ═══════════════════════════════════════════════════════════════════════════════
# CONTRATOS DE DATOS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AgentTask:
    """Tarea que el Orchestrator delega a un agente especializado."""
    intent: str
    user_text: str
    chat_id: int
    user_name: str = ""
    context: dict = field(default_factory=dict)
    constraints: list = field(default_factory=list)
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class AgentResult:
    """Resultado estructurado que devuelve un agente al Orchestrator."""
    status: str           # ok | error | partial
    summary: str          # texto para que el Orchestrator consolide
    agent: str = ""
    data: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    extra_files: list = field(default_factory=list)  # (nombre, bytes, caption)
    pdf_path: str = None
    git_info: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA 1 — INTENT ROUTER
# ═══════════════════════════════════════════════════════════════════════════════

ROUTER_SYSTEM = """Sos un clasificador de intenciones. Analizás el mensaje y devolvés EXACTAMENTE UNA de estas categorías:

conversational
coding_task
research_task
personal_task
astrology_task
reinsurance_task
mixed_task

REGLAS:
- coding_task: editar código del bot, modificar handlers, agregar módulos, tocar GitHub/repo, commits, PRs, cambiar el bot mismo, deploy, Railway, VPS config técnica.
- research_task: buscar info, analizar documentos, comparar fuentes, sintetizar.
- personal_task: memoria del usuario, historial, preferencias, seguimiento de decisiones.
- astrology_task: carta natal, tránsitos, aspectos, interpretaciones astrológicas.
- reinsurance_task: reaseguros, treaty, quota share, wording, normativa de seguros.
- mixed_task: si el mensaje combina claramente dos o más categorías anteriores.
- conversational: TODO lo demás — preguntas, charla, clima, emails, calendario, VPS status/monitoring, reservas, análisis general, explicaciones.

IMPORTANTE: VPS monitoring (ver status, docker ps, logs, uptime) es conversational. VPS config técnica (modificar configs, instalar servicios) es coding_task.

Respondé SOLO con la categoría exacta. Sin explicación."""


def classify_intent(user_text: str) -> str:
    """
    Clasifica el mensaje usando claude-haiku (rápido, barato).
    Fallback a keyword matching si falla.
    """
    try:
        resp = get_client().messages.create(
            model=MODEL_ROUTER,
            max_tokens=15,
            system=ROUTER_SYSTEM,
            messages=[{"role": "user", "content": user_text}]
        )
        result = resp.content[0].text.strip().lower().replace(" ", "_")
        valid = {"conversational", "coding_task", "research_task", "personal_task",
                 "astrology_task", "reinsurance_task", "mixed_task"}
        if result in valid:
            log.info(f"Intent router: '{user_text[:50]}' → {result}")
            return result
    except Exception as e:
        log.warning(f"Intent router API falló ({e}), usando keywords")

    return _keyword_fallback(user_text)


def _keyword_fallback(text: str) -> str:
    text_lower = text.lower()
    if any(k in text_lower for k in [
        "carta natal", "carta astral", "tránsito", "ascendente", "signo solar",
        "astrología", "horóscopo", "planeta en", "casa ", "aspecto "
    ]): return "astrology_task"

    if any(k in text_lower for k in [
        "reaseguro", "treaty", "quota share", "excess of loss", "burning cost",
        "wording", "cedente", "retrocesión", "lloyd's", "ibnr"
    ]): return "reinsurance_task"

    if any(k in text_lower for k in [
        "recordás", "te acordás", "memoria", "historial", "la última vez",
        "mis preferencias", "qué decidimos", "seguimiento"
    ]): return "personal_task"

    if any(k in text_lower for k in [
        "modificá el bot", "cambiá el código", "editá el handler",
        "nuevo módulo", "push a github", "hacé un commit", "abrí un pr",
        "cambiá el system prompt", "agregá un tool", "implementá"
    ]): return "coding_task"

    return "conversational"


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA 2 — REPO SAFETY + LOCK
# ═══════════════════════════════════════════════════════════════════════════════

class RepoLock:
    """
    Lock de concurrencia para operaciones sobre el repositorio.
    Previene ejecuciones simultáneas — un solo writer a la vez.
    """
    def __init__(self, timeout_s: int = 600):
        self._locks: dict[str, dict] = {}
        self._mutex = threading.Lock()
        self.timeout_s = timeout_s

    def acquire(self, repo: str, task_id: str, description: str) -> bool:
        with self._mutex:
            if repo in self._locks:
                info = self._locks[repo]
                # Auto-release si superó el timeout (anti-deadlock)
                if time.time() - info["at"] > self.timeout_s:
                    log.warning(f"RepoLock: auto-release stale lock on {repo}")
                    del self._locks[repo]
                else:
                    return False
            self._locks[repo] = {"id": task_id, "at": time.time(), "desc": description}
            return True

    def release(self, repo: str, task_id: str):
        with self._mutex:
            if repo in self._locks and self._locks[repo]["id"] == task_id:
                del self._locks[repo]

    def status(self, repo: str) -> Optional[dict]:
        with self._mutex:
            info = self._locks.get(repo)
            if info and time.time() - info["at"] > self.timeout_s:
                del self._locks[repo]
                return None
            return info

    def is_locked(self, repo: str) -> bool:
        return self.status(repo) is not None


REPO_LOCK = RepoLock()
DEFAULT_REPO = "cuki82/cukinator-bot"

# Archivos core que nunca puede tocar el Operational Agent
PROTECTED_FILES = frozenset([
    "bot.py", "bot_core.py", "multi_agent.py", "orchestrator.py",
    "intent_router.py", "handlers/message_handler.py",
    "handlers/callback_handler.py", "Dockerfile", "requirements.txt"
])


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA 3 — MCP ABSTRACTION LAYER
# ═══════════════════════════════════════════════════════════════════════════════

class MCPLayer:
    """
    Model Context Protocol — interfaz estándar a tools y sistemas.

    Namespaces:
    - ops-local:       logs, status, restart, health del VPS
    - github-control:  repo status, branches, commits, PRs, archivos
    - railway-control: servicios, deploy, logs, restart
    - memory-db:       memoria, historial, jobs, preferencias
    - knowledge-hub:   documentos, cláusulas, normativa, KB

    Hoy: interfaz definida + routing a implementaciones existentes.
    Los tools reales están en bot_core.py (vps_exec, github_push, etc.)
    """

    REGISTRY = {
        "ops-local": {
            "logs":    "vps_exec: journalctl o docker logs",
            "status":  "vps_exec: docker ps, uptime, df -h",
            "restart": "vps_docker: action=restart",
            "health":  "vps_exec: health checks",
        },
        "github-control": {
            "status":   "vps_exec: git status",
            "branches": "github_push (read mode)",
            "commits":  "vps_exec: git log",
            "prs":      "github_pr",
            "files":    "vps_leer_archivo",
        },
        "railway-control": {
            "status":  "vps_exec: railway status via API",
            "deploy":  "github_push → PR → merge → auto-deploy",
            "logs":    "Railway API logs",
            "restart": "Railway API restart",
        },
        "memory-db": {
            "memory":      "memory_buscar",
            "history":     "get_history_full",
            "preferences": "config_leer",
            "jobs":        "agent_changelog",
        },
        "knowledge-hub": {
            "documents":   "ri_listar_documentos",
            "search":      "ri_consultar",
            "ingest":      "ri_ingestar",
            "stats":       "ri_stats",
        },
    }

    def resolve(self, namespace: str, tool: str) -> str:
        ns = self.REGISTRY.get(namespace, {})
        return ns.get(tool, f"Tool {namespace}/{tool} no encontrado")

    def list_tools(self) -> dict:
        return self.REGISTRY


MCP = MCPLayer()


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA 4 — AGENT TEAMS
# ═══════════════════════════════════════════════════════════════════════════════

def _run_agent_loop(system: str, messages: list, tools: list,
                    model: str, max_iter: int, tool_handler) -> AgentResult:
    """
    Loop genérico de Claude para cualquier agente.
    Ejecuta hasta end_turn o límite de iteraciones.
    Devuelve AgentResult — NUNCA responde al usuario directamente.
    """
    client = get_client()
    extra_files = []
    pdf_path = None

    for i in range(max_iter):
        try:
            resp = client.messages.create(
                model=model, max_tokens=4096,
                system=system, tools=tools, messages=messages
            )
        except Exception as e:
            return AgentResult(status="error", summary=str(e), errors=[str(e)])

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result, ef, pp = tool_handler(block)
                    if ef: extra_files.extend(ef)
                    if pp: pdf_path = pp
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)
                    })
            messages.append({"role": "user", "content": results})
        else:
            parts = [b.text for b in resp.content if hasattr(b, "text") and b.text.strip()]
            summary = "\n".join(parts)
            if not summary:
                try:
                    fr = client.messages.create(
                        model=model, max_tokens=300,
                        system=system,
                        messages=messages + [{"role": "user", "content": "Resumí qué hiciste en 2-3 líneas."}]
                    )
                    summary = next((b.text for b in fr.content if hasattr(b, "text")), "Completado.")
                except Exception:
                    summary = "Tarea completada."

            return AgentResult(
                status="ok", summary=summary,
                extra_files=extra_files, pdf_path=pdf_path
            )

    return AgentResult(status="partial",
                       summary="Límite de iteraciones alcanzado. Intentá con una instrucción más específica.")


# ── Operational Agent ──────────────────────────────────────────────────────────

OPERATIONAL_SYSTEM = """Sos el Operational Agent de Cukinator. Tenés permisos exclusivos para operaciones sobre el sistema.

REGLAS ABSOLUTAS:
1. NUNCA pushees a main directamente. Siempre usá bot-changes o feature/nombre.
2. SIEMPRE leé un archivo antes de modificarlo.
3. NUNCA modifiques archivos core: bot.py, bot_core.py, multi_agent.py, orchestrator.py, Dockerfile.
4. Para archivos core: describí el cambio necesario en el PR body para revisión manual.
5. Después de cualquier push: creá un PR.
6. VPS: ejecutá comandos con vps_exec. Para archivos: vps_leer_archivo / vps_escribir_archivo.
7. GitHub: github_push solo a bot-changes. github_pr para el PR.
8. Devolvé un resumen estructurado de qué hiciste.

FLUJO PARA CAMBIOS DE CÓDIGO:
1. Verificar qué existe (vps_exec: git status, o leer archivos relevantes)
2. Crear/cambiar a branch bot-changes
3. Leer archivos que vas a modificar
4. Hacer los cambios en módulos no protegidos
5. Push a bot-changes
6. Crear PR con descripción clara

FLUJO PARA OPERACIONES VPS:
1. Conectar via vps_exec
2. Ejecutar comando
3. Reportar resultado

Respondé con un resumen claro: qué hiciste, qué archivos tocaste, estado del repo."""


def run_operational_agent(task: AgentTask, available_tools: list,
                          tool_handler) -> AgentResult:
    """
    Operational Agent — único con permisos operativos.
    Usa los tools reales del sistema (github_push, vps_exec, etc.)
    """
    # Verificar repo lock antes de ejecutar
    if REPO_LOCK.is_locked(DEFAULT_REPO):
        info = REPO_LOCK.status(DEFAULT_REPO)
        elapsed = int(time.time() - info["at"])
        return AgentResult(
            status="error",
            summary=f"**Repo ocupado** — tarea en curso ({elapsed}s): {info['desc']}\nEsperá a que termine.",
            agent="operational"
        )

    # Filtrar solo tools operativos
    op_tool_names = {
        "github_push", "github_pr", "vps_exec", "vps_leer_archivo",
        "vps_escribir_archivo", "vps_docker", "buscar_reserva",
        "agent_log", "agent_guardar_secret", "agent_registrar_skill",
        "config_guardar", "config_leer", "config_listar",
    }
    op_tools = [t for t in available_tools if t["name"] in op_tool_names]

    messages = [{"role": "user", "content": f"Tarea operativa: {task.user_text}"}]

    # Adquirir lock
    task_id = task.task_id
    acquired = REPO_LOCK.acquire(DEFAULT_REPO, task_id, task.user_text[:60])

    try:
        result = _run_agent_loop(
            system=OPERATIONAL_SYSTEM,
            messages=messages,
            tools=op_tools,
            model=MODEL_OPERATIONAL,
            max_iter=12,
            tool_handler=tool_handler
        )
        result.agent = "operational"
        return result
    finally:
        if acquired:
            REPO_LOCK.release(DEFAULT_REPO, task_id)


# ── Research Agent ─────────────────────────────────────────────────────────────

RESEARCH_SYSTEM = """Sos el Research Agent de Cukinator. Tu especialidad es buscar, analizar y sintetizar información.

Tenés acceso a:
- search_web: búsqueda en DuckDuckGo
- ri_consultar: knowledge base interna de reaseguros
- ri_listar_documentos: listar documentos disponibles

Devolvé un análisis claro, estructurado y accionable.
No respondas al usuario directamente — tu output lo procesa el Orchestrator."""


def run_research_agent(task: AgentTask, available_tools: list, tool_handler) -> AgentResult:
    research_tool_names = {"search_web", "ri_consultar", "ri_listar_documentos", "ri_stats"}
    tools = [t for t in available_tools if t["name"] in research_tool_names]
    messages = [{"role": "user", "content": task.user_text}]
    result = _run_agent_loop(RESEARCH_SYSTEM, messages, tools, MODEL_SPECIALIZED, 6, tool_handler)
    result.agent = "research"
    return result


# ── Personal Agent ─────────────────────────────────────────────────────────────

PERSONAL_SYSTEM = """Sos el Personal Agent de Cukinator. Gestionás la memoria y el contexto personal del usuario.

Tenés acceso a:
- memory_buscar: buscar en historial y memoria
- memory_guardar_hecho: guardar hechos importantes
- memory_persona: info sobre personas específicas
- memory_stats: estadísticas de memoria
- config_leer / config_listar: preferencias guardadas

Devolvé información relevante del historial o confirmá lo que guardaste."""


def run_personal_agent(task: AgentTask, available_tools: list, tool_handler) -> AgentResult:
    personal_tool_names = {"memory_buscar", "memory_guardar_hecho", "memory_persona",
                           "memory_stats", "config_leer", "config_listar"}
    tools = [t for t in available_tools if t["name"] in personal_tool_names]
    messages = [{"role": "user", "content": task.user_text}]
    result = _run_agent_loop(PERSONAL_SYSTEM, messages, tools, MODEL_SPECIALIZED, 4, tool_handler)
    result.agent = "personal"
    return result


# ── Astrology Agent ────────────────────────────────────────────────────────────

ASTROLOGY_SYSTEM = """Sos el Astrology Agent de Cukinator. Calculás cartas natales, tránsitos y análisis astrológicos.

Tenés acceso a:
- calcular_carta_natal: calcular y mostrar carta natal
- astro_guardar_perfil: guardar perfil astrológico
- astro_ver_perfil: ver carta guardada
- astro_listar_perfiles: listar todos los perfiles
- astro_eliminar_perfil: eliminar un perfil

Usá estos tools para procesar el pedido. Devolvé la información astrológica completa."""


def run_astrology_agent(task: AgentTask, available_tools: list, tool_handler) -> AgentResult:
    astro_tool_names = {"calcular_carta_natal", "astro_guardar_perfil", "astro_ver_perfil",
                        "astro_listar_perfiles", "astro_eliminar_perfil"}
    tools = [t for t in available_tools if t["name"] in astro_tool_names]
    messages = [{"role": "user", "content": task.user_text}]
    result = _run_agent_loop(ASTROLOGY_SYSTEM, messages, tools, MODEL_SPECIALIZED, 6, tool_handler)
    result.agent = "astrology"
    return result


# ── Reinsurance Agent ──────────────────────────────────────────────────────────

REINSURANCE_SYSTEM = """Sos el Reinsurance Agent de Cukinator. Sos especialista en reaseguros e insurance operations.

Tenés acceso a:
- ri_consultar: buscar en knowledge base de reaseguros
- ri_listar_documentos: ver documentos disponibles
- ri_ingestar: indexar nuevo documento
- ri_stats: estadísticas de la KB
- search_web: búsqueda web para info actualizada

Respondé con precisión técnica: definición → implicancia operativa → ejemplo real.
Si aplica normativa argentina: agregá impacto regulatorio."""


def run_reinsurance_agent(task: AgentTask, available_tools: list, tool_handler) -> AgentResult:
    ri_tool_names = {"ri_consultar", "ri_listar_documentos", "ri_ingestar",
                     "ri_stats", "search_web"}
    tools = [t for t in available_tools if t["name"] in ri_tool_names]
    messages = [{"role": "user", "content": task.user_text}]
    result = _run_agent_loop(REINSURANCE_SYSTEM, messages, tools, MODEL_SPECIALIZED, 6, tool_handler)
    result.agent = "reinsurance"
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA 5 — ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

ORCHESTRATOR_SYSTEM = """Sos el Orchestrator de Cukinator — el cerebro del sistema.

Tu trabajo:
1. Entender el pedido del usuario
2. Sintetizar resultados de agentes especializados en una respuesta clara
3. Ser la ÚNICA voz hacia el usuario

RESTRICCIONES ABSOLUTAS:
- NO modificás código
- NO usás GitHub directamente
- NO ejecutás comandos
- NO tocás infraestructura
- Solo conversás, decidís y consolidás

FORMATO DE RESPUESTA (para respuestas técnicas/operativas):

**Título claro**
Resumen de una línea.

**Estado**
- qué ocurrió
- qué cambió

**Resultado**
Detalle de lo ejecutado.

**Git** (si aplica)
- branch: `bot-changes`
- PR: link

**Siguiente paso**
Acción sugerida o pregunta concreta.

Para errores:
**Error detectado**
- qué falló

**Cómo resolverlo**
1. opción concreta

Para conversación simple: respondé directo, sin estructura, máximo 3-4 líneas."""


def run_orchestrator_consolidation(user_text: str, agent_results: list[AgentResult],
                                   intent: str, chat_id: int) -> str:
    """
    El Orchestrator consolida los resultados de los agentes y genera
    la respuesta final para el usuario.
    """
    # Para respuestas simples conversacionales sin delegación
    if not agent_results:
        return ""

    # Construir contexto con resultados de agentes
    context_parts = [f"Pedido del usuario: {user_text}\n\nResultados de agentes:"]
    for r in agent_results:
        context_parts.append(f"\n[{r.agent.upper()} — status: {r.status}]\n{r.summary}")
        if r.errors:
            context_parts.append(f"Errores: {', '.join(r.errors)}")

    context = "\n".join(context_parts)

    prompt = f"{context}\n\nGenerá la respuesta final para el usuario. Consolidá la información de los agentes en un mensaje claro y escaneable."

    try:
        resp = get_client().messages.create(
            model=MODEL_ORCHESTRATOR,
            max_tokens=2048,
            system=ORCHESTRATOR_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        return next((b.text for b in resp.content if hasattr(b, "text") and b.text.strip()), "")
    except Exception as e:
        log.error(f"Orchestrator consolidation error: {e}")
        # Fallback: devolver el summary del primer agente
        return agent_results[0].summary if agent_results else ""


# ═══════════════════════════════════════════════════════════════════════════════
# CAPA 6 — PIPELINE PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

def route_and_execute(task: AgentTask, available_tools: list,
                      tool_handler) -> tuple[str, list, str]:
    """
    Pipeline principal del sistema multi-agente.

    1. Clasifica la intención
    2. Delega al agente correcto
    3. Consolida con el Orchestrator
    4. Devuelve (respuesta, extra_files, pdf_path)

    Para tareas conversacionales simples devuelve ("", [], None)
    y el caller usa ask_claude normal.
    """
    intent = task.intent or classify_intent(task.user_text)
    log.info(f"[{task.chat_id}] Pipeline: intent={intent} task_id={task.task_id}")

    # Conversational → el caller maneja con ask_claude normal
    if intent == "conversational":
        return "", [], None

    # Delegar a agentes
    agent_results = []

    if intent == "coding_task":
        result = run_operational_agent(task, available_tools, tool_handler)
        agent_results.append(result)

    elif intent == "research_task":
        result = run_research_agent(task, available_tools, tool_handler)
        agent_results.append(result)

    elif intent == "personal_task":
        result = run_personal_agent(task, available_tools, tool_handler)
        agent_results.append(result)

    elif intent == "astrology_task":
        result = run_astrology_agent(task, available_tools, tool_handler)
        agent_results.append(result)

    elif intent == "reinsurance_task":
        result = run_reinsurance_agent(task, available_tools, tool_handler)
        agent_results.append(result)

    elif intent == "mixed_task":
        # Dividir en sub-tareas y ejecutar múltiples agentes
        # Por ahora: el agente que matchea mejor el texto
        sub_intent = _keyword_fallback(task.user_text)
        sub_task = AgentTask(
            intent=sub_intent,
            user_text=task.user_text,
            chat_id=task.chat_id,
            user_name=task.user_name
        )
        result = route_and_execute(sub_task, available_tools, tool_handler)
        return result  # Ya viene procesado

    else:
        return "", [], None

    # Sin resultados
    if not agent_results:
        return "", [], None

    # Consolidar extra_files y pdf de todos los agentes
    all_extra_files = []
    pdf_path = None
    for r in agent_results:
        all_extra_files.extend(r.extra_files or [])
        if r.pdf_path:
            pdf_path = r.pdf_path

    # Orchestrator consolida la respuesta
    final_response = run_orchestrator_consolidation(
        task.user_text, agent_results, intent, task.chat_id
    )

    if not final_response:
        final_response = agent_results[0].summary

    return final_response, all_extra_files, pdf_path


def _keyword_fallback(text: str) -> str:
    """Fallback para clasificación sin API."""
    text_lower = text.lower()
    if any(k in text_lower for k in ["carta natal", "tránsito", "astrología", "signo"]):
        return "astrology_task"
    if any(k in text_lower for k in ["reaseguro", "treaty", "quota share"]):
        return "reinsurance_task"
    if any(k in text_lower for k in ["memoria", "historial", "recordás"]):
        return "personal_task"
    if any(k in text_lower for k in ["busca", "investigá", "analizá", "síntesis"]):
        return "research_task"
    if any(k in text_lower for k in ["modificá el bot", "código", "github", "commit"]):
        return "coding_task"
    return "conversational"
