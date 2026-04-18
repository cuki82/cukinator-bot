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
    """Search vectorial HNSW. Retorna lista de dicts."""
    emb = _openai_embed(query)
    if emb is None:
        return []
    vec = _pg_vec_literal(emb)
    with pg_conn() as con:
        with con.cursor() as cur:
            # Tunear ef_search para latencia/recall trade-off
            cur.execute("SET LOCAL hnsw.ef_search = 40")
            if namespace:
                cur.execute(
                    f"""SELECT source, chunk_index, content, namespace,
                              1 - (embedding <=> %s::vector) AS score,
                              metadata
                        FROM {schema}.kb_documents
                        WHERE namespace = %s
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s""",
                    (vec, namespace, vec, top_k),
                )
            else:
                cur.execute(
                    f"""SELECT source, chunk_index, content, namespace,
                              1 - (embedding <=> %s::vector) AS score,
                              metadata
                        FROM {schema}.kb_documents
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s""",
                    (vec, vec, top_k),
                )
            rows = cur.fetchall()
    return [
        {
            "source": r[0], "chunk_index": r[1], "content": r[2],
            "namespace": r[3] or "general", "score": float(r[4]),
            "metadata": r[5] if isinstance(r[5], dict) else json.loads(r[5] or "{}"),
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


def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Divide texto en chunks con overlap."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


def ingest(source: str, text: str, metadata: dict = None, namespace: str = "general",
           tenant: str = None, chat_id: int = None) -> int:
    """
    Indexa un documento en la KB del tenant.
    - Si hay Postgres (pgvector): embedding OpenAI + insert en <tenant>.kb_documents.
    - Si no: TF-IDF + SQLite local (fallback).
    - tenant: slug del tenant; si no se pasa, se resuelve de chat_id; sino default.
    """
    chunks = chunk_text(text)
    if not chunks:
        return 0

    # Backend Postgres + pgvector
    if pg_available():
        slug = tenant or (resolve_tenant(chat_id) if chat_id else DEFAULT_TENANT)
        schema = tenant_schema(slug)
        try:
            return _pg_ingest(schema, source, chunks, metadata or {}, namespace)
        except Exception as e:
            log.error(f"pg ingest falló ({slug}.{source}): {e} — fallback a SQLite")

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
           tenant: str = None, chat_id: int = None) -> list:
    """
    Busca los chunks más relevantes para la query en la KB del tenant.
    namespace: filtra por dominio (reinsurance, cukinator, personal, ...).
    Si hay Postgres: HNSW + OpenAI embeddings. Sino: TF-IDF + SQLite.
    """
    # Backend Postgres
    if pg_available():
        slug = tenant or (resolve_tenant(chat_id) if chat_id else DEFAULT_TENANT)
        schema = tenant_schema(slug)
        try:
            return _pg_search(schema, query, top_k, namespace)
        except Exception as e:
            log.error(f"pg search falló ({slug}): {e} — fallback a SQLite")

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
                  chat_id: int = None) -> str:
    """
    Arma el contexto RAG para incluir en el prompt de Claude.
    Solo incluye chunks con score >= min_score. Retorna "" si no hay nada relevante.
    Multi-tenant: usa el schema del tenant resuelto del chat_id (o el explícito).
    """
    results = search(query, top_k=top_k, namespace=namespace, tenant=tenant, chat_id=chat_id)
    results = [r for r in results if r["score"] >= min_score]
    if not results:
        return ""
    header = "Contexto relevante de la knowledge base"
    if namespace:
        header += f" ({namespace})"
    parts = [header + ":\n"]
    for i, r in enumerate(results, 1):
        score_pct = int(r["score"] * 100)
        ns_tag = f" · {r['namespace']}" if r.get("namespace") and r["namespace"] != "general" else ""
        parts.append(f"[{i}] {r['source']}{ns_tag} (relevancia {score_pct}%):\n{r['content']}\n")
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
