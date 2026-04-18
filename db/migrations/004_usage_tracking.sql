-- ─────────────────────────────────────────────────────────────────────────
-- Migración 004: usage tracking + rate limiting per-tenant
--
-- shared.tenant_usage: acumula tokens/costo por tenant y período (mes).
-- Index sobre (tenant_slug, period_start) para lookup rápido del mes actual.
--
-- budget se guarda en shared.tenants.settings.monthly_budget_usd (si existe,
-- el middleware en bot_core chequea antes de llamar a la API).
-- ─────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS shared.tenant_usage (
    id           BIGSERIAL PRIMARY KEY,
    tenant_slug  TEXT NOT NULL,
    period_start DATE NOT NULL,        -- primer día del mes
    period_end   DATE NOT NULL,        -- último día del mes
    tokens_in    BIGINT DEFAULT 0,
    tokens_out   BIGINT DEFAULT 0,
    cost_usd     NUMERIC(10,4) DEFAULT 0,
    msg_count    INTEGER DEFAULT 0,
    updated_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_slug, period_start)
);

CREATE INDEX IF NOT EXISTS tenant_usage_slug_idx
    ON shared.tenant_usage(tenant_slug, period_start DESC);

-- Función para sumar tokens al período vigente (o crearlo).
CREATE OR REPLACE FUNCTION shared.accumulate_usage(
    p_tenant_slug  TEXT,
    p_tokens_in    BIGINT,
    p_tokens_out   BIGINT,
    p_cost_usd     NUMERIC
)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_period_start DATE;
    v_period_end   DATE;
BEGIN
    v_period_start := date_trunc('month', CURRENT_DATE)::date;
    v_period_end   := (date_trunc('month', CURRENT_DATE) + INTERVAL '1 month - 1 day')::date;

    INSERT INTO shared.tenant_usage
        (tenant_slug, period_start, period_end, tokens_in, tokens_out, cost_usd, msg_count, updated_at)
    VALUES
        (p_tenant_slug, v_period_start, v_period_end,
         p_tokens_in, p_tokens_out, p_cost_usd, 1, NOW())
    ON CONFLICT (tenant_slug, period_start) DO UPDATE SET
        tokens_in  = shared.tenant_usage.tokens_in  + EXCLUDED.tokens_in,
        tokens_out = shared.tenant_usage.tokens_out + EXCLUDED.tokens_out,
        cost_usd   = shared.tenant_usage.cost_usd   + EXCLUDED.cost_usd,
        msg_count  = shared.tenant_usage.msg_count  + 1,
        updated_at = NOW();
END;
$$;

-- RLS (consistente con el resto)
ALTER TABLE shared.tenant_usage ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tenant_usage' AND policyname = 'postgres_all') THEN
        CREATE POLICY postgres_all ON shared.tenant_usage FOR ALL TO postgres USING (true) WITH CHECK (true);
    END IF;
END$$;
