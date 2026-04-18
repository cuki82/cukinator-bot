"""
modules/rag_kb.py — RAG engine dual-mode: pgvector (Supabase) o SQLite+TF-IDF.

Backend preferido: pgvector con OpenAI text-embedding-3-small (1536 dim).
La búsqueda corre en Postgres con índice HNSW (latencia ~5-20ms por query
sobre 100k chunks). Multi-tenant: cada tenant tiene su propio schema con
su propio kb_documents (reamerica.kb_documents, diaz.kb_documents, etc).

Backend fallback (cuando no hay Supabase configurada): SQLite + numpy +
TF-IDF con vocab mixto. Mantiene el bot funcional en dev o mientras no se
haya migrado. El switching es automático: pg_available() → pgvector, sino
TF-IDF local.

Cada chunk lleva un `namespace` (reaseguros, cukinator, personal, etc.)
para filtrar por dominio dentro del mismo tenant.
"""
import os
import json
import sqlite3
import logging
import hashlib
import requests
import numpy as np

log = logging.getLogger(__name__)
DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

# ── Backend detection ─────────────────────────────────────────────────────
try:
    from services.db import pg_available, pg_conn  # type: ignore
    from services.tenants import resolve_tenant, tenant_schema, DEFAULT_TENANT  # type: ignore
    _HAS_PG_LAYER = True
except Exception:
    _HAS_PG_LAYER = False
    def pg_available() -> bool: return False
    def resolve_tenant(_cid): return "reamerica"
    def tenant_schema(s): return s
    DEFAULT_TENANT = "reamerica"


_OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
_OPENAI_EMBED_DIM = 1536  # debe coincidir con vector(N) del schema SQL


def _openai_embed(text: str):
    """Un embedding vía OpenAI /v1/embeddings. Devuelve lista de floats o None.
    Latencia típica: 50-150ms."""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": _OPENAI_EMBED_MODEL, "input": text[:8000]},
            timeout=15,
        )
        if r.status_code != 200:
            log.error(f"openai embed {r.status_code}: {r.text[:200]}")
            return None
        return r.json()["data"][0]["embedding"]
    except Exception as e:
        log.error(f"openai embed error: {e}")
        return None


def _openai_embed_batch(texts: list) -> list:
    """Embeddings en batch. Más rápido que uno-a-uno para ingest masivo."""
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
    if not key:
        return [None] * len(texts)
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": _OPENAI_EMBED_MODEL, "input": [t[:8000] for t in texts]},
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"openai embed batch {r.status_code}: {r.text[:200]}")
            return [None] * len(texts)
        data = r.json()["data"]
        return [d["embedding"] for d in data]
    except Exception as e:
        log.error(f"openai embed batch error: {e}")
        return [None] * len(texts)


# ── Backend pgvector ──────────────────────────────────────────────────────

def _pg_vec_literal(vec: list) -> str:
    """Formato de vector para pgvector: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def _pg_ingest(schema: str, source: str, chunks: list, metadata: dict, namespace: str) -> int:
    """Ingest batch a Postgres. Usa embeddings OpenAI en batch."""
    embs = _openai_embed_batch(chunks)
    meta_json = json.dumps(metadata or {})
    indexed = 0
    with pg_conn() as con:
        with con.cursor() as cur:
            for i, (chunk, emb) in enumerate(zip(chunks, embs)):
                if emb is None:
                    log.warning(f"skip chunk {i} de {source}: embedding vacío")
                    continue
                content_hash = hashlib.md5(chunk.encode()).hexdigest()
                cur.execute(
                    f"""INSERT INTO {schema}.kb_documents
                        (source, chunk_index, content, embedding, metadata, namespace, content_hash)
                        VALUES (%s, %s, %s, %s::vector, %s::jsonb, %s, %s)
                        ON CONFLICT (source, chunk_index) DO UPDATE
                        SET content = EXCLUDED.content,
                            embedding = EXCLUDED.embedding,
                            metadata = EXCLUDED.metadata,
                            namespace = EXCLUDED.namespace,
                            content_hash = EXCLUDED.content_hash""",
                    (source, i, chunk, _pg_vec_literal(emb), meta_json, namespace, content_hash),
                )
                indexed += 1
    log.info(f"pg ingest {schema}.{source}: {indexed}/{len(chunks)} chunks (ns={namespace})")
    return indexed


def _pg_search(schema: str, query: str, top_k: int, namespace):
    """Search híbrido (vector + BM25) con RRF. Usa shared.rag_search."""
    emb = _openai_embed(query)
    if emb is None:
        return []
    vec = _pg_vec_literal(emb)
    with pg_conn() as con:
        with con.cursor() as cur:
            # Tunear ef_search para latencia/recall trade-off
            cur.execute("SET LOCAL hnsw.ef_search = 40")
            cur.execute(
                "SELECT * FROM shared.rag_search(%s, %s::vector, %s, %s, %s)",
                (schema, vec, query, namespace, top_k),
            )
            rows = cur.fetchall()
    # columns: id, source, chunk_index, content, namespace, vscore, bscore, rrf, metadata
    return [
        {
            "id":          r[0],
            "source":      r[1],
            "chunk_index": r[2],
            "content":     r[3],
            "namespace":   r[4] or "general",
            "score":       float(r[7] or 0),          # score principal = RRF
            "vector_score": float(r[5] or 0),
            "bm25_score":  float(r[6] or 0),
            "metadata":    r[8] if isinstance(r[8], dict) else json.loads(r[8] or "{}"),
        }
        for r in rows
    ]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content     TEXT NOT NULL,
    embedding   BLOB,
    metadata    TEXT DEFAULT '{}',
    namespace   TEXT DEFAULT 'general',
    content_hash TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_kb_source ON kb_documents(source);
CREATE INDEX IF NOT EXISTS idx_kb_namespace ON kb_documents(namespace);
"""


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    # Migración: agregar columna namespace si la tabla ya existía sin ella
    cols = [r[1] for r in con.execute("PRAGMA table_info(kb_documents)").fetchall()]
    if "namespace" not in cols:
        con.execute("ALTER TABLE kb_documents ADD COLUMN namespace TEXT DEFAULT 'general'")
    con.commit()
    return con


def _embed(texts: list[str]) -> list[list[float]]:
    """
    Genera embeddings usando Claude via prompt engineering.
    Usa TF-IDF como fallback si no hay API key.
    """
    try:
        import anthropic
        key = os.environ.get("ANTHROPIC_KEY", "")
        if not key:
            raise ValueError("No ANTHROPIC_KEY")
        client = anthropic.Anthropic(api_key=key)
        embeddings = []
        for text in texts:
            # Usar claude para generar un vector de caracteristicas semanticas
            # Este es un approach pragmatico: pedirle a claude un resumen estructurado
            # y luego usar hash/TF-IDF sobre ese resumen como embedding proxy
            # Para embeddings reales usar OpenAI o sentence-transformers
            raise NotImplementedError("Use sentence-transformers o OpenAI para embeddings reales")
    except Exception:
        pass

    # Fallback: TF-IDF simple con vocabulario fijo de reaseguros
    return [_tfidf_embed(t) for t in texts]


# Vocabulario mixto: reaseguros + dominio Cukinator (bot/infra/dev/productividad)
_VOCAB = [
    # Reaseguros
    "reaseguro", "reasegurador", "cedente", "cesion", "retrocesion",
    "prima", "siniestro", "cobertura", "clausula", "treaty", "facultativo",
    "proporcional", "exceso", "catastrofe", "quota", "surplus", "stop",
    "retencion", "limite", "sublimite", "franquicia", "deducible",
    "cartera", "riesgo", "exposicion", "acumulacion", "pml", "eml",
    "vigencia", "renovacion", "slip", "bordero", "cuenta", "liquidacion",
    "siniestralidad", "frecuencia", "severidad", "ibnr",
    "reserva", "desarrollo", "triangulo", "actuarial",
    "vida", "incendio", "responsabilidad", "marino", "aviacion",
    "property", "casualty", "liability", "engineering", "agriculture",
    # Cukinator / bot / infra
    "cukinator", "bot", "telegram", "railway", "vps", "hostinger",
    "systemd", "service", "docker", "container", "journalctl",
    "worker", "agent", "orchestrator", "handler", "intent", "router",
    "mcp", "tool", "endpoint", "health", "deploy", "commit", "push",
    "github", "repo", "branch", "main", "pull", "dockerfile",
    "python", "fastapi", "uvicorn", "pydantic", "sqlite", "postgres",
    "vault", "fernet", "secret", "credential", "token", "env",
    "memory", "kb", "rag", "embedding", "chunk", "search",
    "claude", "anthropic", "openai", "llm", "api", "opus", "sonnet", "haiku",
    # Personal / productividad
    "gmail", "calendar", "email", "agenda", "evento", "reunion",
    "reaemrica", "astro", "carta", "natal", "planeta", "signo",
    "whatsapp", "voz", "audio", "whisper", "tts", "elevenlabs",
    # Operación / estado
    "error", "log", "debug", "bug", "fix", "refactor", "test",
    "active", "running", "failed", "standby", "zombie", "conflict",
]

_VOCAB_IDX = {w: i for i, w in enumerate(_VOCAB)}


def _tfidf_embed(text: str) -> list[float]:
    """Vector TF-IDF sobre vocabulario de reaseguros. Dimensión fija = len(_VOCAB)."""
    words = text.lower().split()
    vec = np.zeros(len(_VOCAB), dtype=np.float32)
    for w in words:
        if w in _VOCAB_IDX:
            vec[_VOCAB_IDX[w]] += 1.0
    # Normalizar
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec.tolist()


def _vec_to_blob(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list:
    """Chunking simple por cantidad de palabras con overlap. Uso legacy."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


def chunk_text_semantic(text: str, target_words: int = 350,
                        min_words: int = 80, max_words: int = 550) -> list:
    """Chunking semántico: respeta párrafos y secciones antes de partir.

    Estrategia:
      1. Partir por dobles newlines (párrafos naturales).
      2. Agrupar párrafos consecutivos hasta llegar a target_words.
      3. Si un párrafo solo excede max_words, usar chunk_text() sobre él.
      4. No dejar chunks con menos de min_words salvo el último.

    Para docs estructurados (PDFs de reaseguros, manuales), preserva
    el contexto semántico mucho mejor que cortar por N palabras fijas.
    """
    import re as _re
    # Normalizar saltos de línea y tratar encabezados markdown como separadores duros
    text = _re.sub(r"\r\n", "\n", text or "")
    # Insertamos separador extra antes de encabezados (# / ## / ### ...)
    text = _re.sub(r"\n(#{1,6} .+)", r"\n\n\1", text)

    # Partir por párrafos (dobles newlines)
    parrs = [p.strip() for p in _re.split(r"\n{2,}", text) if p.strip()]
    if not parrs:
        return []

    chunks = []
    current = []
    current_wc = 0
    for p in parrs:
        wc = len(p.split())
        if wc > max_words:
            # Flush el current antes del párrafo gigante
            if current:
                chunks.append("\n\n".join(current))
                current, current_wc = [], 0
            # Un párrafo solo demasiado grande → sub-chunk por palabras
            for sub in chunk_text(p, chunk_size=max_words, overlap=30):
                chunks.append(sub)
            continue
        if current_wc + wc > target_words and current_wc >= min_words:
            # Flush
            chunks.append("\n\n".join(current))
            current = [p]
            current_wc = wc
        else:
            current.append(p)
            current_wc += wc
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _resolve_schema(tenant: str = None, chat_id: int = None, schema: str = None) -> str:
    """Si se pasa schema directo, lo usa (ej. 'personal'). Sino resuelve del tenant."""
    if schema:
        # Validación anti-SQL-injection
        if not all(c.isalnum() or c == "_" for c in schema):
            raise ValueError(f"schema inválido: {schema!r}")
        return schema
    slug = tenant or (resolve_tenant(chat_id) if chat_id else DEFAULT_TENANT)
    return tenant_schema(slug)


def ingest(source: str, text: str, metadata: dict = None, namespace: str = "general",
           tenant: str = None, chat_id: int = None, schema: str = None,
           semantic: bool = True) -> int:
    """
    Indexa un documento en la KB del tenant (o del schema explícito).
    - schema: si se pasa, usa ese schema directo (ej. 'personal').
    - semantic=True (default): usa chunk_text_semantic que respeta párrafos
      y secciones. False → chunk_text legacy por N palabras.
    - Si hay Postgres (pgvector): embedding OpenAI + insert con hybrid search.
    - Si no: TF-IDF + SQLite local (fallback).
    """
    chunks = chunk_text_semantic(text) if semantic else chunk_text(text)
    if not chunks:
        return 0

    # Backend Postgres + pgvector
    if pg_available():
        resolved = _resolve_schema(tenant=tenant, chat_id=chat_id, schema=schema)
        try:
            return _pg_ingest(resolved, source, chunks, metadata or {}, namespace)
        except Exception as e:
            log.error(f"pg ingest falló ({resolved}.{source}): {e} — fallback a SQLite")

    # Fallback SQLite + TF-IDF
    embeddings = _embed(chunks)
    meta_str = json.dumps(metadata or {})
    con = _conn()
    indexed = 0
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        content_hash = hashlib.md5(chunk.encode()).hexdigest()
        try:
            con.execute(
                """INSERT OR REPLACE INTO kb_documents
                   (source, chunk_index, content, embedding, metadata, namespace, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, i, chunk, _vec_to_blob(emb), meta_str, namespace, content_hash)
            )
            indexed += 1
        except Exception as e:
            log.error(f"Error indexando chunk {i} de {source}: {e}")
    con.commit()
    con.close()
    log.info(f"KB(sqlite): indexados {indexed} chunks de '{source}' (ns={namespace})")
    return indexed


def search(query: str, top_k: int = 5, source_filter: str = None, namespace: str = None,
           tenant: str = None, chat_id: int = None, schema: str = None) -> list:
    """
    Busca los chunks más relevantes para la query.
    schema: si se pasa, busca en ese schema directo (ej. 'personal'). Sino usa
      el del tenant resuelto de chat_id.
    """
    if pg_available():
        resolved = _resolve_schema(tenant=tenant, chat_id=chat_id, schema=schema)
        try:
            return _pg_search(resolved, query, top_k, namespace)
        except Exception as e:
            log.error(f"pg search falló ({resolved}): {e} — fallback a SQLite")

    # Fallback SQLite + TF-IDF
    query_vec = np.array(_embed([query])[0], dtype=np.float32)
    con = _conn()
    where = "WHERE embedding IS NOT NULL"
    params = []
    if source_filter:
        where += " AND source LIKE ?"
        params.append(f"%{source_filter}%")
    if namespace:
        where += " AND namespace = ?"
        params.append(namespace)
    rows = con.execute(
        f"SELECT source, chunk_index, content, embedding, metadata, namespace FROM kb_documents {where}",
        params
    ).fetchall()
    con.close()

    scored = []
    for source, chunk_idx, content, emb_blob, meta_str, ns in rows:
        emb = _blob_to_vec(emb_blob)
        score = _cosine(query_vec, emb)
        scored.append({
            "source": source,
            "chunk_index": chunk_idx,
            "content": content,
            "score": score,
            "namespace": ns or "general",
            "metadata": json.loads(meta_str or "{}")
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


# Score mínimo para considerar un chunk relevante. Bajo este umbral se descarta
# para no inyectar ruido en el prompt cuando la query no tiene match real en KB.
MIN_SCORE = 0.15


def build_context(query: str, top_k: int = 5, namespace: str = None,
                  min_score: float = MIN_SCORE, tenant: str = None,
                  chat_id: int = None, schema: str = None,
                  with_citations: bool = True) -> str:
    """
    Arma el contexto RAG para incluir en el prompt de Claude.
    Cada chunk se marca con [C1], [C2], ... para que el LLM pueda citar
    las fuentes explícitamente en su respuesta.

    Solo incluye chunks con score >= min_score.
    """
    results = search(query, top_k=top_k, namespace=namespace, tenant=tenant,
                     chat_id=chat_id, schema=schema)
    results = [r for r in results if r["score"] >= min_score]
    if not results:
        return ""
    header = "Contexto relevante de la knowledge base"
    if namespace:
        header += f" (namespace {namespace})"
    parts = [header + ":\n"]
    for i, r in enumerate(results, 1):
        src = r.get("source", "?")
        ns_tag = r.get("namespace", "general")
        chunk_idx = r.get("chunk_index", 0)
        score = r.get("score", 0)
        score_pct = int(score * 100) if score < 1 else int(score * 10)
        # ID amigable para que el LLM cite: [C1], [C2], ...
        tag = f"[C{i}]"
        parts.append(
            f"{tag} fuente=`{src}` · ns=`{ns_tag}` · chunk={chunk_idx} · rel={score_pct}\n"
            f"{r['content']}\n"
        )
    if with_citations:
        parts.append(
            "\n[INSTRUCCIÓN CITAS]: al usar información de este contexto, citá "
            "la fuente usando la marca [C1], [C2], etc. que aparece arriba. "
            "Ej: 'Según el treaty XYZ [C2], la cedente…'"
        )
    return "\n".join(parts)


def list_sources() -> list[dict]:
    """Lista todos los documentos indexados."""
    con = _conn()
    rows = con.execute(
        """SELECT source, COUNT(*) as chunks, MAX(created_at) as updated
           FROM kb_documents GROUP BY source ORDER BY source"""
    ).fetchall()
    con.close()
    return [{"source": r[0], "chunks": r[1], "updated": r[2][:16]} for r in rows]


def delete_source(source: str) -> int:
    """Elimina todos los chunks de un documento."""
    con = _conn()
    cur = con.execute("DELETE FROM kb_documents WHERE source = ?", (source,))
    con.commit()
    con.close()
    return cur.rowcount
