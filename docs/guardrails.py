"""
Guardrails para un Agent Worker que llama LLMs en loop (tool-use).
Objetivo: limitar reintentos, iteraciones, tokens, costo y tiempo por tarea.

Uso mínimo:

    from guardrails import Guardrails

    g = Guardrails.from_env()   # lee config de las env vars

    g.begin_task(task_id="req-001", user_id=42, monthly_usage_usd=12.3)
    # → chequea si el user ya excedió el budget mensual; levanta BudgetExceeded si sí.

    for iteration in g.iter_turns():
        # iter_turns() corta por max_iterations O cuando corrés g.done()
        resp = call_llm_with_retry(g, prompt)
        g.record_usage(prompt_tokens=resp.pt, completion_tokens=resp.ct)
        if resp.is_final:
            g.done()

    summary = g.summary()
    # {"tokens": 12345, "cost_usd": 0.42, "iterations": 7, "retries": 2, "duration_s": 28}
"""

from __future__ import annotations
import os
import time
import logging
import random
from dataclasses import dataclass, field
from typing import Callable, Optional, Iterator

log = logging.getLogger("guardrails")


# ─── Excepciones ───────────────────────────────────────────────────────────

class GuardrailViolation(Exception):
    """Base: cualquier límite excedido."""

class BudgetExceeded(GuardrailViolation):
    """Monthly cap de USD o tokens excedido."""

class TaskBudgetExceeded(GuardrailViolation):
    """Cap POR-TAREA de USD, tokens o iteraciones excedido."""

class TaskTimeout(GuardrailViolation):
    """Tiempo total de la tarea superó el wall-clock timeout."""

class RetryExhausted(GuardrailViolation):
    """Se acabaron los retries contra el LLM (típicamente 429s)."""


# ─── Pricing table (USD por millón de tokens) ──────────────────────────────
# Actualizar cuando cambien tarifas. Si un modelo no está, se cobra 0 (se loguea).

MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-opus-4-7":           {"input": 15.0, "output": 75.0, "cache_create": 18.75, "cache_read": 1.5},
    "claude-sonnet-4-6":         {"input": 3.0,  "output": 15.0, "cache_create": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {"input": 1.0,  "output": 5.0,  "cache_create": 1.25,  "cache_read": 0.10},
    # OpenAI
    "gpt-5-codex":  {"input": 3.0,  "output": 15.0},
    "gpt-4o":       {"input": 2.5,  "output": 10.0},
    "gpt-4o-mini":  {"input": 0.15, "output": 0.6},
    "gpt-4.1":      {"input": 2.0,  "output": 8.0},
    # Local / Ollama → sin costo
    "ollama/llama3.1:8b": {"input": 0.0, "output": 0.0},
}

def cost_for(model: str, prompt: int, completion: int, cache_create: int = 0, cache_read: int = 0) -> float:
    p = MODEL_PRICING.get(model)
    if not p:
        log.warning(f"[guardrails] modelo sin pricing: {model}")
        return 0.0
    M = 1_000_000
    c  = (prompt / M)     * p["input"]
    c += (completion / M) * p["output"]
    c += (cache_create / M) * p.get("cache_create", 0.0)
    c += (cache_read / M)   * p.get("cache_read", 0.0)
    return c


# ─── Config ────────────────────────────────────────────────────────────────

@dataclass
class GuardrailsConfig:
    # Hard caps por tarea (antes de abortar)
    max_iterations:     int   = 30        # tool-use loops máximos
    max_task_tokens:    int   = 200_000   # prompt+completion sumados en una tarea
    max_task_cost_usd:  float = 2.0       # USD máximo en una sola tarea
    task_timeout_s:     float = 600.0     # 10 min wall-clock

    # Cap mensual (lo calcula el caller antes de begin_task y lo pasa)
    monthly_cap_usd:    Optional[float] = None   # None = sin límite mensual
    monthly_cap_tokens: Optional[int]   = None

    # Retries contra el LLM (cuando tira 429 o 5xx)
    max_retries:        int   = 3
    retry_base_delay:   float = 2.0       # segundos; exponential backoff
    retry_max_delay:    float = 30.0
    retry_jitter:       float = 0.3       # 0..1 → 30% random

    # Safety net: si el proveedor devuelve un Retry-After > esto, abortamos
    retry_after_hard_ceiling_s: float = 60.0

    # Logging
    log_every_iteration: bool = True

    @classmethod
    def from_env(cls) -> "GuardrailsConfig":
        """Lee de env vars. Todas opcionales — usa defaults si faltan."""
        def _i(k, d): return int(os.environ.get(k, d))
        def _f(k, d): return float(os.environ.get(k, d))
        def _io(k):
            v = os.environ.get(k)
            return int(v) if v and v.strip() else None
        def _fo(k):
            v = os.environ.get(k)
            return float(v) if v and v.strip() else None
        return cls(
            max_iterations     = _i("GR_MAX_ITERATIONS", 30),
            max_task_tokens    = _i("GR_MAX_TASK_TOKENS", 200_000),
            max_task_cost_usd  = _f("GR_MAX_TASK_COST_USD", 2.0),
            task_timeout_s     = _f("GR_TASK_TIMEOUT_S", 600.0),
            monthly_cap_usd    = _fo("GR_MONTHLY_CAP_USD"),
            monthly_cap_tokens = _io("GR_MONTHLY_CAP_TOKENS"),
            max_retries        = _i("GR_MAX_RETRIES", 3),
            retry_base_delay   = _f("GR_RETRY_BASE_DELAY", 2.0),
            retry_max_delay    = _f("GR_RETRY_MAX_DELAY", 30.0),
            retry_jitter       = _f("GR_RETRY_JITTER", 0.3),
            retry_after_hard_ceiling_s = _f("GR_RETRY_AFTER_CEIL", 60.0),
            log_every_iteration= os.environ.get("GR_LOG_ITER", "1") == "1",
        )


# ─── Estado por tarea ──────────────────────────────────────────────────────

@dataclass
class TaskState:
    task_id:  str
    user_id:  Optional[int]
    started:  float = field(default_factory=time.time)
    iterations: int = 0
    retries:    int = 0
    tokens_in:  int = 0
    tokens_out: int = 0
    cost_usd:   float = 0.0
    _done:      bool = False

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


# ─── El guardrails ─────────────────────────────────────────────────────────

class Guardrails:
    def __init__(self, config: GuardrailsConfig):
        self.cfg = config
        self.task: Optional[TaskState] = None
        # Usage mensual del user — lo pasa el caller, no lo calculamos nosotros
        self._monthly_usd    = 0.0
        self._monthly_tokens = 0

    @classmethod
    def from_env(cls) -> "Guardrails":
        return cls(GuardrailsConfig.from_env())

    # ── Lifecycle ─────────────────────────────────────────────────────

    def begin_task(
        self,
        task_id: str,
        user_id: Optional[int] = None,
        monthly_usage_usd: float = 0.0,
        monthly_usage_tokens: int = 0,
    ) -> TaskState:
        """Abre una nueva tarea. Chequea caps mensuales antes de empezar."""
        self._monthly_usd    = monthly_usage_usd
        self._monthly_tokens = monthly_usage_tokens

        if self.cfg.monthly_cap_usd is not None and monthly_usage_usd >= self.cfg.monthly_cap_usd:
            raise BudgetExceeded(
                f"Monthly USD cap exceeded: ${monthly_usage_usd:.2f} / ${self.cfg.monthly_cap_usd:.2f}"
            )
        if self.cfg.monthly_cap_tokens is not None and monthly_usage_tokens >= self.cfg.monthly_cap_tokens:
            raise BudgetExceeded(
                f"Monthly token cap exceeded: {monthly_usage_tokens:,} / {self.cfg.monthly_cap_tokens:,}"
            )

        self.task = TaskState(task_id=task_id, user_id=user_id)
        log.info(f"[gr] begin task={task_id} user={user_id} "
                 f"monthly_used=${monthly_usage_usd:.2f}/{monthly_usage_tokens:,} tk")
        return self.task

    def done(self) -> None:
        if self.task:
            self.task._done = True

    def summary(self) -> dict:
        if not self.task:
            return {}
        return {
            "task_id":    self.task.task_id,
            "tokens":     self.task.total_tokens,
            "tokens_in":  self.task.tokens_in,
            "tokens_out": self.task.tokens_out,
            "cost_usd":   round(self.task.cost_usd, 6),
            "iterations": self.task.iterations,
            "retries":    self.task.retries,
            "duration_s": round(self.task.elapsed, 2),
            "done":       self.task._done,
        }

    # ── Iteration loop ─────────────────────────────────────────────────

    def iter_turns(self) -> Iterator[int]:
        """Generator — usalo como `for i in gr.iter_turns(): ...`.
        Aborta cuando: done() fue llamado, max_iterations, max_task_tokens,
        max_task_cost_usd, task_timeout_s, o cap mensual."""
        if not self.task:
            raise RuntimeError("begin_task() primero")
        while not self.task._done:
            self._assert_within_limits()
            self.task.iterations += 1
            if self.cfg.log_every_iteration:
                log.info(f"[gr] iter {self.task.iterations}/{self.cfg.max_iterations} "
                         f"tk={self.task.total_tokens:,} cost=${self.task.cost_usd:.4f} "
                         f"elapsed={self.task.elapsed:.1f}s")
            yield self.task.iterations
        log.info(f"[gr] task done after {self.task.iterations} iter — {self.summary()}")

    def _assert_within_limits(self) -> None:
        t = self.task
        assert t
        if t.iterations >= self.cfg.max_iterations:
            raise TaskBudgetExceeded(
                f"max_iterations reached ({self.cfg.max_iterations}). "
                f"Bug probable: loop de tool-use infinito."
            )
        if t.total_tokens >= self.cfg.max_task_tokens:
            raise TaskBudgetExceeded(
                f"max_task_tokens ({self.cfg.max_task_tokens:,}) excedido: "
                f"{t.total_tokens:,}. La tarea es demasiado grande."
            )
        if t.cost_usd >= self.cfg.max_task_cost_usd:
            raise TaskBudgetExceeded(
                f"max_task_cost_usd (${self.cfg.max_task_cost_usd:.2f}) excedido: "
                f"${t.cost_usd:.2f}."
            )
        if t.elapsed >= self.cfg.task_timeout_s:
            raise TaskTimeout(
                f"Task timeout {self.cfg.task_timeout_s}s excedido ({t.elapsed:.1f}s)."
            )
        # Monthly check en vivo (por si el user mandó muchas requests en paralelo)
        projected_usd = self._monthly_usd + t.cost_usd
        if self.cfg.monthly_cap_usd is not None and projected_usd >= self.cfg.monthly_cap_usd:
            raise BudgetExceeded(
                f"Monthly USD cap excedido durante la tarea: "
                f"${projected_usd:.2f} / ${self.cfg.monthly_cap_usd:.2f}"
            )

    # ── Usage recording ────────────────────────────────────────────────

    def record_usage(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_create_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> float:
        """Registra tokens usados en una call al LLM. Devuelve el costo USD."""
        if not self.task:
            return 0.0
        c = cost_for(model, prompt_tokens, completion_tokens, cache_create_tokens, cache_read_tokens)
        self.task.tokens_in  += prompt_tokens + cache_create_tokens + cache_read_tokens
        self.task.tokens_out += completion_tokens
        self.task.cost_usd   += c
        return c

    # ── Retry wrapper ──────────────────────────────────────────────────

    def with_retries(
        self,
        fn: Callable[[], object],
        is_retryable: Callable[[Exception], bool] = None,
        get_retry_after: Callable[[Exception], Optional[float]] = None,
    ):
        """Ejecuta fn() con exponential backoff + jitter cuando la excepción
        es considerada retryable. Respeta Retry-After si el proveedor lo
        entrega, pero con un techo (retry_after_hard_ceiling_s).

        is_retryable(e)      → True si vale la pena reintentar (default: 429 o 5xx)
        get_retry_after(e)   → segundos sugeridos por el provider (None si no hay)
        """
        is_retryable   = is_retryable   or _default_is_retryable
        get_retry_after = get_retry_after or _default_get_retry_after
        last_err = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return fn()
            except Exception as e:
                last_err = e
                if not is_retryable(e) or attempt == self.cfg.max_retries:
                    raise
                if self.task:
                    self.task.retries += 1
                # Qué delay aplicar
                suggested = get_retry_after(e)
                if suggested is not None:
                    if suggested > self.cfg.retry_after_hard_ceiling_s:
                        log.warning(f"[gr] provider pidió esperar {suggested}s > ceiling "
                                    f"{self.cfg.retry_after_hard_ceiling_s}s — abortando retries.")
                        raise RetryExhausted(f"Retry-After too large: {suggested}s") from e
                    delay = suggested
                else:
                    delay = min(
                        self.cfg.retry_max_delay,
                        self.cfg.retry_base_delay * (2 ** attempt),
                    )
                # Jitter
                if self.cfg.retry_jitter > 0:
                    delay *= 1 + random.uniform(-self.cfg.retry_jitter, self.cfg.retry_jitter)
                log.warning(f"[gr] retry {attempt+1}/{self.cfg.max_retries} tras {delay:.1f}s — {e}")
                time.sleep(max(0.1, delay))
        raise RetryExhausted(str(last_err)) from last_err


def _default_is_retryable(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "429" in msg or "rate" in msg or "too many" in msg
        or "502" in msg or "503" in msg or "504" in msg
        or "timeout" in msg or "temporarily" in msg
    )


def _default_get_retry_after(e: Exception) -> Optional[float]:
    # Si tu cliente HTTP expone el header, sacalo del response.
    # Defecto: no sugerimos nada y dejamos que el backoff exponencial decida.
    return None