"""
config_store.py — Sistema de configuración persistente en Railway DB.
Fuente de verdad para todas las configs operativas del bot.
"""

import sqlite3
import json
import datetime
import os

DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")


# ── Schema ──────────────────────────────────────────────────────────────────────
def init_config_store(db_path: str = None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS configurations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace   TEXT    NOT NULL,
            key         TEXT    NOT NULL,
            version     INTEGER NOT NULL DEFAULT 1,
            value_text  TEXT,
            value_json  TEXT,
            description TEXT,
            tags        TEXT,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_config_ns_key ON configurations(namespace, key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_config_active ON configurations(namespace, key, is_active)")
    con.commit()
    con.close()


# ── CRUD ────────────────────────────────────────────────────────────────────────
def save_config(namespace: str, key: str, value, description: str = "", tags: str = "", db_path: str = None) -> dict:
    """
    Guarda una configuración con versionado automático.
    Desactiva la versión anterior y crea una nueva activa.
    """
    path = db_path or DB_PATH
    con = sqlite3.connect(path)

    # Obtener versión actual
    row = con.execute(
        "SELECT MAX(version) FROM configurations WHERE namespace=? AND key=?",
        (namespace, key)
    ).fetchone()
    next_version = (row[0] or 0) + 1

    # Desactivar versiones anteriores
    con.execute(
        "UPDATE configurations SET is_active=0 WHERE namespace=? AND key=?",
        (namespace, key)
    )

    # Determinar tipo de valor
    if isinstance(value, (dict, list)):
        value_json = json.dumps(value, ensure_ascii=False, indent=2)
        value_text = None
    else:
        value_text = str(value)
        value_json = None

    # Insertar nueva versión
    con.execute("""
        INSERT INTO configurations (namespace, key, version, value_text, value_json, description, tags, is_active, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
    """, (namespace, key, next_version, value_text, value_json, description, tags,
          datetime.datetime.utcnow().isoformat()))

    con.commit()
    con.close()

    return {"namespace": namespace, "key": key, "version": next_version}


def get_config(namespace: str, key: str, db_path: str = None):
    """Retorna el valor activo de una configuración."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT value_text, value_json, version FROM configurations WHERE namespace=? AND key=? AND is_active=1",
        (namespace, key)
    ).fetchone()
    con.close()
    if not row:
        return None
    value_text, value_json, version = row
    if value_json:
        return json.loads(value_json)
    return value_text


def get_config_meta(namespace: str, key: str, db_path: str = None) -> dict:
    """Retorna metadatos + valor de la config activa."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT namespace, key, version, value_text, value_json, description, tags, updated_at FROM configurations WHERE namespace=? AND key=? AND is_active=1",
        (namespace, key)
    ).fetchone()
    con.close()
    if not row:
        return {}
    ns, k, ver, vt, vj, desc, tags, updated = row
    value = json.loads(vj) if vj else vt
    return {"namespace": ns, "key": k, "version": ver, "value": value, "description": desc, "tags": tags, "updated_at": updated}


def list_configs(namespace: str = None, db_path: str = None) -> list:
    """Lista todas las configs activas, opcionalmente filtradas por namespace."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    if namespace:
        rows = con.execute(
            "SELECT namespace, key, version, description, tags, updated_at FROM configurations WHERE is_active=1 AND namespace=? ORDER BY namespace, key",
            (namespace,)
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT namespace, key, version, description, tags, updated_at FROM configurations WHERE is_active=1 ORDER BY namespace, key"
        ).fetchall()
    con.close()
    return [{"namespace": r[0], "key": r[1], "version": r[2], "description": r[3], "tags": r[4], "updated_at": r[5]} for r in rows]


def get_version_history(namespace: str, key: str, db_path: str = None) -> list:
    """Historial completo de versiones de una configuración."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    rows = con.execute(
        "SELECT version, is_active, description, updated_at FROM configurations WHERE namespace=? AND key=? ORDER BY version DESC",
        (namespace, key)
    ).fetchall()
    con.close()
    return [{"version": r[0], "is_active": bool(r[1]), "description": r[2], "updated_at": r[3]} for r in rows]


def restore_version(namespace: str, key: str, version: int, db_path: str = None) -> bool:
    """Restaura una versión anterior como activa."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    exists = con.execute(
        "SELECT id FROM configurations WHERE namespace=? AND key=? AND version=?",
        (namespace, key, version)
    ).fetchone()
    if not exists:
        con.close()
        return False
    con.execute("UPDATE configurations SET is_active=0 WHERE namespace=? AND key=?", (namespace, key))
    con.execute(
        "UPDATE configurations SET is_active=1, updated_at=? WHERE namespace=? AND key=? AND version=?",
        (datetime.datetime.utcnow().isoformat(), namespace, key, version)
    )
    con.commit()
    con.close()
    return True


# ── Carga de configuraciones activas ────────────────────────────────────────────
def load_all_active(db_path: str = None) -> dict:
    """Carga todas las configuraciones activas en un dict namespace.key -> valor."""
    path = db_path or DB_PATH
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT namespace, key, value_text, value_json FROM configurations WHERE is_active=1"
        ).fetchall()
    except Exception:
        rows = []
    con.close()
    result = {}
    for ns, key, vt, vj in rows:
        result[f"{ns}.{key}"] = json.loads(vj) if vj else vt
    return result


# ── Seed de configuraciones iniciales ───────────────────────────────────────────
INITIAL_CONFIGS = [
    {
        "namespace": "astrology",
        "key": "house_system",
        "value": "P",
        "description": "Sistema de casas por defecto (P=Placidus)",
        "tags": "technical,swe",
    },
    {
        "namespace": "astrology",
        "key": "zodiac",
        "value": "tropical",
        "description": "Tipo de zodiaco por defecto",
        "tags": "technical,swe",
    },
    {
        "namespace": "astrology",
        "key": "swe_flags",
        "value": {"flags": 260, "description": "SEFLG_MOSEPH | SEFLG_SPEED", "moseph": True, "speed": True},
        "description": "Flags de Swiss Ephemeris",
        "tags": "technical,swe",
    },
    {
        "namespace": "astrology",
        "key": "orb_rules",
        "value": {
            "default_max": 5.0,
            "by_aspect": {
                "Conjuncion": 8.0,
                "Oposicion": 8.0,
                "Trigono": 8.0,
                "Cuadratura": 7.0,
                "Sextil": 5.0,
            },
            "luminaries_bonus": 2.0,
            "note": "Orbes variables segun planeta y aspecto. Luminares tienen orbe ampliado.",
        },
        "description": "Criterios de orbes para aspectos mayores",
        "tags": "astrology,aspects",
    },
    {
        "namespace": "astrology",
        "key": "plenivalent_rules",
        "value": {
            "enabled": True,
            "by_element": True,
            "extra_orb": 3.0,
            "require_structural_coherence": False,
            "aspects_included": ["Conjuncion", "Oposicion", "Trigono", "Cuadratura", "Sextil"],
            "note": "Aspectos por elemento con orbe ampliado hasta 8 grados",
        },
        "description": "Reglas para detección de aspectos plenivalentes",
        "tags": "astrology,aspects",
    },
    {
        "namespace": "astrology",
        "key": "interceptions_rules",
        "value": {
            "detect_intercepted_houses": True,
            "detect_intercepted_signs": True,
            "definition_intercepted_house": "Casa cuya cuspide inicial y final caen en el mismo signo",
            "definition_intercepted_sign": "Signo que no aparece en ninguna cuspide de casa",
            "note": "NO confundir casa interceptada (contiene un signo) con signo interceptado (no esta en cuspide)",
        },
        "description": "Criterios para detección de intercepciones",
        "tags": "astrology,interceptions",
    },
    {
        "namespace": "astrology",
        "key": "dignities_rules",
        "value": {
            "categories": ["Domicilio", "Exaltacion", "Exilio", "Caida", "Peregrino"],
            "system": "traditional_modern_hybrid",
            "outer_planets_included": True,
            "note": "Sin interpretacion. Solo clasificacion tecnica.",
        },
        "description": "Sistema de dignidades y debilidades planetarias",
        "tags": "astrology,dignities",
    },
    {
        "namespace": "astrology",
        "key": "ficha_tecnica_sections",
        "value": [
            "0_base_limpia",
            "1_aspectos_mayores",
            "2_regentes_cadena_dispositora",
            "3_dignidades_debilidades",
            "4_jerarquias_centros_gravitacionales",
            "5_redes_aspectos",
            "6_vectores_energeticos",
            "7_intercepciones",
            "8_validacion_tecnica",
        ],
        "description": "Estructura obligatoria de la ficha técnica astrológica",
        "tags": "astrology,template,output",
    },
    {
        "namespace": "telegram",
        "key": "menu_trigger_phrases",
        "value": [
            "mostrame la lista de cartas",
            "lista de cartas",
            "ver cartas",
            "abri el menu",
            "menu",
            "/cartas",
            "/menu",
        ],
        "description": "Frases que activan el menú interactivo de cartas",
        "tags": "telegram,menu,ux",
    },
    {
        "namespace": "telegram",
        "key": "menu_carta_actions",
        "value": [
            {"label": "Ficha natal", "callback": "astro:natal:{nombre}"},
            {"label": "Ficha tecnica completa", "callback": "astro:ficha:{nombre}"},
            {"label": "Transitos actuales", "callback": "astro:transitos:{nombre}"},
            {"label": "Volver", "callback": "astro:list"},
        ],
        "description": "Acciones disponibles en el menú de carta por persona",
        "tags": "telegram,menu,ux",
    },
    {
        "namespace": "prompts",
        "key": "system_behavior",
        "value": {
            "style": "relajado, canchero, zona norte Buenos Aires, Big Lebowski",
            "language": "español argentino porteño",
            "max_lines": 5,
            "no_emojis": True,
            "no_verbose": True,
            "menu_policy": "solo mostrar botones si el usuario lo pide explicitamente",
            "conversation_always_flows": True,
        },
        "description": "Comportamiento conversacional del bot",
        "tags": "prompts,behavior",
    },
    {
        "namespace": "technical",
        "key": "swe_engine_version",
        "value": {
            "module": "swiss_engine.py",
            "houses_function": "swe.houses_ex()",
            "coordinate_convention": "Este=+, Oeste=-, Norte=+, Sur=-",
            "all_calcs_in_ut": True,
            "debug_mode_available": True,
        },
        "description": "Configuración del motor Swiss Ephemeris",
        "tags": "technical,swe",
    },
]


def seed_initial_configs(db_path: str = None, overwrite: bool = False):
    """Carga las configuraciones iniciales si no existen."""
    path = db_path or DB_PATH
    existing = {f"{c['namespace']}.{c['key']}" for c in list_configs(db_path=path)}
    seeded = []
    for cfg in INITIAL_CONFIGS:
        full_key = f"{cfg['namespace']}.{cfg['key']}"
        if full_key not in existing or overwrite:
            save_config(
                cfg["namespace"], cfg["key"], cfg["value"],
                cfg.get("description", ""), cfg.get("tags", ""),
                db_path=path
            )
            seeded.append(full_key)
    return seeded


if __name__ == "__main__":
    init_config_store()
    seeded = seed_initial_configs()
    print(f"Inicializado. Configs sembradas: {len(seeded)}")
    for c in list_configs():
        print(f"  {c['namespace']}.{c['key']} v{c['version']} — {c['description'][:50]}")
