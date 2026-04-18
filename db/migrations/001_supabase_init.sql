-- ─────────────────────────────────────────────────────────────────────────
-- Cukinator — Supabase multi-tenant initialization
--
-- Correr este script UNA VEZ en el SQL Editor de Supabase después de crear
-- el proyecto. Deja el siguiente setup:
--
--   shared.tenants             → catálogo de tenants + chat_ids
--   shared.create_tenant_schema(p_schema)  → función que arma schema por tenant
--   shared.rag_search(...)     → búsqueda vectorial cross-schema
--
-- Por cada tenant genera un schema con:
--   <tenant>.kb_documents      → RAG con pgvector (embedding 1536)
--   <tenant>.messages          → history de chat
--   <tenant>.memory_facts      → facts / memoria
--   <tenant>.config            → key-value versionado
--
-- Agregar un tenant nuevo:
--   INSERT INTO shared.tenants (slug, display_name, schema_name, owner_email)
--     VALUES ('nuevoslug', 'Nombre Visible', 'nuevoslug', 'mail@dueño.com');
--   SELECT shared.create_tenant_schema('nuevoslug');
-- ─────────────────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS shared;

-- Catálogo de tenants
CREATE TABLE IF NOT EXISTS shared.tenants (
    id           SERIAL PRIMARY KEY,
    slug         TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    schema_name  TEXT UNIQUE NOT NULL,
    owner_email  TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Mapeo chat_id → tenant
CREATE TABLE IF NOT EXISTS shared.tenant_chat_ids (
    tenant_slug TEXT NOT NULL REFERENCES shared.tenants(slug) ON DELETE CASCADE,
    chat_id     BIGINT NOT NULL,
    role        TEXT DEFAULT 'owner',
    added_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (tenant_slug, chat_id)
);

CREATE INDEX IF NOT EXISTS tenant_chat_ids_chat_idx ON shared.tenant_chat_ids(chat_id);


-- ── Función: crear schema completo para un tenant ────────────────────────
CREATE OR REPLACE FUNCTION shared.create_tenant_schema(p_schema TEXT)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', p_schema);

    -- RAG KB documents con pgvector
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.kb_documents (
            id           BIGSERIAL PRIMARY KEY,
            source       TEXT NOT NULL,
            chunk_index  INTEGER NOT NULL,
            content      TEXT NOT NULL,
            embedding    vector(1536),
            metadata     JSONB DEFAULT ''{}''::jsonb,
            namespace    TEXT DEFAULT ''general'',
            content_hash TEXT,
            created_at   TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(source, chunk_index)
        )', p_schema);

    -- Índice vectorial HNSW (hierarchical navigable small world):
    -- mejor latencia que ivfflat (~10× más rápido en queries), más memoria.
    -- Params: m=16 (conexiones por nodo), ef_construction=64 (calidad al
    -- construir). En query se tunea con SET hnsw.ef_search = 40.
    EXECUTE format('
        CREATE INDEX IF NOT EXISTS %I
            ON %I.kb_documents
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)',
        p_schema || '_kb_embedding_idx', p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.kb_documents (namespace)',
        p_schema || '_kb_ns_idx', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.kb_documents (source)',
        p_schema || '_kb_src_idx', p_schema);

    -- Chat history
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.messages (
            id         BIGSERIAL PRIMARY KEY,
            chat_id    BIGINT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            session_id TEXT,
            ts         TIMESTAMPTZ DEFAULT NOW()
        )', p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.messages (chat_id, ts DESC)',
        p_schema || '_messages_chat_idx', p_schema);

    -- Facts / memoria
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.memory_facts (
            id         BIGSERIAL PRIMARY KEY,
            category   TEXT,
            topic      TEXT,
            content    TEXT NOT NULL,
            source_msg BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )', p_schema);

    -- Configs versionadas
    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.config (
            id         BIGSERIAL PRIMARY KEY,
            key        TEXT NOT NULL,
            value      TEXT,
            version    INTEGER DEFAULT 1,
            active     BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )', p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.config (key) WHERE active',
        p_schema || '_config_key_idx', p_schema);
END;
$$;


-- ── Función: búsqueda RAG cross-schema ───────────────────────────────────
-- Útil para no duplicar lógica en el cliente Python: le pasás el schema y
-- el embedding de la query y devuelve top-K chunks ordenados por cosine.
CREATE OR REPLACE FUNCTION shared.rag_search(
    p_schema          TEXT,
    p_query_embedding vector(1536),
    p_namespace       TEXT DEFAULT NULL,
    p_top_k           INTEGER DEFAULT 5
)
RETURNS TABLE(
    source      TEXT,
    chunk_index INTEGER,
    content     TEXT,
    namespace   TEXT,
    score       DOUBLE PRECISION,
    metadata    JSONB
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY EXECUTE format($f$
        SELECT source, chunk_index, content, namespace,
               1 - (embedding <=> $1)::DOUBLE PRECISION AS score,
               metadata
        FROM %I.kb_documents
        WHERE ($2::text IS NULL OR namespace = $2)
        ORDER BY embedding <=> $1
        LIMIT $3
    $f$, p_schema)
    USING p_query_embedding, p_namespace, p_top_k;
END;
$$;


-- ── Bootstrap: solo el tenant base (reamerica) ───────────────────────────
-- Los tenants adicionales se agregan con:
--   INSERT INTO shared.tenants (slug, display_name, schema_name, owner_email)
--     VALUES ('slug', 'Display', 'slug', 'owner@mail.com');
--   SELECT shared.create_tenant_schema('slug');
-- O desde el bot via services/tenants.py → add_tenant(slug, name, email).
INSERT INTO shared.tenants (slug, display_name, schema_name, owner_email) VALUES
    ('reamerica', 'Reamerica Risk Advisors', 'reamerica', 'proyectoastroboy@gmail.com')
ON CONFLICT (slug) DO NOTHING;

SELECT shared.create_tenant_schema('reamerica');

-- Mapear el chat_id del owner al tenant reamerica
INSERT INTO shared.tenant_chat_ids (tenant_slug, chat_id, role) VALUES
    ('reamerica', 8626420783, 'owner')
ON CONFLICT DO NOTHING;
