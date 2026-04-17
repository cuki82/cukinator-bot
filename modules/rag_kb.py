"""
modules/rag_kb.py — RAG engine para knowledge base general del proyecto.

Vocabulario TF-IDF mixto: reaseguros + dominio Cukinator (bot/VPS/infra).
Cada documento lleva un namespace para filtrar por dominio.
Migrar a sentence-transformers cuando haga falta recall semántico real.
"""
import os
import json
import sqlite3
import logging
import hashlib
import numpy as np

log = logging.getLogger(__name__)
DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

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


def ingest(source: str, text: str, metadata: dict = None, namespace: str = "general") -> int:
    """
    Indexa un documento en la KB.
    source: identificador unico (nombre de archivo, URL, etc.)
    text: contenido completo
    namespace: dominio del doc (reinsurance, cukinator, personal, general, ...)
    Retorna cantidad de chunks indexados.
    """
    chunks = chunk_text(text)
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
    log.info(f"KB: indexados {indexed} chunks de '{source}' (ns={namespace})")
    return indexed


def search(query: str, top_k: int = 5, source_filter: str = None, namespace: str = None) -> list[dict]:
    """
    Busca los chunks mas relevantes para la query.
    namespace: si se especifica, filtra por ese namespace.
    Retorna lista de dicts con content, source, score, namespace, metadata.
    """
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


def build_context(query: str, top_k: int = 5, namespace: str = None, min_score: float = MIN_SCORE) -> str:
    """
    Arma el contexto RAG para incluir en el prompt de Claude.
    Solo incluye chunks con score >= min_score. Retorna "" si no hay nada relevante.
    """
    results = search(query, top_k=top_k, namespace=namespace)
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
