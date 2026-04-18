-- ─────────────────────────────────────────────────────────────────────────
-- Migración 002: audit log cross-tenant + RLS básica + vault en Supabase
-- ─────────────────────────────────────────────────────────────────────────

-- ── Audit log ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shared.audit_events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ DEFAULT NOW(),
    tenant_slug TEXT,
    chat_id     BIGINT,
    actor       TEXT,              -- 'user' | 'bot' | 'worker' | 'system'
    action      TEXT NOT NULL,     -- 'tool_invoke', 'vault_set', 'astro_save', 'intent', etc
    resource    TEXT,              -- tool name / table / entity afectado
    details     JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS audit_ts_idx       ON shared.audit_events(ts DESC);
CREATE INDEX IF NOT EXISTS audit_tenant_idx   ON shared.audit_events(tenant_slug, ts DESC);
CREATE INDEX IF NOT EXISTS audit_chat_idx     ON shared.audit_events(chat_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_action_idx   ON shared.audit_events(action);


-- ── Vault migrado a Supabase (Fernet blob) ─────────────────────────────
-- Schema shared porque las credenciales son del sistema, no de un tenant.
-- MASTER_KEY sigue viviendo fuera (en systemd Environment) — Supabase
-- almacena el ciphertext. Aunque se cayera Supabase el bot tendría
-- cached en memoria el set_at del startup.
CREATE TABLE IF NOT EXISTS shared.vault (
    key_name   TEXT PRIMARY KEY,
    value_enc  TEXT NOT NULL,       -- Fernet token (ya cifrado)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- ── Per-tenant system prompt override ──────────────────────────────────
-- Cada tenant puede definir su identidad/tono en shared.tenants.system_prompt.
-- Si es NULL, se usa el default del bot.
ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS system_prompt TEXT;
ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS display_language TEXT DEFAULT 'es-AR';
ALTER TABLE shared.tenants ADD COLUMN IF NOT EXISTS settings JSONB DEFAULT '{}'::jsonb;


-- ── RLS (Row Level Security) básica ────────────────────────────────────
-- Supabase anon/authenticated role no debería ver tablas sensibles.
-- El bot se conecta con el role "postgres" que bypassa RLS.
-- Habilitamos RLS como capa extra por si el role cambia.

ALTER TABLE shared.tenants           ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared.tenant_chat_ids   ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared.audit_events      ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared.vault             ENABLE ROW LEVEL SECURITY;

-- Policy: postgres role puede todo (service role del bot)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tenants' AND policyname = 'postgres_all') THEN
        CREATE POLICY postgres_all ON shared.tenants FOR ALL TO postgres USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'tenant_chat_ids' AND policyname = 'postgres_all') THEN
        CREATE POLICY postgres_all ON shared.tenant_chat_ids FOR ALL TO postgres USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'audit_events' AND policyname = 'postgres_all') THEN
        CREATE POLICY postgres_all ON shared.audit_events FOR ALL TO postgres USING (true) WITH CHECK (true);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'vault' AND policyname = 'postgres_all') THEN
        CREATE POLICY postgres_all ON shared.vault FOR ALL TO postgres USING (true) WITH CHECK (true);
    END IF;
END$$;

-- Los schemas por tenant (reamerica, personal, …) también con RLS
DO $$
DECLARE
    tenant_schema TEXT;
    tbl TEXT;
BEGIN
    FOR tenant_schema IN SELECT schema_name FROM shared.tenants
    LOOP
        FOR tbl IN
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = tenant_schema
        LOOP
            EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', tenant_schema, tbl);
            BEGIN
                EXECUTE format('CREATE POLICY postgres_all ON %I.%I FOR ALL TO postgres USING (true) WITH CHECK (true)', tenant_schema, tbl);
            EXCEPTION WHEN duplicate_object THEN NULL;
            END;
        END LOOP;
    END LOOP;
END$$;
