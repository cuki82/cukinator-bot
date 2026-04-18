-- ─────────────────────────────────────────────────────────────────────────
-- Migración 003: RAG híbrido (BM25 + vector) + citas estructuradas
--
-- Agrega a kb_documents de cada tenant (y del schema personal):
--   • columna content_tsv (tsvector) con full-text search en español
--   • trigger que mantiene content_tsv sincronizado con content
--   • índice GIN sobre content_tsv para queries BM25 rápidas
--
-- Actualiza shared.rag_search para devolver rank híbrido (RRF = Reciprocal
-- Rank Fusion, fórmula estándar: score = 1/(k + rank)) combinando el ranking
-- vectorial (cosine) y el ranking BM25 (ts_rank).
-- ─────────────────────────────────────────────────────────────────────────

-- Función para agregar tsvector + trigger + índice a un schema existente
CREATE OR REPLACE FUNCTION shared.add_hybrid_search(p_schema TEXT)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    -- Agregar columna tsvector si no existe
    EXECUTE format('
        ALTER TABLE %I.kb_documents
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
    ', p_schema);

    -- Poblar tsvector para rows existentes
    EXECUTE format('
        UPDATE %I.kb_documents
        SET content_tsv = to_tsvector(''spanish'', coalesce(content, ''''))
        WHERE content_tsv IS NULL
    ', p_schema);

    -- Trigger que actualiza tsvector en inserts/updates
    EXECUTE format('
        CREATE OR REPLACE FUNCTION %I.update_kb_tsv() RETURNS trigger
        LANGUAGE plpgsql AS $trg$
        BEGIN
            NEW.content_tsv := to_tsvector(''spanish'', coalesce(NEW.content, ''''));
            RETURN NEW;
        END;
        $trg$
    ', p_schema);

    EXECUTE format('
        DROP TRIGGER IF EXISTS kb_tsv_trigger ON %I.kb_documents
    ', p_schema);

    EXECUTE format('
        CREATE TRIGGER kb_tsv_trigger
        BEFORE INSERT OR UPDATE OF content
        ON %I.kb_documents
        FOR EACH ROW EXECUTE FUNCTION %I.update_kb_tsv()
    ', p_schema, p_schema);

    -- Índice GIN sobre tsvector (search fast)
    EXECUTE format('
        CREATE INDEX IF NOT EXISTS %I
        ON %I.kb_documents USING gin(content_tsv)
    ', p_schema || '_kb_tsv_idx', p_schema);
END;
$$;


-- Aplicar a todos los schemas existentes
SELECT shared.add_hybrid_search(schema_name)
FROM shared.tenants;
SELECT shared.add_hybrid_search('personal');


-- Extender create_tenant_schema para incluir tsvector en nuevos tenants
CREATE OR REPLACE FUNCTION shared.create_tenant_schema(p_schema TEXT)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', p_schema);

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
            content_tsv  tsvector,
            created_at   TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(source, chunk_index)
        )', p_schema);

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

    -- Agregar hybrid search a este nuevo schema
    PERFORM shared.add_hybrid_search(p_schema);

    -- Tablas auxiliares (como antes)
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

    EXECUTE format('
        CREATE TABLE IF NOT EXISTS %I.memory_facts (
            id         BIGSERIAL PRIMARY KEY,
            category   TEXT,
            topic      TEXT,
            content    TEXT NOT NULL,
            source_msg BIGINT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )', p_schema);

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


-- ── Hybrid search con RRF (Reciprocal Rank Fusion) ─────────────────────
-- Cada chunk es rankeado por: (a) similitud vectorial cosine y (b) ts_rank
-- (BM25-like). Se combinan con 1/(k + rank) por cada método y se suma.
-- k=60 es el valor estándar del paper original de Cormack.

DROP FUNCTION IF EXISTS shared.rag_search(TEXT, vector, TEXT, INTEGER);

CREATE OR REPLACE FUNCTION shared.rag_search(
    p_schema          TEXT,
    p_query_embedding vector(1536),
    p_query_text      TEXT DEFAULT NULL,
    p_namespace       TEXT DEFAULT NULL,
    p_top_k           INTEGER DEFAULT 5,
    p_rrf_k           INTEGER DEFAULT 60
)
RETURNS TABLE(
    id          BIGINT,
    source      TEXT,
    chunk_index INTEGER,
    content     TEXT,
    namespace   TEXT,
    vector_score DOUBLE PRECISION,
    bm25_score   DOUBLE PRECISION,
    rrf_score    DOUBLE PRECISION,
    metadata    JSONB
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY EXECUTE format($f$
        WITH vec AS (
            SELECT id, source, chunk_index, content, namespace, metadata,
                   1 - (embedding <=> $1)::DOUBLE PRECISION AS vscore,
                   ROW_NUMBER() OVER (ORDER BY embedding <=> $1) AS vrank
            FROM %I.kb_documents
            WHERE ($3::text IS NULL OR namespace = $3)
              AND embedding IS NOT NULL
            ORDER BY embedding <=> $1
            LIMIT ($4 * 3)
        ),
        ts AS (
            SELECT id, source, chunk_index, content, namespace, metadata,
                   ts_rank(content_tsv, websearch_to_tsquery('spanish', coalesce($2, '')))::DOUBLE PRECISION AS bscore,
                   ROW_NUMBER() OVER (
                       ORDER BY ts_rank(content_tsv, websearch_to_tsquery('spanish', coalesce($2, ''))) DESC
                   ) AS brank
            FROM %I.kb_documents
            WHERE ($3::text IS NULL OR namespace = $3)
              AND ($2::text IS NULL OR content_tsv @@ websearch_to_tsquery('spanish', $2))
            ORDER BY bscore DESC
            LIMIT ($4 * 3)
        ),
        merged AS (
            SELECT
                coalesce(v.id, t.id)                    AS id,
                coalesce(v.source, t.source)            AS source,
                coalesce(v.chunk_index, t.chunk_index)  AS chunk_index,
                coalesce(v.content, t.content)          AS content,
                coalesce(v.namespace, t.namespace)      AS namespace,
                coalesce(v.metadata, t.metadata)        AS metadata,
                coalesce(v.vscore, 0)                   AS vscore,
                coalesce(t.bscore, 0)                   AS bscore,
                (CASE WHEN v.vrank IS NULL THEN 0 ELSE 1.0/($5 + v.vrank) END)
                    + (CASE WHEN t.brank IS NULL THEN 0 ELSE 1.0/($5 + t.brank) END)
                    AS rrf
            FROM vec v
            FULL OUTER JOIN ts t ON v.id = t.id
        )
        SELECT id, source, chunk_index, content, namespace,
               vscore, bscore, rrf, metadata
        FROM merged
        ORDER BY rrf DESC
        LIMIT $4
    $f$, p_schema, p_schema)
    USING p_query_embedding, p_query_text, p_namespace, p_top_k, p_rrf_k;
END;
$$;
