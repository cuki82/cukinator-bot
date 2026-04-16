"""
base_agent.py — Clase base para todos los agentes especializados.

Cada agente:
- Tiene su propio system prompt
- Tiene su propio set de tools (subset del master TOOLS)
- Hace su propia llamada a Claude API
- Devuelve AgentResult estructurado al Orchestrator
- NUNCA habla directo al usuario
"""

import logging
import os
import anthropic
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "")


@dataclass
class AgentTask:
    """Contrato de tarea que recibe un agente del Orchestrator."""
    intent: str
    user_text: str
    chat_id: int
    context: dict = field(default_factory=dict)
    constraints: list = field(default_factory=list)


@dataclass
class AgentResult:
    """Resultado estructurado que devuelve un agente al Orchestrator."""
    status: str          # ok | error | partial | no_action
    summary: str         # respuesta final para el usuario (la formatea el Orchestrator)
    agent: str = ""
    data: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    extra_files: list = field(default_factory=list)  # (nombre, bytes, caption)
    pdf_path: str = None


class BaseAgent:
    """
    Clase base para agentes especializados.
    
    Cada agente hijo define:
    - AGENT_NAME: nombre del agente
    - SYSTEM_PROMPT: instrucciones especializadas
    - TOOLS: lista de tools disponibles para este agente
    - execute(task) -> AgentResult
    """
    AGENT_NAME = "base"
    SYSTEM_PROMPT = "Sos un agente especializado."
    TOOLS = []
    MAX_ITERATIONS = 6
    MODEL = "claude-opus-4-5"
    MAX_TOKENS = 2048

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    def _run(self, messages: list, extra_context: str = "") -> AgentResult:
        """
        Ejecuta el loop de Claude con los tools del agente.
        Devuelve AgentResult — nunca habla al usuario directamente.
        """
        system = self.SYSTEM_PROMPT
        if extra_context:
            system += f"\n\nCONTEXTO ADICIONAL:\n{extra_context}"

        iteration = 0
        extra_files = []
        pdf_path = None

        while iteration < self.MAX_ITERATIONS:
            iteration += 1
            try:
                response = self.client.messages.create(
                    model=self.MODEL,
                    max_tokens=self.MAX_TOKENS,
                    system=system,
                    tools=self.TOOLS,
                    messages=messages
                )
            except Exception as e:
                log.error(f"[{self.AGENT_NAME}] Claude API error: {e}")
                return AgentResult(
                    status="error",
                    summary=f"Error en {self.AGENT_NAME}: {e}",
                    agent=self.AGENT_NAME,
                    errors=[str(e)]
                )

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []

                for block in response.content:
                    if block.type == "tool_use":
                        result, ef, pp = self._handle_tool(block)
                        if ef:
                            extra_files.extend(ef)
                        if pp:
                            pdf_path = pp
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result)
                        })

                messages.append({"role": "user", "content": tool_results})

            else:
                # Respuesta final
                text_parts = []
                for block in response.content:
                    if hasattr(block, "text") and block.text.strip():
                        text_parts.append(block.text.strip())

                summary = "\n".join(text_parts) if text_parts else ""

                if not summary:
                    # Forzar resumen
                    messages.append({"role": "user", "content": "Resumí en 1-2 líneas qué hiciste."})
                    try:
                        force = self.client.messages.create(
                            model=self.MODEL, max_tokens=256,
                            system=system, messages=messages
                        )
                        for b in force.content:
                            if hasattr(b, "text") and b.text.strip():
                                summary = b.text.strip()
                                break
                    except Exception:
                        pass
                    summary = summary or "Tarea completada."

                return AgentResult(
                    status="ok",
                    summary=summary,
                    agent=self.AGENT_NAME,
                    extra_files=extra_files,
                    pdf_path=pdf_path
                )

        return AgentResult(
            status="partial",
            summary="Alcancé el límite de operaciones. Intentá con una instrucción más específica.",
            agent=self.AGENT_NAME
        )

    def _handle_tool(self, block) -> tuple:
        """
        Override en cada agente para manejar sus tools específicos.
        Devuelve (result_str, extra_files, pdf_path)
        """
        return f"Tool {block.name} no implementado en {self.AGENT_NAME}", [], None

    def execute(self, task: AgentTask) -> AgentResult:
        """Override en cada agente. Por defecto usa _run con el texto del usuario."""
        messages = [{"role": "user", "content": task.user_text}]
        return self._run(messages)
