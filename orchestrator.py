"""
orchestrator.py — Cerebro conversacional del sistema Cukinator.

Arquitectura:
  Telegram → Orchestrator → [agente especializado] → Orchestrator → Telegram

El Orchestrator:
  - clasifica intenciones
  - decide si responde directo o delega
  - es la ÚNICA voz hacia el usuario
  - NUNCA toca código, GitHub, VPS directamente

Los agentes especializados:
  - procesan y devuelven resultados estructurados
  - nunca hablan directo al usuario
"""

import os
import logging
import json
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

log = logging.getLogger(__name__)


# ── Tipos de intención ─────────────────────────────────────────────────────────

class Intent(str, Enum):
    CONVERSATIONAL   = "conversational"
    OPERATIONAL      = "operational"    # código, GitHub, VPS, deploy
    RESEARCH         = "research"       # búsqueda, análisis, síntesis
    PERSONAL         = "personal"       # memoria, historial, preferencias
    ASTROLOGY        = "astrology"      # cartas, tránsitos
    REINSURANCE      = "reinsurance"    # seguros/reaseguros
    MIXED            = "mixed"


# ── Contrato de tarea delegada ─────────────────────────────────────────────────

@dataclass
class AgentTask:
    target_agent: str
    objective: str
    task: str
    context: dict = field(default_factory=dict)
    constraints: list = field(default_factory=list)
    expected_output: list = field(default_factory=list)


@dataclass
class AgentResult:
    status: str          # ok | error | partial
    summary: str
    data: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    agent: str = ""


# ── Clasificador de intención ──────────────────────────────────────────────────

INTENT_KEYWORDS = {
    Intent.OPERATIONAL: [
        # código
        "código", "implementá", "implementa", "creá el módulo", "crea el módulo",
        "escribí", "escribe", "editá", "edita", "modificá", "modifica",
        # repo
        "github", "push", "commit", "branch", "rama", "pull request", "pr", "merge",
        "archivo", "bot.py", "bot_core", "handler", "módulo", "script",
        # infra
        "vps", "railway", "deploy", "reiniciá", "reinicia", "restart", "logs",
        "docker", "container", "servicio", "servidor", "hostinger",
        "ssh", "uptime", "disco", "memoria ram",
        # config técnica
        "litellm", "openwebui", "open webui", "ollama", "config.yaml",
    ],
    Intent.RESEARCH: [
        "buscá", "busca", "investigá", "investiga", "analizá", "analiza",
        "comparación", "comparar", "resumen de", "síntesis", "resumí",
        "qué dice", "qué es", "cómo funciona", "explicame",
        "artículo", "paper", "documento", "fuente", "bibliografía",
    ],
    Intent.PERSONAL: [
        "recordás", "recordas", "te acordás", "te acordas", "memoria",
        "historial", "la última vez", "antes me dijiste", "preferencia",
        "mi email", "mi nombre", "mis datos", "seguimiento",
    ],
    Intent.ASTROLOGY: [
        "carta natal", "carta astral", "tránsito", "transito", "ascendente",
        "signo", "planeta", "casa", "aspecto", "horóscopo", "horoscopo",
        "astrología", "astrologia", "sol en", "luna en", "ephemeris",
    ],
    Intent.REINSURANCE: [
        "reaseguro", "reinsurance", "treaty", "facultativo", "retrocesión",
        "underwriting", "cedente", "quota share", "excess of loss",
        "burning cost", "loss ratio", "ibnr", "wording", "cláusula",
        "normativa aseguradora", "ley 17418", "ssn", "lloyd's",
    ],
}

def classify_intent(text: str) -> tuple[Intent, list[Intent]]:
    """
    Clasifica el texto en una o más intenciones.
    Devuelve (intent_primario, [todos_los_intents_detectados])
    """
    text_lower = text.lower()
    detected = []

    for intent, keywords in INTENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            detected.append((intent, score))

    if not detected:
        return Intent.CONVERSATIONAL, [Intent.CONVERSATIONAL]

    detected.sort(key=lambda x: x[1], reverse=True)
    intents = [i for i, _ in detected]

    if len(intents) > 1:
        return Intent.MIXED, intents
    return intents[0], intents


# ── Repo Lock ──────────────────────────────────────────────────────────────────

import threading
import time

class RepoLock:
    """
    Lock para operaciones sobre el repositorio.
    Previene ejecuciones concurrentes sobre el mismo repo/branch.
    """
    def __init__(self):
        self._locks: dict[str, dict] = {}  # repo -> {locked_by, locked_at, task}
        self._mutex = threading.Lock()

    def acquire(self, repo: str, task_id: str, task_desc: str) -> bool:
        with self._mutex:
            if repo in self._locks:
                lock_info = self._locks[repo]
                # Auto-release si lleva más de 10 minutos
                if time.time() - lock_info["locked_at"] > 600:
                    log.warning(f"RepoLock: auto-releasing stale lock on {repo}")
                    del self._locks[repo]
                else:
                    return False
            self._locks[repo] = {
                "locked_by": task_id,
                "locked_at": time.time(),
                "task": task_desc,
            }
            return True

    def release(self, repo: str, task_id: str):
        with self._mutex:
            if repo in self._locks and self._locks[repo]["locked_by"] == task_id:
                del self._locks[repo]

    def status(self, repo: str) -> Optional[dict]:
        with self._mutex:
            return self._locks.get(repo)

    def is_locked(self, repo: str) -> bool:
        with self._mutex:
            if repo not in self._locks:
                return False
            if time.time() - self._locks[repo]["locked_at"] > 600:
                del self._locks[repo]
                return False
            return True


# Instancia global
repo_lock = RepoLock()


# ── Orchestrator ───────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Cerebro conversacional del sistema.

    Responsabilidades:
    - Clasificar intención del usuario
    - Decidir si responder directo o delegar
    - Invocar agentes especializados
    - Consolidar y formatear respuesta final
    - Ser la ÚNICA voz hacia el usuario
    """

    def __init__(self, claude_client, db_path: str):
        self.claude = claude_client
        self.db_path = db_path
        self.DEFAULT_REPO = "cuki82/cukinator-bot"

    def route(self, text: str, chat_id: int) -> tuple[Intent, list[Intent]]:
        """Clasifica y devuelve la ruta de procesamiento."""
        return classify_intent(text)

    def build_delegation_task(self, intent: Intent, text: str, context: dict) -> AgentTask:
        """Construye el contrato de tarea para el agente especializado."""
        agent_map = {
            Intent.OPERATIONAL:  "operational",
            Intent.RESEARCH:     "research",
            Intent.PERSONAL:     "personal",
            Intent.ASTROLOGY:    "astrology",
            Intent.REINSURANCE:  "reinsurance",
        }
        return AgentTask(
            target_agent=agent_map.get(intent, "conversational"),
            objective=f"Procesar: {text[:80]}",
            task=text,
            context=context,
            constraints=["no_direct_user_response", "return_structured_result"],
            expected_output=["status", "summary", "data"],
        )

    def check_repo_available(self, repo: str = None) -> tuple[bool, str]:
        """Verifica si el repo está disponible para operaciones."""
        target = repo or self.DEFAULT_REPO
        if repo_lock.is_locked(target):
            info = repo_lock.status(target)
            elapsed = int(time.time() - info["locked_at"])
            return False, f"Repo `{target}` bloqueado por tarea en curso ({elapsed}s): {info['task']}"
        return True, "ok"

    def format_response(self, intent: Intent, result: AgentResult, original_query: str) -> str:
        """
        Formatea la respuesta final hacia el usuario.
        Siempre pasa por acá — nunca los agentes responden directo.
        """
        if result.status == "error":
            lines = [f"**Error en {result.agent or 'sistema'}**\n"]
            for e in result.errors:
                lines.append(f"- {e}")
            if result.summary:
                lines.append(f"\n{result.summary}")
            return "\n".join(lines)

        return result.summary


# ── Specialized Agent Stubs ────────────────────────────────────────────────────
# Contratos definidos. Implementación completa en Fase 2/3.

class OperationalAgent:
    """
    ÚNICO agente autorizado para tocar código, GitHub, VPS, Railway.

    Reglas:
    - Nunca push directo a main
    - Siempre branch nueva (bot-changes o feature/*)
    - Siempre PR antes de merge
    - Verifica repo lock antes de ejecutar
    - Devuelve resultado estructurado al Orchestrator

    Fase actual: stub con routing a ask_claude() con restricciones.
    Fase 2: Claude Code SDK / subprocess executor real.
    """
    PROTECTED_REPOS = ["cuki82/cukinator-bot"]
    PROTECTED_FILES = [
        "bot.py", "bot_core.py", "orchestrator.py",
        "handlers/message_handler.py", "handlers/callback_handler.py",
        "Dockerfile", "requirements.txt",
    ]

    def can_modify_file(self, path: str) -> tuple[bool, str]:
        if path in self.PROTECTED_FILES:
            return False, f"`{path}` es un archivo core protegido. Cambios solo via sesión de desarrollo."
        return True, "ok"

    def execute(self, task: AgentTask) -> AgentResult:
        """Ejecuta tarea operativa. En Fase 2: Claude Code SDK."""
        # Por ahora: delegar a ask_claude con contexto operacional
        # La implementación completa usa subprocess + git + validación
        return AgentResult(
            status="partial",
            summary="Tarea operativa recibida. En Fase 2 se ejecuta via Operational Agent con Claude Code.",
            agent="operational",
            data={"task": task.task, "constraints": task.constraints},
        )


class ResearchAgent:
    """Búsqueda, síntesis, análisis documental. Fase 2."""
    def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(status="partial", summary="Research Agent — Fase 2", agent="research")


class PersonalAgent:
    """Memoria, historial, preferencias. Usa memory_store existente."""
    def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(status="partial", summary="Personal Agent — usa memory_store existente", agent="personal")


class AstrologyAgent:
    """Cartas natales, tránsitos. Usa swiss_engine existente."""
    def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(status="partial", summary="Astrology Agent — usa swiss_engine existente", agent="astrology")


class ReinsuranceAgent:
    """Knowledge base de reaseguros. Usa reinsurance_kb existente."""
    def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(status="partial", summary="Reinsurance Agent — usa reinsurance_kb existente", agent="reinsurance")


# ── MCP Layer (Fase 2) ─────────────────────────────────────────────────────────
# Abstracción estándar para acceso a sistemas y datos.
# Implementación completa en Fase 2.

class MCPLayer:
    """
    Model Context Protocol — capa estándar de tools y recursos.

    Namespaces planificados:
    - ops-local:     logs, status servicios, restart, health, comandos permitidos
    - github-control: status repo, ramas, commits, lectura archivos, PRs
    - memory-db:     memoria, historial, estado jobs, preferencias
    - knowledge-hub: documentos, cláusulas, normativa, knowledge base

    Fase actual: interfaz definida, implementación en Fase 2.
    """

    NAMESPACES = {
        "ops-local": ["logs", "status", "restart", "health", "disk", "memory"],
        "github-control": ["status", "branches", "commits", "files", "prs"],
        "memory-db": ["memory", "history", "jobs", "preferences"],
        "knowledge-hub": ["documents", "clauses", "regulations", "kb"],
    }

    def get_tool(self, namespace: str, tool: str):
        """Retorna el tool apropiado del namespace."""
        if namespace not in self.NAMESPACES:
            raise ValueError(f"Namespace desconocido: {namespace}")
        if tool not in self.NAMESPACES[namespace]:
            raise ValueError(f"Tool {tool} no disponible en {namespace}")
        # Fase 2: routing real a implementación
        return None

    def list_tools(self) -> dict:
        return self.NAMESPACES


# ── Instancias globales ────────────────────────────────────────────────────────

operational_agent  = OperationalAgent()
research_agent     = ResearchAgent()
personal_agent     = PersonalAgent()
astrology_agent    = AstrologyAgent()
reinsurance_agent  = ReinsuranceAgent()
mcp               = MCPLayer()
