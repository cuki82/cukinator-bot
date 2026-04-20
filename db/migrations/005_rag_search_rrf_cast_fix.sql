-- Fix: shared.rag_search devolvía error
--   "Returned type numeric does not match expected type double precision in column 8"
-- Causa: el cálculo `1.0/($5 + rank)` en PG da NUMERIC, pero la función declara
-- rrf_score como DOUBLE PRECISION. Casteo explícito a double precision.

CREATE OR REPLACE FUNCTION shared.rag_search(
    p_schema           text,
    p_query_embedding  vector,
    p_query_text       text DEFAULT NULL::text,
    p_namespace        text DEFAULT NULL::text,
    p_top_k            integer DEFAULT 5,
    p_rrf_k            integer DEFAULT 60
)
RETURNS TABLE(
    id              bigint,
    source          text,
    chunk_index     integer,
    content         text,
    namespace       text,
    vector_score    double precision,
    bm25_score      double precision,
    rrf_score       double precision,
    metadata        jsonb
)
LANGUAGE plpgsql
AS $function$
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
                coalesce(v.vscore, 0)::DOUBLE PRECISION AS vscore,
                coalesce(t.bscore, 0)::DOUBLE PRECISION AS bscore,
                ((CASE WHEN v.vrank IS NULL THEN 0 ELSE 1.0/($5 + v.vrank) END)
                 + (CASE WHEN t.brank IS NULL THEN 0 ELSE 1.0/($5 + t.brank) END)
                )::DOUBLE PRECISION                     AS rrf
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
$function$;
