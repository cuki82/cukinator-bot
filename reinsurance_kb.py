"""
reinsurance_kb.py — Módulo de conocimiento de reaseguros e insurance operations.
Base de datos estructurada con pipeline de ingesta y consulta semántica via Claude.
"""

import sqlite3
import json
import datetime
import hashlib
import os
import logging

log = logging.getLogger(__name__)
DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

# ── Schema ─────────────────────────────────────────────────────────────────────
SCHEMA_REINSURANCE = """

-- Documentos fuente
CREATE TABLE IF NOT EXISTS ri_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_hash     TEXT UNIQUE,
    title        TEXT NOT NULL,
    source_type  TEXT NOT NULL,  -- doctrine | wording | regulation | operational
    source_org   TEXT,           -- LMA, Lloyd's, SSN, etc.
    reference_code TEXT,
    risk_type    TEXT,
    jurisdiction TEXT DEFAULT 'AR',
    status       TEXT DEFAULT 'active',  -- active | withdrawn | superseded
    doc_date     TEXT,
    language     TEXT DEFAULT 'es',
    file_path    TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Chunks de texto para búsqueda
CREATE TABLE IF NOT EXISTS ri_chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id),
    chunk_index  INTEGER,
    content      TEXT NOT NULL,
    keywords     TEXT,  -- JSON array
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Conceptos y definiciones derivados
CREATE TABLE IF NOT EXISTS ri_concepts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id),
    term         TEXT NOT NULL,
    definition   TEXT NOT NULL,
    domain       TEXT,  -- treaty, facultative, retro, pricing, claims, etc.
    source_type  TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Resúmenes ejecutivos por documento
CREATE TABLE IF NOT EXISTS ri_summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id) UNIQUE,
    executive    TEXT,  -- resumen ejecutivo
    key_points   TEXT,  -- JSON array
    operational  TEXT,  -- implicancia operativa
    risks        TEXT,  -- alertas y ambigüedades
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- QA pairs generados
CREATE TABLE IF NOT EXISTS ri_qa (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id),
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    domain       TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Cláusulas de wordings
CREATE TABLE IF NOT EXISTS ri_clauses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id),
    clause_ref   TEXT,  -- NMA 1234, LMA 5000, etc.
    clause_name  TEXT,
    clause_type  TEXT,  -- exclusion, condition, definition, coverage
    summary      TEXT,
    operational_effect TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Reglas normativas
CREATE TABLE IF NOT EXISTS ri_regulations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id),
    article_ref  TEXT,  -- Art. 158 Ley 17.418
    content      TEXT,
    consolidated TEXT,
    effective_date TEXT,
    operational_impact TEXT,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Jobs de ingesta
CREATE TABLE IF NOT EXISTS ri_ingestion_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       INTEGER REFERENCES ri_documents(id),
    status       TEXT DEFAULT 'pending',  -- pending | processing | done | error
    steps_done   TEXT DEFAULT '[]',  -- JSON array
    error_msg    TEXT,
    started_at   DATETIME,
    finished_at  DATETIME,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ri_chunks_doc   ON ri_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_ri_concepts_term ON ri_concepts(term);
CREATE INDEX IF NOT EXISTS idx_ri_qa_domain    ON ri_qa(domain);
CREATE INDEX IF NOT EXISTS idx_ri_clauses_ref  ON ri_clauses(clause_ref);
"""

DOMAIN_KEYWORDS = {
    "treaty": ["treaty", "tratado", "proporcional", "no proporcional", "quota share",
                "surplus", "excess of loss", "stop loss", "aggregate"],
    "facultative": ["facultativo", "facultative", "individual", "caso a caso"],
    "retrocession": ["retrocesion", "retrocession", "retrocesionario"],
    "pricing": ["pricing", "rate", "tarifa", "prima", "burning cost", "loss ratio"],
    "claims": ["siniestro", "claim", "liquidacion", "loss", "reserve", "ibnr"],
    "underwriting": ["underwriting", "suscripcion", "riesgo", "exposure", "tiv"],
    "normativa": ["ley", "resolucion", "ssn", "art.", "articulo", "codigo"],
    "wording": ["clausula", "clause", "endorsement", "policy", "poliza", "wording"],
}


def init_reinsurance_kb(db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_REINSURANCE)
    con.commit()
    con.close()
    log.info("Reinsurance KB initialized")


def detect_domain(text: str) -> list:
    """Detecta los dominios de reaseguros presentes en el texto."""
    text_lower = text.lower()
    found = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            found.append(domain)
    return found or ["general"]


def is_reinsurance_context(text: str) -> bool:
    """Determina si el texto pertenece al dominio de reaseguros."""
    triggers = [
        "reinsur", "reasegur", "treaty", "tratado", "facultative", "facultativo",
        "retrocession", "retrocesion", "underwriting", "suscripcion",
        "treaty", "wording", "clausula", "ssn", "resolucion ssn",
        "ley 17418", "ley 20091", "mga", "mgu", "program business",
        "burning cost", "loss ratio", "ibnr", "quota share", "excess of loss",
        "lma", "lloyd", "nma", "slip", "coverholder"
    ]
    text_lower = text.lower()
    return any(t in text_lower for t in triggers)


# ── CRUD documentos ────────────────────────────────────────────────────────────
def create_document(title: str, source_type: str, source_org: str = None,
                    reference_code: str = None, risk_type: str = None,
                    jurisdiction: str = "AR", file_path: str = None,
                    db_path: str = None) -> int:
    path = db_path or DB_PATH
    doc_hash = hashlib.md5(f"{title}{source_type}{source_org}".encode()).hexdigest()
    con = sqlite3.connect(path)
    try:
        cur = con.execute("""
            INSERT INTO ri_documents (doc_hash, title, source_type, source_org,
                reference_code, risk_type, jurisdiction, file_path)
            VALUES (?,?,?,?,?,?,?,?)
        """, (doc_hash, title, source_type, source_org, reference_code,
               risk_type, jurisdiction, file_path))
        doc_id = cur.lastrowid
        # Crear job de ingesta
        con.execute("INSERT INTO ri_ingestion_jobs (doc_id, status) VALUES (?,?)",
                    (doc_id, "pending"))
        con.commit()
        return doc_id
    except sqlite3.IntegrityError:
        # Ya existe
        row = con.execute("SELECT id FROM ri_documents WHERE doc_hash=?", (doc_hash,)).fetchone()
        return row[0] if row else -1
    finally:
        con.close()


def add_chunk(doc_id: int, content: str, chunk_index: int,
              keywords: list = None, db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.execute("""
        INSERT INTO ri_chunks (doc_id, chunk_index, content, keywords)
        VALUES (?,?,?,?)
    """, (doc_id, chunk_index, content, json.dumps(keywords or [])))
    con.commit()
    con.close()


def add_concept(doc_id: int, term: str, definition: str, domain: str = None,
                db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.execute("""
        INSERT OR REPLACE INTO ri_concepts (doc_id, term, definition, domain, source_type)
        SELECT ?,?,?,?, source_type FROM ri_documents WHERE id=?
    """, (doc_id, term, definition, domain, doc_id))
    con.commit()
    con.close()


def add_summary(doc_id: int, executive: str, key_points: list,
                operational: str, risks: str = None, db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.execute("""
        INSERT OR REPLACE INTO ri_summaries
        (doc_id, executive, key_points, operational, risks)
        VALUES (?,?,?,?,?)
    """, (doc_id, executive, json.dumps(key_points), operational, risks))
    con.commit()
    con.close()


def add_qa(doc_id: int, question: str, answer: str, domain: str = None,
           db_path: str = None):
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    con.execute("""
        INSERT INTO ri_qa (doc_id, question, answer, domain)
        VALUES (?,?,?,?)
    """, (doc_id, question, answer, domain))
    con.commit()
    con.close()


# ── Búsqueda ───────────────────────────────────────────────────────────────────
def search_knowledge(query: str, limit: int = 8, db_path: str = None) -> dict:
    """Búsqueda por keywords en conceptos, chunks y QA."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    words = [w.lower().strip() for w in query.split() if len(w) > 2][:6]
    results = {"concepts": [], "chunks": [], "qa": [], "summaries": []}

    for word in words:
        like = f"%{word}%"

        # Conceptos
        rows = con.execute("""
            SELECT c.term, c.definition, c.domain, d.title, d.source_type
            FROM ri_concepts c JOIN ri_documents d ON c.doc_id=d.id
            WHERE LOWER(c.term) LIKE ? OR LOWER(c.definition) LIKE ?
            LIMIT ?
        """, (like, like, limit)).fetchall()
        for r in rows:
            entry = {"term": r[0], "definition": r[1], "domain": r[2],
                     "source": r[3], "type": r[4]}
            if entry not in results["concepts"]:
                results["concepts"].append(entry)

        # QA
        rows = con.execute("""
            SELECT q.question, q.answer, q.domain, d.title
            FROM ri_qa q JOIN ri_documents d ON q.doc_id=d.id
            WHERE LOWER(q.question) LIKE ? OR LOWER(q.answer) LIKE ?
            LIMIT ?
        """, (like, like, limit)).fetchall()
        for r in rows:
            entry = {"question": r[0], "answer": r[1], "domain": r[2], "source": r[3]}
            if entry not in results["qa"]:
                results["qa"].append(entry)

        # Chunks
        rows = con.execute("""
            SELECT ch.content, d.title, d.source_type
            FROM ri_chunks ch JOIN ri_documents d ON ch.doc_id=d.id
            WHERE LOWER(ch.content) LIKE ?
            LIMIT ?
        """, (like, limit // 2)).fetchall()
        for r in rows:
            entry = {"content": r[0][:400], "source": r[1], "type": r[2]}
            if entry not in results["chunks"]:
                results["chunks"].append(entry)

    con.close()
    return results


def get_document_list(source_type: str = None, db_path: str = None) -> list:
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    if source_type:
        rows = con.execute("""
            SELECT id, title, source_type, source_org, reference_code, jurisdiction, status, created_at
            FROM ri_documents WHERE source_type=? ORDER BY created_at DESC
        """, (source_type,)).fetchall()
    else:
        rows = con.execute("""
            SELECT id, title, source_type, source_org, reference_code, jurisdiction, status, created_at
            FROM ri_documents ORDER BY created_at DESC
        """).fetchall()
    con.close()
    return [{"id": r[0], "title": r[1], "type": r[2], "org": r[3],
             "ref": r[4], "jurisdiction": r[5], "status": r[6], "date": r[7]}
            for r in rows]


def get_kb_stats(db_path: str = None) -> dict:
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    stats = {}
    for table in ["ri_documents", "ri_chunks", "ri_concepts", "ri_qa",
                  "ri_clauses", "ri_summaries"]:
        try:
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            stats[table.replace("ri_", "")] = count
        except Exception:
            stats[table.replace("ri_", "")] = 0
    con.close()
    return stats


# ── Pipeline de ingesta ────────────────────────────────────────────────────────
def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> list:
    """Divide texto en chunks con overlap."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


def build_enrichment_prompt(text: str, source_type: str, title: str) -> str:
    """Genera el prompt para que Claude enriquezca un documento."""
    return f"""Analizá este fragmento de un documento de {source_type} de reaseguros titulado "{title}".

Respondé SOLO con JSON válido con este formato:
{{
  "concepts": [
    {{"term": "...", "definition": "...", "domain": "treaty|facultative|pricing|claims|normativa|wording"}}
  ],
  "qa": [
    {{"question": "...", "answer": "...", "domain": "..."}}
  ],
  "keywords": ["kw1", "kw2"]
}}

Generá entre 2-5 conceptos y 2-3 QA relevantes para profesionales de reaseguros.
Sé técnico y preciso. Sin relleno.

TEXTO:
{text[:2000]}"""


def build_summary_prompt(full_text: str, source_type: str, title: str) -> str:
    return f"""Resumí este documento de {source_type} de reaseguros: "{title}"

Respondé SOLO con JSON:
{{
  "executive": "resumen ejecutivo en 3-4 oraciones",
  "key_points": ["punto 1", "punto 2", "punto 3"],
  "operational": "implicancia operativa principal",
  "risks": "alertas o ambigüedades relevantes"
}}

TEXTO:
{full_text[:4000]}"""


if __name__ == "__main__":
    import os
    os.environ["DB_PATH"] = "/opt/cukinator/memory.db"
    init_reinsurance_kb()
    stats = get_kb_stats()
    print("Reinsurance KB OK:", stats)
