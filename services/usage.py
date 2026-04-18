"""
services/usage.py — tracking de tokens y costo por tenant + rate limiting.

Pricing aproximado de Anthropic (abril 2026, USD por 1M tokens):
    Haiku 4.5   : $1   in / $5   out
    Sonnet 4.6  : $3   in / $15  out
    Opus 4.5+   : $15  in / $75  out

OpenAI (Codex planner/summarizer):
    gpt-4o-mini : $0.15 in / $0.60 out
    gpt-5-codex : $1.25 in / $10   out  (aprox)

cost_usd se calcula por llamada y se acumula en shared.tenant_usage.

Uso:
    from services.usage import record, get_period, check_budget

    record(tenant="reamerica", model="claude-sonnet-4-6",
           tokens_in=2500, tokens_out=400)
    u = get_period("reamerica")          # dict con totales del mes
    ok, msg = check_budget("reamerica")  # False si excedió el budget mensual
"""
import logging
from datetime import date
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# USD por 1M tokens
PRICES = {
    # Claude
    "claude-haiku-4-5":  (1.0,  5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-5":   (15.0, 75.0),
    "claude-opus-4-6":   (15.0, 75.0),
    "claude-opus-4-7":   (15.0, 75.0),
    # OpenAI (Codex pipeline del worker)
    "gpt-4o-mini":       (0.15, 0.60),
    "gpt-5-codex":       (1.25, 10.0),
    # Embeddings (costo único, input)
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Retorna costo estimado en USD para esa llamada."""
    price_in, price_out = PRICES.get(model, (3.0, 15.0))  # default Sonnet
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000.0


def record(tenant: str, model: str, tokens_in: int, tokens_out: int) -> float:
    """Registra el uso en shared.tenant_usage. Retorna el cost_usd calculado.
    Fail silent si no hay PG — no debe bloquear al bot."""
    cost = estimate_cost(model, tokens_in, tokens_out)
    try:
        from services.db import pg_available, pg_conn
        if not pg_available() or not tenant:
            return cost
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT shared.accumulate_usage(%s, %s, %s, %s)",
                    (tenant, tokens_in, tokens_out, cost),
                )
    except Exception as e:
        log.debug(f"usage record skip: {e}")
    return cost


def get_period(tenant: str) -> dict:
    """Devuelve el consumo del mes actual del tenant."""
    try:
        from services.db import pg_available, pg_conn
        if not pg_available() or not tenant:
            return {}
        with pg_conn() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT tokens_in, tokens_out, cost_usd, msg_count,
                           period_start, period_end
                    FROM shared.tenant_usage
                    WHERE tenant_slug = %s
                      AND period_start = date_trunc('month', CURRENT_DATE)::date
                """, (tenant,))
                row = cur.fetchone()
                if not row:
                    return {"tenant": tenant, "tokens_in": 0, "tokens_out": 0,
                            "cost_usd": 0.0, "msg_count": 0}
                return {
                    "tenant": tenant,
                    "tokens_in": int(row[0] or 0),
                    "tokens_out": int(row[1] or 0),
                    "cost_usd": float(row[2] or 0),
                    "msg_count": int(row[3] or 0),
                    "period_start": row[4].isoformat() if row[4] else None,
                    "period_end":   row[5].isoformat() if row[5] else None,
                }
    except Exception as e:
        log.debug(f"usage get_period fail: {e}")
        return {}


def check_budget(tenant: str) -> Tuple[bool, str]:
    """Chequea si el tenant excedió su budget mensual.
    El budget vive en shared.tenants.settings.monthly_budget_usd.
    Si no hay budget configurado → (True, '') (sin límite).

    Retorna (ok, mensaje). Si ok=False, mensaje explica el excedido."""
    try:
        from services.tenants import get_tenant_config
        cfg = get_tenant_config(tenant)
        budget = (cfg.get("settings") or {}).get("monthly_budget_usd")
        if budget is None:
            return (True, "")
        usage = get_period(tenant)
        spent = float(usage.get("cost_usd", 0))
        if spent >= float(budget):
            return (False,
                    f"Tenant `{tenant}` superó el cupo mensual: "
                    f"${spent:.2f} / ${float(budget):.2f}. Se resetea el 1° del próximo mes.")
        return (True, "")
    except Exception as e:
        log.debug(f"check_budget fail: {e}")
        return (True, "")
