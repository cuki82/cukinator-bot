"""
orchestrator_v2.py — Orchestrator real del sistema Cukinator.

Arquitectura optimizada para velocidad:
- Orchestrator usa claude-haiku (rápido, barato) para decidir
- Agentes usan el modelo apropiado según tarea
- Ejecución paralela para mixed_tasks
- System prompt cacheado por Anthropic (>1024 tokens)
- Sin capas intermedias innecesarias

Flujo:
  Telegram → Orchestrator → decide → agente → Orchestrator → usuario
"""

import os
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
import anthropic

log = logging.getLogger(__name__)

# ── Modelos ────────────────────────────────────────────────────────────────────
# Haiku para decisiones rápidas, Opus para ejecución compleja
MODEL_ORCHESTRATOR = "claude-haiku-4-5"   # ~200ms decision
MODEL_OPUS         = "claude-opus-4-5"    # tareas complejas
MODEL_HAIKU        = "claude-haiku-4-5"   # tareas simples/búsqueda

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")
_client = None

def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _client


# ── Contratos ──────────────────────────────────────────────────────────────────

@dataclass
class OrchestratorDecision:
    """Lo que el Orchestrator decide hacer."""
    intent: str          # conversational | coding | research | personal | astrology | reinsurance | mixed
    response: str        # respuesta directa si es conversacional
    delegate_to: list    # lista de agentes a invocar
    task_description: str # descripción clara de la tarea para el agente
    reasoning: str       # por qué tomó esta decisión (interno)


@dataclass
class AgentResult:
    """Resultado de un agente especializado."""
    agent: str
    status: str          # ok | error | partial
    content: str         # respuesta del agente
    extra_files: list = field(default_factory=list)
    pdf_path: str = None
    duration_ms: int = 0


# ── System Prompts (cacheados por Anthropic) ───────────────────────────────────

ORCHESTRATOR_SYSTEM = """Clasificá el mensaje del usuario y respondé en JSON válido.

FORMATO EXACTO (solo JSON, sin markdown, sin texto extra):
{"intent":"CATEGORIA","direct_response":"RESPUESTA_SI_ES_CONVERSACIONAL","delegate_to":[],"task":"","reasoning":""}

CATEGORÍAS:
- conversational: charla, preguntas, clima, hora, emails, calendario, búsqueda, videos, reservas, estado VPS
- coding: modificar código del bot, GitHub, deploy, Railway
- research: investigar, analizar documentos
- personal: memoria, historial, preferencias
- astrology: cartas natales, tránsitos, astrología
- reinsurance: reaseguros, seguros, normativa
- mixed: combina dos o más categorías

REGLA: Si hay dudas → conversational.

Para conversational: completá "direct_response" con una respuesta breve y natural en español rioplatense.
Para otros: dejá "direct_response" vacío y completá "task" con la descripción de la tarea.
Para delegate_to: usá ["operational"], ["research"], ["personal"], ["astrology"] o ["reinsurance"].

Ejemplos:
{"intent":"conversational","direct_response":"Hola! Todo bien por acá. ¿Qué necesitás?","delegate_to":[],"task":"","reasoning":"saludo"}
{"intent":"astrology","direct_response":"","delegate_to":["astrology"],"task":"Calcular carta natal de Juan, 15/03/1985, 08:00, Buenos Aires","reasoning":"pedido carta natal"}
{"intent":"coding","direct_response":"","delegate_to":["operational"],"task":"Modificar el handler de voz para que responda más rápido","reasoning":"cambio de código"}"""

OPERATIONAL_AGENT_SYSTEM = """Sos el Operational Agent. Único autorizado para operaciones sobre código e infraestructura.

REGLAS:
1. Nunca pushees a main — siempre bot-changes
2. Siempre leé antes de modificar
3. Archivos protegidos (bot.py, bot_core.py, orchestrator_v2.py, Dockerfile): solo describís el cambio, no lo ejecutás
4. Después de cualquier push → creá PR
5. Reportá exactamente qué hiciste

HERRAMIENTAS DISPONIBLES: github_push, github_pr, vps_exec, vps_docker, vps_leer_archivo, vps_escribir_archivo, agent_log, config_guardar"""

RESEARCH_AGENT_SYSTEM = """Sos el Research Agent. Especialista en buscar, analizar y sintetizar información.

HERRAMIENTAS: search_web, ri_consultar, ri_listar_documentos
Buscá primero en la KB interna, luego en la web si no encontrás.
Devolvé información estructurada, verificada y con fuentes cuando sea relevante."""

PERSONAL_AGENT_SYSTEM = """Sos el Personal Agent. Gestionás la memoria y contexto del usuario.

HERRAMIENTAS: memory_buscar, memory_guardar_hecho, memory_persona, memory_stats, config_leer, config_listar
Respondé con información relevante del historial o confirmá lo guardado."""

ASTROLOGY_AGENT_SYSTEM = """Sos el Astrology Agent. Especialista en astrología y cartas natales.

HERRAMIENTAS: calcular_carta_natal, astro_guardar_perfil, astro_ver_perfil, astro_listar_perfiles, astro_eliminar_perfil
Calculá y presentá la información astrológica con precisión técnica."""

REINSURANCE_AGENT_SYSTEM = """Sos el Reinsurance Agent. Especialista en reaseguros e insurance operations.

HERRAMIENTAS: ri_consultar, ri_listar_documentos, ri_stats, ri_ingestar, search_web
Respondé con precisión técnica: definición → implicancia operativa → ejemplo real.
Para normativa argentina: agregá impacto regulatorio."""


# ── Orchestrator Call ──────────────────────────────────────────────────────────

def orchestrate(user_text: str, history: list, chat_id: int,
                user_name: str = "") -> OrchestratorDecision:
    """
    Llama al Orchestrator (Haiku) para decidir qué hacer.
    Retorna la decisión en <300ms típicamente.
    """
    import json

    # Construir contexto mínimo
    recent = history[-6:] if len(history) > 6 else history
    messages = recent + [{"role": "user", "content": user_text}]

    try:
        t0 = time.time()
        resp = get_client().messages.create(
            model=MODEL_ORCHESTRATOR,
            max_tokens=512,
            system=ORCHESTRATOR_SYSTEM,
            messages=messages
        )
        elapsed = int((time.time() - t0) * 1000)
        log.info(f"[{chat_id}] Orchestrator decision: {elapsed}ms")

        text = resp.content[0].text.strip()

        # Parsear JSON de la decisión
        # Extraer JSON del texto (puede venir con markdown)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        elif "{" in text:
            text = text[text.index("{"):text.rindex("}")+1]

        data = json.loads(text)

        return OrchestratorDecision(
            intent=data.get("intent", "conversational"),
            response=data.get("direct_response", ""),
            delegate_to=data.get("delegate_to", []),
            task_description=data.get("task", user_text),
            reasoning=data.get("reasoning", "")
        )

    except Exception as e:
        log.error(f"Orchestrator error: {e} — fallback a conversational")
        # Fallback seguro
        return OrchestratorDecision(
            intent="conversational",
            response="",
            delegate_to=[],
            task_description=user_text,
            reasoning=f"fallback: {e}"
        )


# ── Agent Runner ───────────────────────────────────────────────────────────────

def run_agent(agent_name: str, task: str, history: list,
              available_tools: list, tool_handler,
              chat_id: int) -> AgentResult:
    """
    Ejecuta un agente especializado.
    Cada agente tiene su system prompt y subset de tools.
    """
    t0 = time.time()

    AGENT_CONFIG = {
        "operational": {
            "system": OPERATIONAL_AGENT_SYSTEM,
            "model": MODEL_OPUS,
            "tools": {"github_push", "github_pr", "vps_exec", "vps_docker",
                      "vps_leer_archivo", "vps_escribir_archivo",
                      "agent_log", "config_guardar", "config_leer"},
            "max_iter": 10,
        },
        "research": {
            "system": RESEARCH_AGENT_SYSTEM,
            "model": MODEL_HAIKU,
            "tools": {"search_web", "ri_consultar", "ri_listar_documentos", "ri_stats"},
            "max_iter": 5,
        },
        "personal": {
            "system": PERSONAL_AGENT_SYSTEM,
            "model": MODEL_HAIKU,
            "tools": {"memory_buscar", "memory_guardar_hecho", "memory_persona",
                      "memory_stats", "config_leer", "config_listar"},
            "max_iter": 4,
        },
        "astrology": {
            "system": ASTROLOGY_AGENT_SYSTEM,
            "model": MODEL_OPUS,
            "tools": {"calcular_carta_natal", "astro_guardar_perfil", "astro_ver_perfil",
                      "astro_listar_perfiles", "astro_eliminar_perfil"},
            "max_iter": 6,
        },
        "reinsurance": {
            "system": REINSURANCE_AGENT_SYSTEM,
            "model": MODEL_OPUS,
            "tools": {"ri_consultar", "ri_listar_documentos", "ri_stats",
                      "ri_ingestar", "search_web"},
            "max_iter": 6,
        },
    }

    config = AGENT_CONFIG.get(agent_name)
    if not config:
        return AgentResult(agent=agent_name, status="error",
                           content=f"Agente desconocido: {agent_name}")

    # Filtrar tools del agente
    agent_tools = [t for t in available_tools if t["name"] in config["tools"]]

    # Mensajes con historial reciente
    recent_history = history[-4:] if len(history) > 4 else history
    messages = recent_history + [{"role": "user", "content": task}]

    extra_files = []
    pdf_path = None
    client = get_client()

    for i in range(config["max_iter"]):
        try:
            resp = client.messages.create(
                model=config["model"],
                max_tokens=3000,
                system=config["system"],
                tools=agent_tools if agent_tools else [],
                messages=messages
            )
        except Exception as e:
            return AgentResult(agent=agent_name, status="error", content=str(e))

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
            content = "\n".join(parts) or "Completado."
            elapsed = int((time.time() - t0) * 1000)
            log.info(f"[{chat_id}] Agent {agent_name} done: {elapsed}ms")
            return AgentResult(
                agent=agent_name, status="ok", content=content,
                extra_files=extra_files, pdf_path=pdf_path, duration_ms=elapsed
            )

    return AgentResult(agent=agent_name, status="partial",
                       content="Límite de iteraciones.", duration_ms=int((time.time()-t0)*1000))


# ── Consolidador ───────────────────────────────────────────────────────────────

def consolidate(user_text: str, results: list[AgentResult],
                history: list, chat_id: int) -> str:
    """
    Si hay múltiples resultados de agentes, el Orchestrator los consolida.
    Para un solo resultado, devuelve directo (sin llamada extra).
    """
    if len(results) == 1:
        return results[0].content

    # Múltiples agentes → Haiku consolida rápido
    context = f"Pedido: {user_text}\n\nResultados:\n"
    for r in results:
        context += f"\n[{r.agent.upper()}]\n{r.content}\n"

    try:
        resp = get_client().messages.create(
            model=MODEL_HAIKU,
            max_tokens=1024,
            system="Consolidá los resultados de múltiples agentes en una respuesta única, clara y sin redundancias para el usuario. Tono relajado, directo.",
            messages=[{"role": "user", "content": context}]
        )
        return resp.content[0].text.strip()
    except Exception as e:
        # Fallback: concatenar
        return "\n\n".join(r.content for r in results)


# ── Pipeline principal ─────────────────────────────────────────────────────────

def run_pipeline(user_text: str, history: list, chat_id: int,
                 user_name: str, available_tools: list,
                 tool_handler) -> tuple[str, list, str]:
    """
    Pipeline completo:
    1. Orchestrator decide (Haiku ~200ms)
    2. Si conversacional → responde directo con Claude (Opus)
    3. Si delegado → agentes ejecutan (en paralelo si son varios)
    4. Consolidación si hay múltiples resultados

    Returns: (response_text, extra_files, pdf_path)
    """
    t_start = time.time()

    # Paso 1: Orchestrator decide
    decision = orchestrate(user_text, history, chat_id, user_name)
    log.info(f"[{chat_id}] Intent: {decision.intent} | Delegate: {decision.delegate_to}")

    # Paso 2: Conversacional → responde directo
    if decision.intent == "conversational" or not decision.delegate_to:
        # Si el Orchestrator tiene respuesta directa válida, usarla
        if decision.response and len(decision.response) > 5:
            elapsed = int((time.time() - t_start) * 1000)
            log.info(f"[{chat_id}] Direct response: {elapsed}ms")
            return decision.response, [], None

        # Si no tiene respuesta → caer al flujo directo de bot_core.py
        return "", [], None

    # Paso 3: Delegar a agentes
    all_extra_files = []
    pdf_path = None
    results = []

    if len(decision.delegate_to) == 1:
        # Un solo agente — ejecutar directo
        agent_name = decision.delegate_to[0]
        result = run_agent(
            agent_name, decision.task_description,
            history, available_tools, tool_handler, chat_id
        )
        results.append(result)
        all_extra_files.extend(result.extra_files)
        if result.pdf_path:
            pdf_path = result.pdf_path

    else:
        # Múltiples agentes — ejecutar en paralelo via threads
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(
                    run_agent, agent_name, decision.task_description,
                    history, available_tools, tool_handler, chat_id
                ): agent_name
                for agent_name in decision.delegate_to
            }
            for future in concurrent.futures.as_completed(futures, timeout=120):
                try:
                    result = future.result()
                    results.append(result)
                    all_extra_files.extend(result.extra_files)
                    if result.pdf_path:
                        pdf_path = result.pdf_path
                except Exception as e:
                    log.error(f"Agent {futures[future]} failed: {e}")

    if not results:
        return "", [], None

    # Paso 4: Consolidar
    final_response = consolidate(user_text, results, history, chat_id)

    elapsed = int((time.time() - t_start) * 1000)
    log.info(f"[{chat_id}] Pipeline total: {elapsed}ms")

    return final_response, all_extra_files, pdf_path
