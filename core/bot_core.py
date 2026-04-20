import sqlite3
import logging
import json
import datetime
import sys
import os
import time

import os
import tempfile
import swisseph as swe
import anthropic
try:
    from agents.intent_router import classify as _classify, select_model as _select_model
except Exception:
    def _classify(t): return "conversational"
    def _select_model(t, i=None): return "claude-opus-4-5"
from ddgs import DDGS
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters, CallbackQueryHandler
from modules.swiss_engine import calc_carta_completa, formatear_ficha, verificar_carta, calc_houses, assign_planet_house, formatear_ficha_tecnica, calc_dignidad, calc_estado_dinamico, calc_regentes, calc_intercepciones, calc_jerarquias
from services.config_store import init_config_store, seed_initial_configs, save_config, get_config, get_config_meta, list_configs, load_all_active
from services.memory_store import (init_memory_store, save_message_full, get_history_full,
    get_sessions, search_memory, search_person_memory, save_memory_fact,
    upsert_person_memory, get_memory_stats, needs_summary, clear_chat_history)
from modules.reinsurance_kb import (init_reinsurance_kb, search_knowledge, get_document_list,
    get_kb_stats, create_document, add_chunk, add_concept, add_summary, add_qa,
    chunk_text, build_enrichment_prompt, build_summary_prompt, is_reinsurance_context,
    detect_domain)
from services.agent_ops import (init_agent_ops, log_change, get_changelog, get_agent_status,
    store_secret, list_secrets, register_skill, list_skills, classify_intent)

# ── Configuración ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_KEY"]
_db_default = "/data/memory.db"
try:
    os.makedirs("/data", exist_ok=True)
except Exception:
    _db_default = "/tmp/memory.db"
DB_PATH        = os.environ.get("DB_PATH", _db_default)
SYSTEM_CONFIG  = {}  # se carga desde DB al arrancar
EPHE_PATH      = os.environ.get("EPHE_PATH", "/app/ephe")
PDF_PATH       = os.environ.get("PDF_PATH",  "/tmp/carta.pdf")
MAX_HISTORY    = 20
# Capa 3 reducción tokens: límite de historial por intent. Más historial donde
# importa contexto (charla, personal, astro), menos donde cada query es self-
# contained (coding va al worker, research es one-shot).
MAX_HISTORY_BY_INTENT = {
    "conversational": 14,
    "personal":       20,
    "astrology":      14,
    "reinsurance":    10,
    "research":        6,
    "coding":          4,
}
GAS_URL        = os.environ["GAS_URL"]

swe.set_ephe_path(EPHE_PATH)


logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ── Motor principal de carta natal ────────────────────────────────────────────
def calcular_carta(fecha: str, hora: str, lugar: str) -> dict:
    """
    fecha: 'DD/MM/AAAA'
    hora:  'HH:MM'
    lugar: nombre de ciudad
    Delega en swiss_engine.calc_carta_completa().
    """
    return calc_carta_completa(fecha, hora, lugar)

def formatear_carta(carta: dict) -> str:
    return formatear_ficha(carta)

# ── Generador de PDF ───────────────────────────────────────────────────────────
def _find_font(candidates: list) -> str:
    """Retorna el primer path de font que existe."""
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

FONT_REGULAR = _find_font([
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/DejaVuSansMono.ttf",
])
FONT_BOLD = _find_font([
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/DejaVuSansMono-Bold.ttf",
])

def generar_pdf(carta: dict, ficha_tecnica: bool = False) -> str:
    if not FONT_REGULAR or not FONT_BOLD:
        raise RuntimeError("Fonts DejaVu no encontrados en el sistema. Instalar fonts-dejavu-mono.")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.add_font("Mono",  "",  FONT_REGULAR)
    pdf.add_font("Mono",  "B", FONT_BOLD)

    NL = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}

    def fila(txt, bold=False):
        pdf.set_font("Mono", "B" if bold else "", 8)
        # Limpiar caracteres problemáticos para PDF
        txt_clean = txt.encode('latin-1', 'replace').decode('latin-1')
        pdf.cell(0, 5, txt_clean, **NL)

    if ficha_tecnica:
        # PDF con ficha técnica completa (8 secciones)
        from modules import swiss_engine as _e
        for nombre_p, pd in carta.get("planetas", {}).items():
            if "error" not in pd:
                pd["dignidad"]        = _e.calc_dignidad(nombre_p, pd.get("signo", ""))
                pd["estado_dinamico"] = _e.calc_estado_dinamico(pd.get("speed", 0), nombre_p)
        carta["regentes"]       = _e.calc_regentes(carta.get("planetas", {}), carta.get("casas", {}))
        carta["intercepciones"] = _e.calc_intercepciones([c["lon"] for c in carta.get("casas", {}).get("cuspides", [])])
        carta["jerarquias"]     = _e.calc_jerarquias(carta.get("planetas", {}), carta.get("aspectos", []))
        contenido = _e.formatear_ficha_tecnica(carta)
        titulo = "FICHA TECNICA COMPLETA - CARTA NATAL"
    else:
        contenido = None
        titulo = "CARTA NATAL - FICHA TECNICA ESTRUCTURAL"

    pdf.set_font("Mono", "B", 13)
    pdf.cell(0, 8, titulo, align="C", **NL)
    fila("─" * 95)

    d = carta["debug"]
    fila(f"Fecha : {d['fecha_original']}   {d['hora_ut']}")
    fila(f"Lugar : {d['lugar_geocodificado'][:80]}")
    fila(f"Coords: {d['lat']}N  {d['lon']}E   TZ: {d['timezone']}   JD: {d['jd_ut']}")
    fila("─" * 95)

    if ficha_tecnica and contenido:
        # Volcar el contenido de la ficha completa línea a línea
        for line in contenido.split("\n"):
            bold = line.startswith("##") or line.startswith("###")
            fila(line, bold=bold)
    else:
        # Ficha simple: planetas, ángulos, cúspides, aspectos
        fila("POSICIONES PLANETARIAS", bold=True)
        fila(f"  {'Planeta':<14} {'Posicion':<24} {'Casa':<10} {'R'}")
        fila(f"  {'─'*14} {'─'*24} {'─'*10} {'─'}")
        for nombre, pd in carta["planetas"].items():
            if "error" in pd:
                fila(f"  {nombre:<14} [error]")
                continue
            r    = "R" if pd["retrogrado"] else " "
            casa = f"Casa {pd['casa']}"
            fila(f"  {nombre:<14} {pd['signo']:<24} {casa:<10} {r}")

        fila("─" * 95)
        fila("ANGULOS", bold=True)
        c = carta["casas"]
        fila(f"  ASC: {c['asc']['signo']}   MC: {c['mc']['signo']}   DSC: {c['dc']['signo']}   IC: {c['ic']['signo']}")

        fila("─" * 95)
        fila("CUSPIDES DE CASAS (Placidus)", bold=True)
        casas = carta["casas"]["cuspides"]
        for i in range(0, 12, 3):
            grupo = casas[i:i+3]
            txt = "   ".join([f"Casa {c['numero']:2d}: {c['signo']:<22}" for c in grupo])
            fila("  " + txt)

        fila("─" * 95)
        fila("ASPECTOS MAYORES", bold=True)
        if carta["aspectos"]:
            fila(f"  {'Planeta 1':<14} {'Aspecto':<16} {'Planeta 2':<14} {'Orbe'}")
            for a in carta["aspectos"]:
                fila(f"  {a['planeta1']:<14} {a['aspecto']:<16} {a['planeta2']:<14} {a['orb']} grados")

    pdf.output(PDF_PATH)
    return PDF_PATH

# ── Base de datos ──────────────────────────────────────────────────────────────
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS perfiles_astro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            lugar TEXT NOT NULL,
            carta_json TEXT NOT NULL,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, nombre)
        )
    """)
    con.commit()
    con.close()
    init_config_store(DB_PATH)
    seed_initial_configs(db_path=DB_PATH)
    init_memory_store(DB_PATH)
    init_reinsurance_kb(DB_PATH)
    init_agent_ops(DB_PATH)

def _astro_use_pg() -> bool:
    """True si hay Postgres + tabla personal.astro_profiles disponible."""
    try:
        from services.db import pg_available
        return pg_available()
    except Exception:
        return False


def astro_guardar(chat_id: int, nombre: str, fecha: str, hora: str, lugar: str, carta: dict) -> str:
    """Guarda/actualiza un perfil astrológico. Postgres (personal.astro_profiles)
    si está disponible, sino SQLite local como fallback transitorio."""
    import json
    nombre_n = nombre.strip().lower()
    if _astro_use_pg():
        try:
            from services.db import pg_conn
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute("""
                        INSERT INTO personal.astro_profiles (chat_id, nombre, fecha, hora, lugar, carta_json, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s::jsonb, NOW())
                        ON CONFLICT (chat_id, nombre) DO UPDATE SET
                            fecha=EXCLUDED.fecha, hora=EXCLUDED.hora, lugar=EXCLUDED.lugar,
                            carta_json=EXCLUDED.carta_json, updated_at=NOW()
                    """, (chat_id, nombre_n, fecha, hora, lugar, json.dumps(carta)))
            return f"Carta de {nombre} guardada."
        except Exception as _e:
            log.warning(f"astro_guardar pg fallback SQLite: {_e}")
    # Fallback SQLite
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO perfiles_astro (chat_id, nombre, fecha, hora, lugar, carta_json)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(chat_id, nombre) DO UPDATE SET
            fecha=excluded.fecha, hora=excluded.hora,
            lugar=excluded.lugar, carta_json=excluded.carta_json,
            ts=CURRENT_TIMESTAMP
    """, (chat_id, nombre_n, fecha, hora, lugar, json.dumps(carta)))
    con.commit()
    con.close()
    return f"Carta de {nombre} guardada."


def astro_recuperar(chat_id: int, nombre: str) -> dict | None:
    import json
    nombre_n = nombre.strip().lower()
    if _astro_use_pg():
        try:
            from services.db import pg_conn
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "SELECT fecha, hora, lugar, carta_json FROM personal.astro_profiles WHERE chat_id=%s AND nombre=%s",
                        (chat_id, nombre_n),
                    )
                    row = cur.fetchone()
            if row:
                carta = row[3] if isinstance(row[3], dict) else json.loads(row[3])
                return {"fecha": row[0], "hora": row[1], "lugar": row[2], "carta": carta}
        except Exception as _e:
            log.warning(f"astro_recuperar pg fallback SQLite: {_e}")
    # Fallback SQLite
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT fecha, hora, lugar, carta_json FROM perfiles_astro WHERE chat_id=? AND nombre=?",
        (chat_id, nombre_n)
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"fecha": row[0], "hora": row[1], "lugar": row[2], "carta": json.loads(row[3])}


def astro_listar(chat_id: int) -> list:
    nombre_fields = ("nombre", "fecha", "hora", "lugar")
    if _astro_use_pg():
        try:
            from services.db import pg_conn
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute("""
                        SELECT nombre, fecha, hora, lugar, updated_at::date::text
                        FROM personal.astro_profiles WHERE chat_id=%s ORDER BY nombre
                    """, (chat_id,))
                    rows = cur.fetchall()
            return [{"nombre": r[0], "fecha": r[1], "hora": r[2], "lugar": r[3], "guardado": r[4]} for r in rows]
        except Exception as _e:
            log.warning(f"astro_listar pg fallback SQLite: {_e}")
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT nombre, fecha, hora, lugar, ts FROM perfiles_astro WHERE chat_id=? ORDER BY nombre",
        (chat_id,)
    ).fetchall()
    con.close()
    return [{"nombre": r[0], "fecha": r[1], "hora": r[2], "lugar": r[3], "guardado": r[4][:10]} for r in rows]


def astro_eliminar(chat_id: int, nombre: str) -> str:
    nombre_n = nombre.strip().lower()
    if _astro_use_pg():
        try:
            from services.db import pg_conn
            with pg_conn() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "DELETE FROM personal.astro_profiles WHERE chat_id=%s AND nombre=%s",
                        (chat_id, nombre_n),
                    )
                    deleted = cur.rowcount
            if deleted:
                return f"Perfil de {nombre} eliminado."
        except Exception as _e:
            log.warning(f"astro_eliminar pg fallback SQLite: {_e}")
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "DELETE FROM perfiles_astro WHERE chat_id=? AND nombre=?",
        (chat_id, nombre_n)
    )
    con.commit()
    con.close()
    return f"Perfil de {nombre} eliminado." if cur.rowcount else f"No encontré perfil de {nombre}."


def save_message(chat_id, role, content):
    con = sqlite3.connect(DB_PATH)
    con.execute("INSERT INTO messages (chat_id, role, content) VALUES (?,?,?)", (chat_id, role, content))
    con.commit()
    con.close()

def get_history(chat_id):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, MAX_HISTORY)
    ).fetchall()
    con.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def clear_history(chat_id):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

# ── Google (Gmail + Calendar via Apps Script relay) ───────────────────────────
import urllib3 as _urllib3

# ── Skill: Clima (OpenWeatherMap) ──────────────────────────────────────────────
import httpx as _httpx

async def get_weather(location: str = "Buenos Aires") -> dict:
    """Obtiene clima actual de una ubicación via OpenWeatherMap."""
    api_key = os.environ.get("WEATHER_API_KEY", "6fc4ecceb823f299b4115a9f414c9fc7")
    if not api_key:
        return {"error": "WEATHER_API_KEY no configurada"}
    url = (f"https://api.openweathermap.org/data/2.5/weather"
           f"?q={location}&appid={api_key}&units=metric&lang=es")
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
        if resp.status_code != 200:
            return {"error": f"No encontré clima para '{location}': {data.get('message','')}"}
        return {
            "ubicacion":    data["name"],
            "pais":         data.get("sys",{}).get("country",""),
            "temperatura":  round(data["main"]["temp"]),
            "sensacion":    round(data["main"]["feels_like"]),
            "humedad":      data["main"]["humidity"],
            "condicion":    data["weather"][0]["description"],
            "viento_kmh":   round(data["wind"]["speed"] * 3.6),
        }
    except Exception as e:
        return {"error": str(e)}


# ── Skill: GitHub Push via API ─────────────────────────────────────────────────
async def github_push(repo: str, path: str, content: str,
                      message: str, branch: str = "main") -> dict:
    """Crea o actualiza un archivo en GitHub via API."""
    import base64 as _b64
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"error": "GITHUB_TOKEN no configurado."}
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            existing = await client.get(url, headers=headers, params={"ref": branch})
            sha = existing.json().get("sha") if existing.status_code == 200 else None
            # Si no existe en bot-changes, buscar en main para tomar el SHA base
            if not sha and branch != "main":
                existing_main = await client.get(url, headers=headers, params={"ref": "main"})
                sha = existing_main.json().get("sha") if existing_main.status_code == 200 else None
            payload = {
                "message": message,
                "content": _b64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if sha:
                payload["sha"] = sha
            resp = await client.put(url, headers=headers, json=payload)
        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "ok":     True,
                "action": "updated" if sha else "created",
                "path":   path,
                "sha":    data["content"]["sha"][:7],
                "url":    data["content"]["html_url"],
                "branch": branch,
            }
        return {"error": resp.text[:200], "status": resp.status_code}
    except Exception as e:
        return {"error": str(e)}


async def github_create_pr(repo: str, title: str, body: str,
                           head: str = "main", base: str = "main") -> dict:
    """Crea un Pull Request en GitHub para que el humano revise y apruebe."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"error": "GITHUB_TOKEN no configurado."}
    url = f"https://api.github.com/repos/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            })
        if resp.status_code == 201:
            data = resp.json()
            return {
                "ok":     True,
                "pr_number": data["number"],
                "url":    data["html_url"],
                "title":  data["title"],
            }
        # Si ya existe un PR abierto
        if resp.status_code == 422:
            return {"ok": False, "error": "Ya hay un PR abierto para bot-changes. Mergealo o cerralo primero.", "status": 422}
        return {"error": resp.text[:200], "status": resp.status_code}
    except Exception as e:
        return {"error": str(e)}
_CITY_TZ = {
    "buenos aires":"America/Argentina/Buenos_Aires","cordoba":"America/Argentina/Cordoba",
    "córdoba":"America/Argentina/Cordoba","rosario":"America/Argentina/Cordoba",
    "mendoza":"America/Argentina/Mendoza","salta":"America/Argentina/Salta",
    "santiago":"America/Santiago","lima":"America/Lima",
    "bogota":"America/Bogota","bogotá":"America/Bogota",
    "mexico":"America/Mexico_City","ciudad de mexico":"America/Mexico_City",
    "sao paulo":"America/Sao_Paulo","são paulo":"America/Sao_Paulo",
    "rio":"America/Sao_Paulo","montevideo":"America/Montevideo",
    "caracas":"America/Caracas","la paz":"America/La_Paz",
    "madrid":"Europe/Madrid","barcelona":"Europe/Madrid",
    "london":"Europe/London","londres":"Europe/London",
    "paris":"Europe/Paris","parís":"Europe/Paris",
    "berlin":"Europe/Berlin","berlín":"Europe/Berlin",
    "roma":"Europe/Rome","amsterdam":"Europe/Amsterdam","zurich":"Europe/Zurich",
    "tokyo":"Asia/Tokyo","shanghai":"Asia/Shanghai","beijing":"Asia/Shanghai",
    "dubai":"Asia/Dubai","singapur":"Asia/Singapore",
    "hong kong":"Asia/Hong_Kong","mumbai":"Asia/Kolkata","delhi":"Asia/Kolkata",
    "sydney":"Australia/Sydney","melbourne":"Australia/Melbourne",
    "new york":"America/New_York","nueva york":"America/New_York",
    "miami":"America/New_York","chicago":"America/Chicago",
    "los angeles":"America/Los_Angeles","toronto":"America/Toronto",
}

DIAS_ES = ["lunes","martes","miércoles","jueves","viernes","sábado","domingo"]

async def get_time(timezone: str = "America/Argentina/Buenos_Aires") -> dict:
    """Obtiene la hora actual via WorldTimeAPI."""
    url = f"http://worldtimeapi.org/api/timezone/{timezone}"
    try:
        async with _httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return {"error": f"No pude obtener la hora para {timezone}"}
        data = resp.json()
        dow = int(data.get("day_of_week", 0))  # 0=domingo en worldtimeapi
        dia = DIAS_ES[dow - 1] if dow > 0 else DIAS_ES[6]
        return {
            "hora":      data["datetime"][11:16],
            "fecha":     f"{dia} {data['datetime'][:10]}",
            "timezone":  data["timezone"],
            "utc_offset": data.get("utc_offset",""),
        }
    except Exception as e:
        return {"error": str(e)}

def city_to_timezone(city: str) -> str:
    """Convierte nombre de ciudad a timezone string."""
    tz = _CITY_TZ.get(city.lower().strip())
    if tz:
        return tz
    # Fallback: geopy+timezonefinder
    try:
        from geopy.geocoders import Nominatim
        from timezonefinder import TimezoneFinder
        loc = Nominatim(user_agent="cuki_time").geocode(city, language="es", timeout=5)
        if loc:
            return TimezoneFinder().timezone_at(lat=loc.latitude, lng=loc.longitude)
    except Exception:
        pass
    return None




# ── Text-to-Speech ─────────────────────────────────────────────────────────────
VOICE_MAX_CHARS  = 500
ELEVENLABS_KEY   = os.environ.get("ELEVENLABS_KEY", "sk_070b39dacb714d3194f831b3de3849ffab5c0e1f73821366")
ELEVENLABS_VOICE = os.environ.get("ELEVENLABS_VOICE", "SHcpmnTftylBb6nJGEXY")  # COCOBASILE

# Catálogo de voces disponibles
VOCES_CATALOG = [
    ("SHcpmnTftylBb6nJGEXY", "⭐ COCOBASILE (tu voz)", "tu"),
    ("nPczCjzI2devNBz1zQrb", "Brian — profunda, resonante", "en"),
    ("TX3LPaxmHKxFdv7VOQHJ", "Liam — energica, social", "en"),
    ("IKne3meq5aSn9XLyUdCD", "Charlie — segura, profunda", "en"),
    ("EXAVITQu4vr4xnSDxMaL", "Sarah — madura, cálida", "en"),
    ("FGY2WhTYpPnrIDTdsKH5", "Laura — entusiasta", "en"),
]

# Voz activa (se puede cambiar en runtime sin reiniciar)
_voz_activa = ELEVENLABS_VOICE

def get_voz_activa() -> str:
    return _voz_activa

def set_voz_activa(voice_id: str, db_path: str = None):
    global _voz_activa
    _voz_activa = voice_id
    try:
        save_config("tts", "elevenlabs_voice_id", voice_id, db_path=db_path or DB_PATH)
    except Exception:
        pass
def texto_a_voz(texto: str, lang: str = "es") -> str | None:
    """Convierte texto a OGG/OPUS. Usa ElevenLabs (COCOBASILE) con fallback a espeak-ng."""
    import tempfile, subprocess, re
    clean = re.sub(r'[*_`#\|─╔╚╗╝═☌☍□△⚹]', '', texto)
    clean = re.sub(r'\s+', ' ', clean).strip()
    if not clean:
        return None
    # Pronunciación correcta del nombre del bot
    clean = re.sub(r'(?i)cukinator', 'Cuki', clean)
    log.info(f"TTS: '{clean[:60]}'")

    # Intentar ElevenLabs primero
    try:
        import requests as _req
        r = _req.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{get_voz_activa()}",
            headers={"xi-api-key": ELEVENLABS_KEY, "Content-Type": "application/json"},
            json={
                "text": clean[:VOICE_MAX_CHARS],
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.38,
                    "similarity_boost": 0.92,
                    "style": 0.4,
                    "use_speaker_boost": True
                },
                "speed": 1.2
            },
            timeout=15
        )
        if r.status_code == 200 and len(r.content) > 1000:
            mp3 = tempfile.mktemp(suffix=".mp3")
            ogg = tempfile.mktemp(suffix=".ogg")
            open(mp3, "wb").write(r.content)
            res = subprocess.run(
                ["ffmpeg", "-y", "-i", mp3, "-c:a", "libopus", "-b:a", "64k", ogg],
                capture_output=True
            )
            os.unlink(mp3)
            if res.returncode == 0:
                log.info(f"TTS ElevenLabs OK: {os.path.getsize(ogg)} bytes")
                return ogg
        log.warning(f"TTS ElevenLabs falló: {r.status_code}")
    except Exception as e:
        log.warning(f"TTS ElevenLabs error: {e}")

    # Fallback: espeak-ng (offline)
    try:
        wav = tempfile.mktemp(suffix=".wav")
        ogg = tempfile.mktemp(suffix=".ogg")
        r1 = subprocess.run(
            ["espeak-ng", "-v", "es", "-s", "145", "-w", wav, clean[:VOICE_MAX_CHARS]],
            capture_output=True
        )
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "64k", ogg],
            capture_output=True
        )
        os.unlink(wav)
        if r1.returncode == 0 and r2.returncode == 0:
            log.info(f"TTS espeak-ng fallback OK: {os.path.getsize(ogg)} bytes")
            return ogg
    except Exception as e:
        log.error(f"TTS espeak-ng error: {e}")

    return None

def es_respuesta_larga(texto: str) -> bool:
    """Detecta si la respuesta es técnica/larga y no debería ir como voz."""
    if len(texto) > VOICE_MAX_CHARS:
        return True
    keywords = ["Casa ", "Signo:", "## ", "ASC:", "MC:", "Cuspide", "Aspecto", "Planetas"]
    return any(k in texto for k in keywords)

_urllib3.disable_warnings()

def gas_call(payload: dict, timeout: int = 25) -> dict:
    import requests as _req
    r = _req.post(GAS_URL, json=payload, verify=False, timeout=timeout,
                  headers={"Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()

def gmail_leer(count: int = 10, dias: int = None, query: str = None) -> str:
    payload = {"action": "get_emails", "count": min(count, 30)}
    if query:
        payload["query"] = query
    elif dias:
        payload["dias"] = dias
    data = gas_call(payload)
    if not data.get("ok"):
        return f"Error leyendo emails: {data.get('error')}"
    emails = data.get("emails", [])
    if not emails:
        return "No encontré emails con ese criterio."
    lines = []
    for i, m in enumerate(emails, 1):
        unread = "🔵" if m.get("unread") else "⚪"
        lines.append(f"{unread} [{i}] {m['from']}")
        lines.append(f"     {m['subject']} — {m['date'][:10]}")
        lines.append(f"     {m['snippet'][:180].strip()}...")
        lines.append("")
    return "\n".join(lines)

def gmail_enviar(to: str, subject: str, body: str, adjunto_path: str = None) -> str:
    import base64, os
    payload = {"action": "send_email", "to": to, "subject": subject, "body": body}
    if adjunto_path and os.path.exists(adjunto_path):
        with open(adjunto_path, "rb") as f:
            payload["attachment_b64"]  = base64.b64encode(f.read()).decode("utf-8")
            payload["attachment_name"] = os.path.basename(adjunto_path)
            payload["attachment_mime"] = "application/pdf"
        log.info(f"Adjuntando: {adjunto_path}")
    data = gas_call(payload)
    if not data.get("ok"):
        return f"Error enviando email: {data.get('error')}"
    return f"Email enviado a {to} ✅" + (" (con adjunto)" if adjunto_path else "")

def gmail_ver_email(email_id: str) -> str:
    data = gas_call({"action": "get_email_detail", "email_id": email_id})
    if not data.get("ok"):
        return f"Error abriendo email: {data.get('error')}"
    m = data.get("email", {})
    lines = []
    lines.append(f"De: {m.get('from')}")
    lines.append(f"Para: {m.get('to')}")
    lines.append(f"Asunto: {m.get('subject')}")
    lines.append(f"Fecha: {m.get('date','')[:10]}")
    lines.append("")
    lines.append(m.get("body", ""))
    atts = m.get("attachments", [])
    if atts:
        lines.append("")
        lines.append(f"📎 Adjuntos ({len(atts)}):")
        for a in atts:
            lines.append(f"  [{a['index']}] {a['name']} ({round(a['size']/1024,1)} KB)")
    return "\n".join(lines)

def gmail_descargar_adjunto(email_id: str, attachment_index: int = 0) -> tuple:
    """Retorna (descripcion_texto, nombre_archivo, bytes_contenido)"""
    data = gas_call({"action": "get_attachment", "email_id": email_id,
                     "attachment_index": attachment_index}, timeout=40)
    if not data.get("ok"):
        return (f"Error descargando adjunto: {data.get('error')}", None, None)
    att = data.get("attachment", {})
    import base64
    content = base64.b64decode(att["data_b64"])
    return (f"Adjunto descargado: {att['name']}", att["name"], content)


# ── Outlook corporativo (Microsoft Graph API) ─────────────────────────────────

def outlook_inbox(user: str, tenant: str = "reamerica", days: int = 7,
                  top: int = 20, unread: bool = False) -> str:
    from services.outlook import outlook_inbox as _inbox
    try:
        msgs = _inbox(user=user, days=days, unread=unread, tenant=tenant, top=top)
    except Exception as e:
        return f"Error leyendo Outlook ({tenant}): {e}"
    if not msgs:
        return "No encontré emails con ese criterio."
    lines = []
    for i, m in enumerate(msgs, 1):
        unread_icon = "🔵" if not m.get("isRead") else "⚪"
        sender = m.get("from", {}).get("emailAddress", {})
        fecha = m.get("receivedDateTime", "")[:10]
        lines.append(f"{unread_icon} [{i}] {sender.get('name', sender.get('address', '?'))}")
        lines.append(f"     {m.get('subject', '(sin asunto)')} — {fecha}")
        preview = m.get("bodyPreview", "").replace("\n", " ")[:160]
        lines.append(f"     {preview}...")
        lines.append(f"     id: {m.get('id','')}")
        lines.append("")
    return "\n".join(lines)


def outlook_leer(user: str, message_id: str, tenant: str = "reamerica") -> str:
    from services.outlook import outlook_thread as _thread
    try:
        m = _thread(user=user, message_id=message_id, tenant=tenant)
    except Exception as e:
        return f"Error leyendo mensaje Outlook: {e}"
    sender = m.get("from", {}).get("emailAddress", {})
    to_list = [r.get("emailAddress", {}).get("address", "") for r in m.get("toRecipients", [])]
    body = m.get("body", {})
    content = body.get("content", "")
    if body.get("contentType") == "html":
        import re
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s{2,}", " ", content).strip()
    lines = [
        f"De: {sender.get('name', sender.get('address', '?'))} <{sender.get('address', '')}>",
        f"Para: {', '.join(to_list)}",
        f"Asunto: {m.get('subject', '')}",
        f"Fecha: {m.get('receivedDateTime', '')[:19]}",
        "",
        content[:4000],
    ]
    return "\n".join(lines)


def outlook_buscar(user: str, query: str, tenant: str = "reamerica", top: int = 15) -> str:
    from services.outlook import outlook_search as _search
    try:
        msgs = _search(user=user, query=query, tenant=tenant, top=top)
    except Exception as e:
        return f"Error buscando en Outlook: {e}"
    if not msgs:
        return "No encontré resultados para esa búsqueda."
    lines = []
    for i, m in enumerate(msgs, 1):
        sender = m.get("from", {}).get("emailAddress", {})
        fecha = m.get("receivedDateTime", "")[:10]
        lines.append(f"[{i}] {sender.get('name', sender.get('address', '?'))} — {fecha}")
        lines.append(f"     {m.get('subject', '(sin asunto)')}")
        lines.append(f"     id: {m.get('id','')}")
        lines.append("")
    return "\n".join(lines)


def outlook_enviar(from_user: str, to: list, subject: str, body_html: str,
                   tenant: str = "reamerica", cc: list = None) -> str:
    from services.outlook import outlook_send as _send
    try:
        _send(from_user=from_user, to=to, subject=subject, body_html=body_html,
              cc=cc, tenant=tenant)
        return f"Email enviado desde {from_user} a {', '.join(to)} ✅"
    except Exception as e:
        return f"Error enviando email Outlook: {e}"


def calendar_ver(desde: str = None, hasta: str = None) -> str:
    import datetime
    from zoneinfo import ZoneInfo

    TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")
    DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

    def fmt_dt(iso: str) -> str:
        """Convierte un string ISO8601 UTC a hora argentina con día de semana."""
        try:
            # Normalizar: reemplazar la Z final por +00:00 para fromisoformat
            dt_utc = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
            dt_ar = dt_utc.astimezone(TZ_AR)
            dia = DIAS[dt_ar.weekday()]
            return dt_ar.strftime(f"{dia} %d/%m/%Y %H:%M")
        except Exception:
            return iso  # Si falla, devuelve el original

    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "action": "get_events",
        "from": desde or now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to":   hasta or (now + datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    data = gas_call(payload)
    if not data.get("ok"):
        return f"Error leyendo calendario: {data.get('error')}"
    events = data.get("events", [])
    if not events:
        return "No hay eventos en ese período."
    lines = []
    for ev in events:
        lines.append(f"📅 {ev['title']}")
        lines.append(f"   Inicio: {fmt_dt(ev['start'])}")
        lines.append(f"   Fin:    {fmt_dt(ev['end'])}")
        if ev.get("location"):
            lines.append(f"   Lugar: {ev['location']}")
        if ev.get("description"):
            desc = ev["description"][:150].strip()
            if desc:
                lines.append(f"   Desc:  {desc}")
        lines.append("")
    return "\n".join(lines)

def calendar_crear(title: str, start: str, end: str, description: str = "", location: str = "") -> str:
    data = gas_call({
        "action": "create_event",
        "title": title, "start": start, "end": end,
        "description": description, "location": location,
    })
    if not data.get("ok"):
        return f"Error creando evento: {data.get('error')}"
    return f"Evento '{title}' creado en el calendario ✅"

# ── Búsqueda web ───────────────────────────────────────────────────────────────
def search_web(query: str) -> str:
    log.info(f"🔍 Buscando: {query}")
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5, backend="duckduckgo"))
        if not results:
            return "No encontré resultados."
        return "\n\n".join(
            f"Título: {r.get('title','')}\nResumen: {r.get('body','')}\nURL: {r.get('href','')}"
            for r in results
        )
    except Exception as e:
        return f"Error en búsqueda: {e}"

# ── Tools de Claude ────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "get_time",
        "description": "Obtiene la hora actual de cualquier ciudad o timezone via WorldTimeAPI. Usá cuando pregunten qué hora es, la hora en algún lugar, o la diferencia horaria. Default: Buenos Aires.",
        "input_schema": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "Timezone IANA (ej: America/Argentina/Buenos_Aires, Europe/London) o nombre de ciudad (ej: Tokyo, Madrid). Default: America/Argentina/Buenos_Aires"
                }
            }
        }
    },
    {
        "name": "github_push",
        "description": (
            "Crea o actualiza un archivo en GitHub en la rama main directamente. "
            "Usá para proponer cambios de código: módulos nuevos, scripts, configs. "
            "NUNCA usés para archivos core: bot.py, bot_core.py, handlers/, Dockerfile. "
            "Después de pushear, usá github_pr para crear el Pull Request para revisión. "
            "El repo por default es cuki82/cukinator-bot."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Repo owner/name. Default: cuki82/cukinator-bot"},
                "path":    {"type": "string", "description": "Path del archivo (ej: modules/nuevo_modulo.py)"},
                "content": {"type": "string", "description": "Contenido COMPLETO del archivo"},
                "message": {"type": "string", "description": "Mensaje del commit"},
                "branch":  {"type": "string", "description": "Branch. Default: main"}
            },
            "required": ["path", "content", "message"]
        }
    },
    {
        "name": "github_pr",
        "description": (
            "Crea un Pull Request en GitHub desde bot-changes → main para que el usuario revise y apruebe. "
            "Usá SIEMPRE después de pushear cambios con github_push. "
            "El PR es la forma de proponer cambios al bot para que el humano los apruebe antes del deploy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string", "description": "Repo. Default: cuki82/cukinator-bot"},
                "title": {"type": "string", "description": "Título del PR"},
                "body":  {"type": "string", "description": "Descripción de los cambios propuestos"}
            },
            "required": ["title", "body"]
        }
    },
    {
        "name": "get_weather",
        "description": "Obtiene el clima actual de una ciudad. Usá cuando el usuario pregunte por el clima, temperatura, tiempo atmosférico de cualquier lugar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Ciudad y país (ej: Buenos Aires, London, Tokyo). Default: Buenos Aires"}
            }
        }
    },
    {
        "name": "buscar_reserva",
        "description": (
            "Busca disponibilidad en restaurantes via scraper en el VPS. "
            "Soporta restaurantes que usan Meitre, TheFork y otros sistemas. "
            "Usá cuando el usuario pida buscar disponibilidad, hacer una reserva, "
            "ver horarios disponibles en un restaurante, o consultar si hay lugar. "
            "Ejemplos: 'buscame disponibilidad en Don Julio para mañana para 2', "
            "'hay lugar en La Carnicería el viernes para 4 personas'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "restaurante": {"type": "string", "description": "Nombre del restaurante"},
                "fecha":       {"type": "string", "description": "Fecha en texto (ej: 'mañana', 'viernes', '15/05/2026')"},
                "personas":    {"type": "integer", "description": "Cantidad de personas (default 2)"},
                "hora":        {"type": "string", "description": "Hora preferida (ej: '20:00', 'noche'). Opcional."}
            },
            "required": ["restaurante", "fecha"]
        }
    },
    {
        "name": "buscar_video",
        "description": (
            "Busca y descarga un video de YouTube para enviarlo al usuario. "
            "Usá cuando el usuario pida un video, resumen de partido, clip, highlight o similar. "
            "Descarga en baja resolución (480p max) para respetar el límite de 50MB de Telegram. "
            "Si el video es muy grande, envía el link en su lugar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Búsqueda para YouTube (ej: 'River Plate resumen partido ayer 2026')"},
                "max_duration": {"type": "integer", "description": "Duración máxima en segundos (default 600 = 10 min)"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "sf_broker_performance",
        "description": (
            "Calcula el dashboard COMPLETO de performance de un broker en Salesforce. "
            "Devuelve volumen de pipeline, hit ratio, velocidad de cierre, cuentas únicas, "
            "top clientes, mix por país/industria, pipeline activo, estancadas, distribución mensual "
            "y prima estimada (cruce Account → Contract → IBF). "
            "Usá esta tool en lugar de armar 10 sf_consultar para 'performance de X', 'cómo le va a X', "
            "'comparar X vs Y' (invocá una vez por persona). Mucho más barato y preciso."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "broker": {"type": "string", "description": "Nombre, email o User Id del broker. Ej: 'Ignacio Romanelli', 'tomas.barrabino', '0058Y00000CO6DxQAL'."},
                "year":   {"type": "integer", "description": "OPCIONAL. Año específico (ej. 2025). Sin esto: histórico completo."},
            },
            "required": ["broker"]
        }
    },
    {
        "name": "sf_consultar",
        "description": (
            "Consulta el CRM Salesforce del tenant (REAMERICA UAT por default). "
            "Usá cuando el user pida 'cuántos accounts tengo', 'mostrame los contactos de X', "
            "'qué oportunidades hay en pipeline', 'datos de la cuenta Y', 'opportunities cerradas este mes', etc. "
            "Aceptá SOQL directo si el user lo pasa, o construilo vos a partir de la pregunta. "
            "RESTRICCIÓN: solo SELECT. NO INSERT/UPDATE/DELETE — esos requieren confirmación explícita "
            "y van por otro canal. Si el user pide modificar/borrar, decile que hace falta /sf con confirmación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "soql":   {"type": "string", "description": "Query SOQL completa. Ej: 'SELECT Id,Name,Industry FROM Account WHERE Industry=\\'Insurance\\' LIMIT 20'. Usá comillas simples para strings."},
                "object": {"type": "string", "description": "OPCIONAL. Si pasás 'object' SIN 'soql', describo el sObject (campos, tipos). Ej: 'Account', 'Contact', 'Opportunity', 'Policy__c'."},
                "env":    {"type": "string", "description": "Ambiente: 'uat' (default) o 'prod' cuando esté configurado."},
            }
        }
    },
    {
        "name": "image_gen",
        "description": (
            "Genera una imagen con DALL-E 3 (OpenAI) y la envía al chat. "
            "Usá cuando el user pida 'generá una imagen', 'dibujá', 'creá un logo', "
            "'imagen de X', 'render', 'mockup visual', etc. Devuelve la imagen como adjunto. "
            "NO uses para diseño web/HTML (eso va al designer). Solo para imágenes raster (foto, ilustración, arte)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Prompt en inglés (DALL-E entiende mejor inglés). Sé descriptivo: estilo, composición, iluminación, mood. Ej: 'a minimalist isometric illustration of a modern reinsurance office, blue and gold palette, high detail, vector style'"},
                "size":   {"type": "string", "description": "1024x1024 (default, square) | 1792x1024 (landscape) | 1024x1792 (portrait)"},
                "quality": {"type": "string", "description": "standard (default, $0.04) | hd (mejor detalle, $0.08)"},
                "style":   {"type": "string", "description": "vivid (default, hiperrealista/dramático) | natural (más fotográfico, menos saturado)"},
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "enviar_voz",
        "description": (
            "Envía tu respuesta como mensaje de voz. "
            "SOLO usá esta herramienta cuando: "
            "1) El usuario EXPLÍCITAMENTE pidió una respuesta de voz, audio, o que le hables EN ESE MENSAJE DE TEXTO. "
            "2) El usuario mandó un mensaje de voz (audio) y la respuesta es corta y conversacional. "
            "NO la uses si el usuario mandó texto sin pedir voz. "
            "NO la uses para fichas técnicas, cartas natales, tablas o textos largos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "El texto que querés que se convierta a voz. Máximo 400 caracteres, sin símbolos técnicos."}
            },
            "required": ["texto"]
        }
    },
    {
        "name": "search_web",
        "description": "Busca información actualizada en internet sobre noticias, precios, eventos actuales, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Consulta de búsqueda"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "calcular_carta_natal",
        "description": (
            "Calcula la carta natal astrológica completa usando Swiss Ephemeris. "
            "Devuelve posiciones planetarias, cúspides de casas, ángulos y aspectos mayores. "
            "Usá esta herramienta cuando el usuario proporcione fecha, hora y lugar de nacimiento. "
            "Si el usuario pide el PDF además del cálculo, ponés generar_pdf=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha":       {"type": "string", "description": "Fecha de nacimiento en formato DD/MM/AAAA"},
                "hora":        {"type": "string", "description": "Hora de nacimiento en formato HH:MM (hora local)"},
                "lugar":       {"type": "string", "description": "Ciudad y país de nacimiento"},
                "generar_pdf":   {"type": "boolean", "description": "Si se debe generar un PDF con la ficha. Default false."},
                "ficha_tecnica": {"type": "boolean", "description": "Si es true, devuelve la ficha técnica completa con secciones 0-8 (dignidades, estados, regentes, intercepciones, jerarquías). Default false."}
            },
            "required": ["fecha", "hora", "lugar"]
        }
    },
    {
        "name": "astro_guardar_perfil",
        "description": "Guarda o actualiza la carta natal de una persona en la base de datos. Usá cuando el usuario pida guardar, memorizar o asignar una carta a alguien. Necesitás los datos de nacimiento y la carta ya calculada.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la persona"},
                "fecha":  {"type": "string", "description": "Fecha de nacimiento DD/MM/AAAA"},
                "hora":   {"type": "string", "description": "Hora de nacimiento HH:MM"},
                "lugar":  {"type": "string", "description": "Ciudad y país de nacimiento"}
            },
            "required": ["nombre", "fecha", "hora", "lugar"]
        }
    },
    {
        "name": "astro_ver_perfil",
        "description": "Recupera y muestra la carta natal guardada de una persona. Usá cuando el usuario pida ver, mostrar o consultar la carta de alguien.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la persona"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "calcular_transitos",
        "description": (
            "Calcula los tránsitos astrológicos actuales sobre una capa (natal / solar / lunar). "
            "target='natal' (default) usa la carta natal. target='solar' usa el retorno solar del "
            "año vigente (requiere que haya uno calculado o calcula uno con lugar_retorno si se "
            "provee). target='lunar' igual para el retorno lunar del mes. Retorna aspectos "
            "priorizados por significancia. Usá cuando el user pida 'qué tránsitos tengo', "
            "'qué está activo en mi solar', 'tránsitos sobre lunar', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la persona con carta natal guardada"},
                "target": {"type": "string", "description": "natal | solar | lunar — sobre qué carta calcular tránsitos (default natal)"},
                "fecha":  {"type": "string", "description": "DD/MM/AAAA — opcional, default hoy (UTC)"},
                "hora":   {"type": "string", "description": "HH:MM UTC — opcional"},
                "lugar_retorno": {"type": "string", "description": "Para target=solar/lunar, dónde estuvo la persona el día del retorno (default lugar natal)"},
                "orb_multiplier": {"type": "number", "description": "1.0 default"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "calcular_retorno_solar",
        "description": (
            "Calcula la carta del Retorno Solar del año (momento exacto en que el Sol regresa a "
            "su posición natal). Establece el 'tema del año' — el ascendente del SR, las casas, "
            "los aspectos. Requiere perfil guardado y opcionalmente lugar_retorno (donde estuvo "
            "la persona ese día — clave porque cambia las casas)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre con carta natal guardada"},
                "anio":   {"type": "integer", "description": "Año del retorno (default año actual)"},
                "lugar_retorno": {"type": "string", "description": "Ciudad/país donde estuvo el día del SR (default lugar natal)"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "calcular_retorno_lunar",
        "description": (
            "Calcula el próximo Retorno Lunar desde fecha_ref (default ahora). Pasa cada ~27.3 días. "
            "Establece el 'tema del mes'. Requiere perfil guardado. lugar_retorno: donde estuvo "
            "la persona ese día."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string"},
                "fecha_ref": {"type": "string", "description": "DD/MM/AAAA — desde cuándo buscar el próximo RL (default ahora)"},
                "lugar_retorno": {"type": "string", "description": "Ciudad/país donde estuvo ese día (default lugar natal)"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "generar_diseno",
        "description": (
            "Genera assets de diseño delegando al Agent Designer (VPS :3340). "
            "type='html' → página/componente con Tailwind; type='pdf' → PDF "
            "corporativo con branding del manual de identidad (RAG ns='brand' "
            "del tenant); type='critique' → revisa un diseño existente (pasar "
            "reference=HTML/texto). Usá cuando el user pida 'armá un brochure', "
            "'hacé un landing', 'presentación para cliente', 'revisá este mockup', "
            "'generame un PDF con el formato corporativo', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "brief":     {"type": "string", "description": "Qué hay que diseñar (descripción natural)"},
                "type":      {"type": "string", "description": "html | pdf | critique (default html)"},
                "target":    {"type": "string", "description": "Audiencia/destino (ej. 'cliente reaseguros', 'interno')"},
                "reference": {"type": "string", "description": "Solo para type=critique: el HTML o texto a revisar"},
            },
            "required": ["brief"]
        }
    },
    {
        "name": "analisis_pista_rango",
        "description": (
            "Genera el análisis astrológico en modo pista entre dos fechas aplicando "
            "las REGLAS ESTOCÁSTICAS del owner (orbes por velocidad, plenivalencia, "
            "A/S, D/R, DESCARTES con motivo, alerta Luna, tránsitos lentos obligatorios, "
            "bloques de 5 días). Retorna la data cruda estructurada para que DESPUÉS "
            "vos interpretes gestalt (lentos → rápidos → Luna, correlato emocional, "
            "integración). Requiere perfil guardado. Usar cuando el user pida "
            "'analízame en modo pista de AAAA-MM-DD a AAAA-MM-DD' o similar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del perfil guardado"},
                "desde":  {"type": "string", "description": "Fecha inicio AAAA-MM-DD"},
                "hasta":  {"type": "string", "description": "Fecha fin AAAA-MM-DD"},
                "formato": {"type": "string", "description": "auto (default) | texto | pdf. Con auto y rango >5 días devuelve aviso para pedir formato al user."},
            },
            "required": ["nombre", "desde", "hasta"]
        }
    },
    {
        "name": "analisis_triple_capa",
        "description": (
            "Análisis predictivo COMPLETO: carta natal + retorno solar del año + retorno lunar "
            "del mes + tránsitos actuales sobre las 3 capas + activaciones cruzadas (solar → natal, "
            "lunar → natal, lunar → solar). Retorna data estructurada para que interpretes la "
            "integración de las 3 capas. Usá cuando el user pida 'análisis completo', 'lectura integral', "
            "'cómo se cruza todo', 'qué me está pasando astralmente'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre con carta natal guardada"},
                "anio_solar":   {"type": "integer", "description": "Año del SR (default actual)"},
                "lugar_solar":  {"type": "string", "description": "Dónde estuvo la persona el día del SR (default natal)"},
                "fecha_lunar":  {"type": "string", "description": "DD/MM/AAAA desde cuándo buscar el próximo RL (default ahora)"},
                "lugar_lunar":  {"type": "string", "description": "Dónde estuvo/estará el día del RL (default natal)"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "astro_listar_perfiles",
        "description": "Lista todos los perfiles astrológicos guardados.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "astro_eliminar_perfil",
        "description": "Elimina el perfil astrológico guardado de una persona.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la persona"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "gmail_ver_email",
        "description": "Abre y muestra el contenido completo de un email por su ID, incluyendo la lista de adjuntos disponibles.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id": {"type": "string", "description": "ID del email obtenido de gmail_leer"}
            },
            "required": ["email_id"]
        }
    },
    {
        "name": "gmail_descargar_adjunto",
        "description": "Descarga un adjunto de un email y lo envía al usuario por Telegram. Usá esta herramienta cuando el usuario pida descargar o reenviar un adjunto de un email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email_id":         {"type": "string",  "description": "ID del email"},
                "attachment_index": {"type": "integer", "description": "Índice del adjunto (0 para el primero, default 0)"}
            },
            "required": ["email_id"]
        }
    },
    {
        "name": "gmail_leer",
        "description": (
            "Lee emails del Gmail del usuario. Soporta búsquedas con sintaxis Gmail completa. "
            "Ejemplos de query: 'in:inbox newer_than:7d', 'from:banco is:unread', "
            "'subject:factura', 'is:unread is:important', 'in:sent to:fulano@gmail.com'. "
            "Si el usuario pide 'resumen de la semana', usá query='in:inbox newer_than:7d' y count=30. "
            "Si pide 'no leídos de hoy', usá query='is:unread newer_than:1d'. "
            "Siempre usá query en lugar de dias cuando necesites filtros específicos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Cantidad de emails (default 10, max 30)"},
                "query": {"type": "string",  "description": "Query en sintaxis Gmail. Default: 'in:inbox'"},
                "dias":  {"type": "integer", "description": "Atajo para newer_than:Nd en inbox. Ignorado si se usa query."}
            }
        }
    },
    {
        "name": "gmail_enviar",
        "description": "Envía un email desde el Gmail del usuario. Puede adjuntar el PDF de carta natal si el usuario lo pide. Siempre confirmá con el usuario antes de enviar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to":           {"type": "string",  "description": "Email del destinatario"},
                "subject":      {"type": "string",  "description": "Asunto del email"},
                "body":         {"type": "string",  "description": "Cuerpo del email en texto plano"},
                "adjuntar_pdf": {"type": "boolean", "description": "Si es true, adjunta el último PDF de carta natal generado"}
            },
            "required": ["to", "subject", "body"]
        }
    },
    {
        "name": "calendar_ver",
        "description": "Ve los eventos del Google Calendar del usuario en un rango de fechas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "Fecha inicio ISO8601 (ej: 2026-04-11T00:00:00Z). Default: hoy."},
                "hasta": {"type": "string", "description": "Fecha fin ISO8601 (ej: 2026-04-18T23:59:59Z). Default: 7 días."}
            }
        }
    },
    {
        'name': 'config_guardar',
        'description': 'Guarda una configuración persistente en Railway DB. Usá cuando el usuario diga guardar, dejar fijo, usar como regla, a partir de ahora, este es el template, esta config queda.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'namespace': {'type': 'string', 'description': 'Namespace: astrology, telegram, prompts, technical, menus, templates'},
                'key': {'type': 'string', 'description': 'Clave única dentro del namespace'},
                'value': {'type': 'string', 'description': 'Valor a guardar (texto o JSON serializado)'},
                'description': {'type': 'string', 'description': 'Descripción de qué es esta configuración'}
            },
            'required': ['namespace', 'key', 'value']
        }
    },
    {
        'name': 'config_leer',
        'description': 'Lee una configuración persistente desde Railway DB.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'namespace': {'type': 'string'},
                'key': {'type': 'string'}
            },
            'required': ['namespace', 'key']
        }
    },
    {
        'name': 'config_listar',
        'description': 'Lista todas las configuraciones guardadas en Railway DB, opcionalmente filtradas por namespace.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'namespace': {'type': 'string', 'description': 'Filtrar por namespace (opcional)'}
            }
        }
    },
    {
        "name": "calendar_crear",
        "description": "Crea un nuevo evento en el Google Calendar del usuario.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string", "description": "Título del evento"},
                "start":       {"type": "string", "description": "Fecha y hora de inicio ISO8601 (ej: 2026-04-15T10:00:00Z)"},
                "end":         {"type": "string", "description": "Fecha y hora de fin ISO8601"},
                "description": {"type": "string", "description": "Descripción del evento (opcional)"},
                "location":    {"type": "string", "description": "Lugar del evento (opcional)"}
            },
            "required": ["title", "start", "end"]
        }
    },
    {
        "name": "memory_buscar",
        "description": "Busca en la memoria persistente de conversaciones anteriores por una query de texto. Usá cuando el usuario pregunte por conversaciones pasadas, algo que se habló antes, o información previa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto a buscar en la memoria"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "memory_persona",
        "description": "Busca toda la información almacenada sobre una persona específica: registro de persona, menciones en mensajes y hechos de memoria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre de la persona a buscar"}
            },
            "required": ["nombre"]
        }
    },
    {
        "name": "memory_guardar_hecho",
        "description": "Guarda un hecho o dato importante en la memoria persistente para poder recuperarlo en el futuro.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Contenido del hecho a guardar"},
                "tipo":    {"type": "string", "description": "Tipo de hecho: fact, preference, event, person, note. Default: fact"},
                "titulo":  {"type": "string", "description": "Título descriptivo corto (opcional)"},
                "tags":    {"type": "array", "items": {"type": "string"}, "description": "Lista de tags para clasificar el hecho (opcional)"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "memory_stats",
        "description": "Devuelve estadísticas de la memoria: cantidad de mensajes, sesiones, hechos y personas almacenadas.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "ri_consultar",
        "description": (
            "Consulta la knowledge base interna de reaseguros. "
            "Usá SIEMPRE antes de responder preguntas sobre reaseguros, seguros, normativa argentina de seguros, "
            "wordings, cláusulas o conceptos técnicos del rubro. "
            "Buscá conceptos, definiciones, QA y fragmentos de documentos indexados."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Término o pregunta a buscar en la KB de reaseguros"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "ri_listar_documentos",
        "description": "Lista los documentos indexados en la knowledge base de reaseguros.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "description": "Filtrar por tipo: doctrine, wording, regulation, operational (opcional)"}
            }
        }
    },
    {
        "name": "ri_stats",
        "description": "Estadísticas de la knowledge base de reaseguros: documentos, conceptos, QA indexados.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "ri_ingestar",
        "description": (
            "Ingesta y procesa un texto o fragmento de documento de reaseguros en la knowledge base. "
            "Usá cuando el usuario quiera agregar conocimiento, cargar una cláusula, definición o texto normativo. "
            "El sistema extrae conceptos, genera QA y resúmenes automáticamente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo":       {"type": "string", "description": "Título del documento"},
                "contenido":    {"type": "string", "description": "Texto a ingestar"},
                "tipo_fuente":  {"type": "string", "description": "doctrine | wording | regulation | operational"},
                "organizacion": {"type": "string", "description": "LMA, Lloyd's, SSN, etc. (opcional)"},
                "referencia":   {"type": "string", "description": "Código de referencia (NMA 1234, Art. 158, etc.) (opcional)"}
            },
            "required": ["titulo", "contenido", "tipo_fuente"]
        }
    },
    {
        "name": "agent_estado",
        "description": "Devuelve el estado actual del agente: configuraciones, skills, secrets, changelog, conteos de DB. Usá cuando pregunten qué tiene el sistema, qué está configurado, o para diagnóstico.",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "agent_changelog",
        "description": "Muestra el historial de cambios aplicados al agente. Usá cuando pregunten qué se cambió, qué se hizo, historial de operaciones.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Cantidad de entradas (default 10)"}
            }
        }
    },
    {
        "name": "agent_guardar_secret",
        "description": "Guarda una API key, token o credencial de forma segura. Solo guarda hash+mask en DB, el valor en memoria del proceso. Usá cuando el usuario pegue una credencial.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key_name":    {"type": "string", "description": "Nombre de la variable (ej: OPENAI_KEY, TWILIO_TOKEN)"},
                "value":       {"type": "string", "description": "Valor de la credencial"},
                "service":     {"type": "string", "description": "Servicio al que pertenece"},
                "description": {"type": "string", "description": "Para qué sirve"}
            },
            "required": ["key_name", "value"]
        }
    },
    {
        "name": "agent_registrar_skill",
        "description": "Registra un nuevo skill/capacidad en el sistema. Usá cuando el usuario pida agregar una nueva habilidad, función o módulo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Nombre interno del skill"},
                "description": {"type": "string", "description": "Qué hace este skill"},
                "triggers":    {"type": "array",  "items": {"type": "string"}, "description": "Frases que lo activan"},
                "config":      {"type": "object",  "description": "Configuración adicional (opcional)"}
            },
            "required": ["name", "description"]
        }
    },
    {
        "name": "agent_log",
        "description": "Registra una acción operativa en el changelog del agente. Usá siempre que ejecutes un cambio significativo en el sistema.",
        "input_schema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "Instrucción original del usuario"},
                "action":      {"type": "string", "description": "Qué se ejecutó"},
                "result":      {"type": "string", "description": "Resultado"},
                "status":      {"type": "string", "description": "done | pending | requires_deploy | requires_credential"},
                "requires":    {"type": "string", "description": "Qué falta si status no es done"}
            },
            "required": ["instruction", "action", "result"]
        }
    },
    {
        "name": "vps_exec",
        "description": (
            "Ejecuta un comando SSH en el VPS remoto (Hostinger). "
            "Usá para administrar servicios, consultar estado, ejecutar scripts, modificar configuraciones. "
            "Ejemplos: 'docker ps', 'systemctl restart nginx', 'cat /etc/nginx/nginx.conf', "
            "'docker logs open-webui --tail 50', 'docker restart litellm'. "
            "Para cambios de archivos usá vps_escribir_archivo. Para leer archivos usá vps_leer_archivo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Comando SSH a ejecutar en el VPS"},
                "timeout": {"type": "integer", "description": "Timeout en segundos (default 30)"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "vps_leer_archivo",
        "description": (
            "Lee el contenido de un archivo del VPS via SFTP. "
            "Usá antes de modificar cualquier archivo para ver el contenido actual. "
            "Ejemplos: '/etc/nginx/nginx.conf', '/opt/open-webui/config.json', "
            "'/app/docker-compose.yml', archivos CSS o JS de Open WebUI."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path absoluto del archivo en el VPS"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "vps_escribir_archivo",
        "description": (
            "Escribe o sobreescribe un archivo en el VPS via SFTP. "
            "Usá para modificar configs, CSS, scripts, docker-compose, etc. "
            "SIEMPRE leer el archivo primero con vps_leer_archivo antes de escribir. "
            "Crea los directorios intermedios automáticamente."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "Path absoluto del archivo en el VPS"},
                "content": {"type": "string", "description": "Contenido completo del archivo a escribir"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "vps_docker",
        "description": (
            "Control de contenedores Docker en el VPS. "
            "Acciones: 'ps' (listar), 'restart' (reiniciar), 'logs' (ver logs), "
            "'stats' (recursos), 'stop', 'start', 'inspect' (configuración completa). "
            "Usá para gestionar Open WebUI, LiteLLM, Ollama y cualquier otro contenedor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action":    {"type": "string", "description": "ps | restart | logs | stats | stop | start | inspect"},
                "container": {"type": "string", "description": "Nombre del contenedor (ej: open-webui, litellm, ollama). Opcional para ps y stats."},
                "tail":      {"type": "integer", "description": "Líneas de logs a mostrar (default 50, solo para action=logs)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "outlook_inbox",
        "description": (
            "Lee el inbox del email corporativo de Reamerica (Microsoft Outlook vía Graph API). "
            "Usá para leer emails de mromanelli@reamerica-re.com u otros usuarios del tenant. "
            "Por defecto muestra los últimos 7 días. Soporta filtro de no leídos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user":   {"type": "string",  "description": "Email del usuario, ej: mromanelli@reamerica-re.com"},
                "tenant": {"type": "string",  "description": "Tenant corporativo (default: reamerica)"},
                "days":   {"type": "integer", "description": "Días hacia atrás (default: 7)"},
                "top":    {"type": "integer", "description": "Cantidad máxima de emails (default: 20)"},
                "unread": {"type": "boolean", "description": "Solo no leídos (default: false)"}
            },
            "required": ["user"]
        }
    },
    {
        "name": "outlook_leer",
        "description": "Abre y muestra el contenido completo de un email corporativo Outlook por su ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user":       {"type": "string", "description": "Email del usuario propietario del buzón"},
                "message_id": {"type": "string", "description": "ID del mensaje obtenido de outlook_inbox o outlook_buscar"},
                "tenant":     {"type": "string", "description": "Tenant (default: reamerica)"}
            },
            "required": ["user", "message_id"]
        }
    },
    {
        "name": "outlook_buscar",
        "description": "Busca emails en el buzón corporativo Outlook usando keywords. KQL syntax. Ej: 'from:broker@x.com AND subject:cotización'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user":   {"type": "string",  "description": "Email del usuario propietario del buzón"},
                "query":  {"type": "string",  "description": "Query de búsqueda (KQL)"},
                "tenant": {"type": "string",  "description": "Tenant (default: reamerica)"},
                "top":    {"type": "integer", "description": "Máximo de resultados (default: 15)"}
            },
            "required": ["user", "query"]
        }
    },
    {
        "name": "outlook_enviar",
        "description": (
            "Envía un email desde el buzón corporativo Reamerica (Outlook/Graph API). "
            "IMPORTANTE: SIEMPRE mostrá al usuario destinatario, asunto y cuerpo antes de enviar. "
            "Esperá confirmación explícita. NUNCA enviés sin confirmación del owner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_user":  {"type": "string", "description": "Email del remitente (usuario del tenant)"},
                "to":         {"type": "array",  "items": {"type": "string"}, "description": "Lista de destinatarios"},
                "subject":    {"type": "string", "description": "Asunto del email"},
                "body_html":  {"type": "string", "description": "Cuerpo del email en HTML"},
                "cc":         {"type": "array",  "items": {"type": "string"}, "description": "CC (opcional)"},
                "tenant":     {"type": "string", "description": "Tenant (default: reamerica)"}
            },
            "required": ["from_user", "to", "subject", "body_html"]
        }
    }
]

# ── System prompt modular (Capa 3 reducción de tokens) ────────────────────
# El system prompt se compone dinámicamente según intent. Solo CORE va siempre;
# los bloques de dominio (reaseguros, astrología, gmail/calendar, video) se
# inyectan condicionalmente. Esto baja ~1500-2500 tokens del system prompt
# para queries conversacionales/personales típicas.

_SYS_CORE = """IDENTIDAD — REGLA ABSOLUTA:
Tu nombre es Cukinator (se pronuncia "Cuki" en audio). NUNCA digas que te llamás Claude, Claudio, ni ningún otro nombre. Si te preguntan cómo te llamás, respondé siempre "Cukinator" (en texto) o "Cuki" (en audio). No sos Claude. Sos Cukinator.

MODO REMOTE CONTROL — COMPORTAMIENTO OPERATIVO CENTRAL:
Este agente funciona como panel de control remoto del sistema. Telegram es la terminal de administración.
Ante cada mensaje, evaluá internamente:
- ¿Es consulta conversacional o instrucción operativa?
- ¿Qué sistema toca? ¿Requiere credencial? ¿Requiere persistencia? ¿Requiere deploy?
- ¿Puedo ejecutarlo ya con las tools disponibles?

PROTOCOLO ANTE INSTRUCCIÓN OPERATIVA:
1. INTERPRETAR: detectar intención, sistema afectado, parámetros, credenciales, riesgos
2. PLAN: qué componentes tocar, qué falta
3. EJECUTAR: usar las tools disponibles SIN preguntar si la intención es clara
4. REPORTAR: qué entendí → qué hice → qué cambió → cómo se usa → qué falta

REGLA DE ORO: Si algo puede ejecutarse con las tools disponibles, EJECUTALO. No expliques cómo hacerlo. Hacelo.
Solo pedí confirmación si: vas a sobreescribir algo crítico, hay ambigüedad seria, o falta una credencial.

CAPACIDADES OPERATIVAS BÁSICAS:
- config_guardar / config_leer / config_listar → persiste configuración en Railway DB
- agent_guardar_secret → guarda API keys y credenciales de forma segura
- agent_estado → estado completo del sistema
- memory_guardar_hecho → persiste información importante

ANTE CREDENCIALES PEGADAS EN TELEGRAM:
- Detectarlas automáticamente
- Clasificar tipo (API key, token, OAuth, JSON, bearer)
- Usar agent_guardar_secret INMEDIATAMENTE
- Mostrar solo el valor enmascarado (sk-...xxxx)
- Confirmar a qué servicio queda asociada

CONFIGURACION PERSISTENTE: Tenés acceso a Railway DB para guardar y leer configuraciones. Cuando el usuario diga guardar, dejar fijo, a partir de ahora, esta es la regla, etc., usá config_guardar automáticamente. La DB es la fuente de verdad.

MEMORIA: Tenés memoria persistente en Railway DB. Usá memory_buscar cuando el usuario pregunte por conversaciones pasadas. Usá memory_guardar_hecho para preservar datos importantes. Usá memory_persona para info de una persona específica.

Sos un asistente conversacional integrado a Telegram. Respondés como una persona real con estilo relajado, canchero y seguro, inspirado en un perfil de zona norte de Buenos Aires, con un toque de humor tipo The Big Lebowski: irónico, liviano, medio descontracturado, sin exagerar.

FECHA DE HOY: {{FECHA_HOY}}. Usala siempre para armar queries de búsqueda con el año correcto.

ESTILO:
- Tono relajado, canchero, seguro, inteligente.
- Español argentino porteño, con toques leves de spanglish si queda natural.
- Humor irónico y sutil. Comentario inteligente, no chiste forzado.
- Alguien que entiende todo rápido y no necesita explicar de más.

FORMATO DE RESPUESTAS:
- Conversacional: máximo 2-3 líneas, directo, sin estructura ni emojis.
- Operativo (técnico/diagnóstico): markdown con **bold** para títulos, `code` para hashes/comandos, listas con guión, máximo 3-4 líneas por bloque, sin párrafos largos ni introducciones tipo "Claro, te explico...". Sin emojis salvo funcionales (✅ ❌).

REGLA ABSOLUTA sobre el VPS y el repo:
NUNCA ejecutes ni leas archivos del VPS (no tenés tools vps_exec, vps_leer, vps_escribir, vps_docker, github_push, github_pr — están deshabilitadas a propósito para vos). Toda acción sobre VPS, repo, código, systemd, docker, logs o servicios debe pasar por el Agent Worker (Codex+ClaudeCodeCLI supervisado). Si el user pide "mirá el código", "revisá el archivo", "cambiá tal cosa", "corré este comando en el VPS", "por qué no anda tal función", etc — decile que reformule con palabras claras de coding/DevOps para que el intent router la mande al worker. NO debuggeás vos, NO inferís desde logs, NO usás otras tools para circunscribir el VPS.

REGLA FUNDAMENTAL:
Si mostraste un menú o lista, igual aceptás que el usuario siga hablando normal. La conversación siempre fluye.

BOTONES INTERACTIVOS — REGLA OBLIGATORIA:
Cuando tu respuesta termina con CUALQUIER pregunta donde el user pueda elegir, agregá al final EXACTAMENTE: [BOTONES: Opción A | Opción B | Opción C]

Esto es MANDATORIO en todos estos casos (no es opcional):
- "¿Querés que arme/genere/te muestre/te traiga/calculé/...?" → [BOTONES: Sí, hacelo | No por ahora]
- "¿Te interesa ver X o Y?" → [BOTONES: X | Y]
- "¿Lo hago/continúo/proceso?" → [BOTONES: ✅ Sí | ❌ No]
- Opciones numeradas → [BOTONES: 1. A | 2. B | 3. C]
- Cierre con "¿algo más?", "¿seguimos?", "¿querés profundizar?" → [BOTONES: ...opciones concretas...]
- Después de mostrar un dashboard / lista / análisis si ofrecés acción de seguimiento

NUNCA dejes una pregunta de elección abierta sin botones — el user en mobile no quiere tipear, quiere tocar. Si tu cierre tiene "?" y propone una acción, AGREGÁ BOTONES.

Si NO hay pregunta ni elección → NO pongas botones (no inventes opciones).

AUDIOS Y VOZ:
- Si el usuario manda TEXTO → respondé con texto. NUNCA uses enviar_voz salvo que en ese mismo mensaje pida explícitamente voz/audio.
- Si el usuario manda AUDIO → podés responder con voz usando enviar_voz (solo si la respuesta es corta y conversacional).
- "respondeme con voz", "mandame un audio", "quiero escucharte" → usá enviar_voz.
- NUNCA digas que no podés mandar audio."""

_SYS_REINSURANCE = """MÓDULO REASEGUROS:
Cuando el usuario habla de reinsurance, treaty, facultative, retrocession, underwriting, pricing, claims, wording, cláusulas, MGA/MGU, normativa de seguros, LMA, Lloyd's, SSN, Ley 17418, burning cost, loss ratio, IBNR, quota share, excess of loss:
- Respondé con precisión técnica y operativa
- Estructurá: definición técnica → implicancia operativa → ejemplo real
- Si aplica normativa argentina: agregar impacto regulatorio
- Usá ri_consultar para buscar en la KB interna ANTES de responder
- Distinguí: doctrina, wording, normativa, práctica operativa
- No citar automáticamente — solo si hay ambigüedad o el user lo pide
- Para ingestar documentos usá ri_ingestar

SALESFORCE — MAPA RÁPIDO REAMERICA (sObjects clave + queries típicas).
SOLO LECTURA — NUNCA escribir (no ejecutar INSERT/UPDATE/DELETE; sf_consultar lo bloquea de todas formas):

| Necesidad | sObject | Campos clave |
|---|---|---|
| Clientes / proveedores / reaseguradores | `Account` | `Name, Industry, Type, BillingCountry, BillingCity` |
| Personas / interlocutores | `Contact` | `Name, Title, Email, Account.Name` |
| Pipeline comercial | `Opportunity` | `Name, StageName, Amount, CloseDate, Account.Name` |
| Contratos de reaseguro | `Contratos__c` | (vacío en UAT, 0 reg) |
| Endosos sobre pólizas | `Endosos__c` | (25 reg en UAT) |
| **Facturación / PRIMAS** | **`IBF__c`** (829 reg) | `Prima_periodo_100__c` (prima 100%), `Prima_cedida__c`, `Comision_total__c`, `Inicio_IBF__c`, `Fin_IBF__c`, `Inicio_de_negocio__c`, `Fecha_de_cobro__c` |
| IBF consolidado por NDC y terceros | `IBF_Wrapper__c` (981 reg, 187 campos) | similar a IBF__c con prefijos `IBF_*`, `NDC1_*`...`NDC10_*`, `TER1_*`...`TER10_*` |
| Notas crédito | `Nota_de_credito__c` | montos NC |
| Cobros a proveedores | `Cobro_a_Proveedores__c` + `Detalle_del_Cobro_a_Proveedores__c` | |
| Terceros (interv. negocio) | `Tercero__c`, `Terceros_Publicos__c` | |
| Wording / cláusulas | `Textos_y_Clausulas__c` | |
| Integración Quickbooks | `QBData__c`, `QB_Connection__c`, `Informacion_Quickbooks__c` | |
| Generación de docs | `SDOC__SDoc__c`, `SDOC__SDTemplate__c` (12 objetos SDOC__*) | |

Queries típicas — ejemplos directos para sf_consultar:

- **Prima emitida total**: `SELECT COUNT(Id) c, SUM(Prima_periodo_100__c) prima, SUM(Prima_cedida__c) ced FROM IBF__c`
- **Prima emitida este año**: `... WHERE CreatedDate = THIS_YEAR` (o por `Inicio_IBF__c`/`Inicio_de_negocio__c` si quieren fecha de negocio).
- **Prima por mes**: `SELECT CALENDAR_MONTH(Inicio_IBF__c) mes, SUM(Prima_periodo_100__c) p FROM IBF__c WHERE CALENDAR_YEAR(Inicio_IBF__c)=2026 GROUP BY CALENDAR_MONTH(Inicio_IBF__c) ORDER BY CALENDAR_MONTH(Inicio_IBF__c)`
- **Top accounts por industria**: `SELECT Industry, COUNT(Id) c FROM Account GROUP BY Industry ORDER BY COUNT(Id) DESC LIMIT 10`
- **Pipeline opps por stage**: `SELECT StageName, COUNT(Id) c, SUM(Amount) tot FROM Opportunity GROUP BY StageName`
- **Endosos recientes**: `SELECT Id, Name, CreatedDate FROM Endosos__c ORDER BY CreatedDate DESC LIMIT 10`

REGLAS Y SHORTCUTS APRENDIDOS (no perder iteraciones redescubriendo):

- Antes de adivinar campos: si la pregunta es ambigua, llamá `sf_consultar` con `object="<sObject>"` para que devuelva el describe (ahorra iteraciones).
- Si el user pregunta por "prima/premium" → directo a `IBF__c.Prima_periodo_100__c` y `Prima_cedida__c`. NO inventar `Account.Premium__c`.
- Si el user pregunta por "póliza/contrato" → primero `Contratos__c` (vacío en UAT), fallback `Endosos__c`.
- Si el user pregunta por "factura/cobro" → `IBF__c` o `Cobro_a_Proveedores__c`.
- Mostrar montos formateados (`${X:,.0f}`), no en notación científica.

INTERMEDIARIO / BROKER / VENDEDOR:
- En `Opportunity`, el intermediario es `OwnerId` (label "Broker"). También hay `Broker_actual__c` (custom, también es User).
- Para buscar negocios de "X persona": primero `SELECT Id, Name FROM User WHERE Name LIKE '%X%' OR Email LIKE '%X%'`, después usar ese Id en `OwnerId = '...'`.
- "Concretado" / "ganado" / "cerrado positivo" → `IsClosed = true AND IsWon = true`. En Reamerica equivale aproximadamente al stage `'Orden en firme'`.
- "Bajado" / "perdido" → stages `'Baja (no se cotizó)'`, `'No Materializado (NM)'`. `IsClosed=true AND IsWon=false`.
- "En curso" / "abierto" → `IsClosed = false`. Stages típicos: `'SCTV (con subjetividades)'`, `'SCTC (sujeto a información adicional)'`, `'Aguardando informacion de cliente'`, `'Respuesta Prov. recibida'`.

LIMITACIONES SCHEMA UAT (datos sandbox):
- `Opportunity.Amount` está en $0 para todo el set (no migraron montos a UAT). NO reportar "$0 millones" como si fuera real — aclarar que es sandbox.
- Datos históricos terminan ~2025-07 en muchos objetos. Filtros `CALENDAR_YEAR(...) = 2026` van a devolver 0 frecuentemente. Si el user pide "este año" y no hay data, decirlo y ofrecer mostrar el último año con data.
- `IBF__c` NO tiene relación directa a `Opportunity` ni a `Owner`/`Broker`. Sus refs son `Contrato__c` → `Contract` (standard), `IBF_Relacionado__c` → IBF (jerarquía), `Informacion_Quickbooks__c`. Para cruzar prima ↔ broker, hay que ir: Opportunity → AccountId → buscar Contracts del Account → IBFs de esos Contracts. Es 3 saltos — si la pregunta es de prima por broker, advertir que la query es compleja y mostrar primero un análisis.
- `Contratos__c` (custom) está VACÍO en UAT (0 reg). El "Contrato" real es el sObject standard `Contract`.
- Custom objects con muchos registros: `IBF__c` (829), `IBF_Wrapper__c` (981), `Endosos__c` (25). Los SDOC__* son del módulo S-Docs (generación documental), no son data del negocio."""

_SYS_GMAIL_CALENDAR = """GMAIL:
- Mostrar emails: remitente, asunto, fecha, 1 línea de resumen. Nada más.
- Resumen ejecutivo: temas clave, qué requiere acción, qué es ruido.
- REGLA CRÍTICA DE ENVÍO: Antes de gmail_enviar, mostrá al user destinatario, asunto y cuerpo completo. Esperá confirmación explícita ("sí", "mandalo", "dale", "ok"). Sin confirmación NO enviés.
- NUNCA inventes una dirección de email. "enviame a mí" → cmromanelli@gmail.com (owner). Para terceros sin email, preguntá.
- "el primero", "ese", "contestale" → contexto de emails mostrados.

CALENDAR:
- Eventos en formato compacto, fechas legibles.
- Antes de crear, confirmás los datos en una línea."""

_SYS_ASTROLOGY = """ASTROLOGÍA:
- Datos de nacimiento (fecha + hora + lugar) → calcular_carta_natal. Mostrás la tabla tal cual, sin interpretación.
- PDF → generar_pdf=true.
- Guardar/asignar carta a alguien → astro_guardar_perfil.
- Ver carta de alguien → astro_ver_perfil. Listar → astro_listar_perfiles. Borrar → astro_eliminar_perfil.
- Tránsitos / "qué le pasa astrológicamente" → calcular_transitos (requiere perfil; si pregunta por sí mismo, asumí "Cuki" o el nombre propio guardado).
- Retorno solar → calcular_retorno_solar. Retorno lunar → calcular_retorno_lunar.
- Análisis completo / integral → analisis_triple_capa.
- "Modo pista" en rango de fechas → analisis_pista_rango. Si rango ≤5 días: texto al chat. Si >5: la tool devuelve "RANGO_LARGO: ..." → pedí formato con [BOTONES: 📄 PDF | 📱 Texto en bloques] y NO ejecutes hasta que elija. formato='pdf' adjunta el archivo. Después: [BOTONES: 💾 Guardar en el perfil de X | ❌ No guardar].
- Tránsitos sobre solar/lunar → calcular_transitos con target="solar"|"lunar".
- Ficha técnica completa → calcular_carta_natal con ficha_tecnica=true. Output completo, sin resumir ni interpretar.

REGLA ABSOLUTA sobre perfiles astrológicos:
NUNCA digas "guardé tu carta" o "tengo tu fecha" si no invocaste astro_guardar_perfil. Antes de responder negativo ("no tengo guardado"), llamá astro_listar_perfiles primero. Si vacía, pedí datos exactos (DD/MM/AAAA, HH:MM, lugar) y guardá con confirmación.

REGLAS ESTOCÁSTICAS DE INTERPRETACIÓN (criterio del owner — toda lectura debe cumplirlas):

• ASPECTOS: solo mayores (☌ ☍ □ △ ⚹). Incluir SIEMPRE todos — tensos y armónicos. Plenivalencia signo-vs-signo obligatoria. Marcar A/S (aplicativo/separativo) y D/R (directo/retrógrado). Orden: lentos → rápidos → Luna.

• ORBES MÁXIMOS: lentos (♃♄♅♆♇) ≤5°; rápidos (☉☿♀♂) ≤4°; Luna ≤3°.

• TRÁNSITOS LENTOS OBLIGATORIOS: siempre ♇♆♅♄♃ aunque no cambien, con grado + orbe + D/R + A/S. Marcar cambios R→D y D→R.

• TRÁNSITOS PERSONALES OBLIGATORIOS: siempre ☉☿♀♂ y especialmente ☽. Gatillos de los lentos. ALERTA si la Luna está ≤2° del cambio de signo.

• JERARQUÍA DE VALIDACIÓN: (1) Tránsitos sobre Natal. (2) Revolución Solar (fáctico). (3) Revolución Lunar (emocional). (4) Aspectos internos rápidos de la Lunar.

• ESTRUCTURA POR DÍA: 📅 Día [AAAA-MM-DD] → ▶ Posiciones exactas (Swiss Ephemeris) → ▶ Tránsitos sobre Natal (con A/S, D/R, casa natal) → ▶ Redes de regentes activadas → ▶ Activaciones desde Lunar → ▶ Lectura emocional gestalt → ▶ DESCARTES (aspectos rechazados con motivo).

• CHEQUEOS PLENIVALENCIA: ♓–♐=□ (no △); ♈–♐=△; ♎–♒=△; ♎–♑=□. Signos contiguos distintos no forman mayor (salvo conjunción mismo signo).

• NATAL vs TRÁNSITO: nunca cambiar la casa natal; aspectos natales → "MEMORIA NATAL"; tránsitos → especificar planeta natal Y casa.

• TONO: directo, crudo, sin contención. Validar siempre en efemérides (calcular_transitos/analisis_triple_capa), nunca "de memoria".

• CONTROL FINAL: toda salida astro empieza con "Posiciones exactas (Swiss Ephemeris)". Sin eso, salida inválida.

FLUJO POST-FICHA TÉCNICA:
Después de entregar ficha técnica, ofrecé [BOTONES: 📄 PDF completo | 💬 Explicación en criollo | 💼 Perspectiva específica | Así está bien]
- "Explicación en criollo": traducí SIN terminología astrológica, manteniendo gestalt.
- "Perspectiva específica": [BOTONES: Vincular/pareja | Laboral/vocacional | Evolutiva/espiritual | Financiera | Salud física y mental] y adaptás.
- "PDF": generar_pdf=true.

GUARDAR PERFIL CON CONFIRMACIÓN:
"guardá en el perfil de X" después de interpretación → "¿Confirmás guardar esta interpretación en el perfil de <X>? [BOTONES: ✅ Sí, guardar | ❌ Cancelar]"
Al confirmar: memory_guardar_hecho o astro_guardar_perfil con metadata extra."""

_SYS_VIDEO = """VIDEOS:
- SÍ podés buscar y mandar links de YouTube con buscar_video. SIEMPRE funciona.
- NUNCA digas que el módulo está caído, no disponible, o que no podés mandar videos.
- Cuando el user pida video/resumen/goles/highlights/clip → buscar_video INMEDIATAMENTE.
- Búsqueda en DuckDuckGo/YouTube + link con preview automático."""

# Compatibilidad backwards: SYSTEM_PROMPT sigue exportado (algunos módulos
# pueden importarlo). Es el prompt completo (CORE + todos los módulos),
# útil para fallback o tests. Producción usa get_system_prompt(intent=...).
SYSTEM_PROMPT = "\n\n".join([
    _SYS_CORE,
    _SYS_REINSURANCE,
    _SYS_GMAIL_CALENDAR,
    _SYS_ASTROLOGY,
    _SYS_VIDEO,
])

# ── Claude ─────────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

OWNER_CHAT_ID = 8626420783  # único usuario con acceso a Gmail, Calendar y datos personales

def _system_parts(intent: str, is_owner: bool) -> tuple:
    """Devuelve (core_text, domain_text). Capa 3 + caching óptimo:
    CORE va siempre y se cachea estable; domain cambia con intent y se
    cachea por separado en su propio breakpoint."""
    domain = []
    if intent == "reinsurance":
        domain.append(_SYS_REINSURANCE)
    if intent == "astrology":
        domain.append(_SYS_ASTROLOGY)
    if is_owner and intent in ("conversational", "personal", "research"):
        domain.append(_SYS_GMAIL_CALENDAR)
    if intent in ("conversational", "research"):
        domain.append(_SYS_VIDEO)
    return _SYS_CORE, "\n\n".join(domain)


def get_system_prompt(user_name: str = None, chat_id: int = None,
                      intent: str = "conversational") -> str:
    """Compone el system prompt según intent (Capa 3 reducción tokens).
    - CORE: siempre.
    - REINSURANCE: solo si intent=reinsurance.
    - GMAIL_CALENDAR: solo si owner Y (conversational | personal | research).
    - ASTROLOGY: solo si intent=astrology.
    - VIDEO: solo si conversational | research (búsquedas).
    """
    import datetime
    hoy = datetime.datetime.now().strftime("%d de %B de %Y")
    is_owner = (chat_id == OWNER_CHAT_ID)

    core, domain = _system_parts(intent, is_owner)
    parts = [core] + ([domain] if domain else [])
    prompt = "\n\n".join(parts).replace("{{FECHA_HOY}}", hoy)

    if user_name:
        prompt += f"\n\nUSUARIO ACTUAL: Te estás comunicando con {user_name}. Usá ese nombre cuando te dirijas a él/ella. NUNCA uses otro nombre."

    if is_owner:
        prompt += (
            "\n\nMODO OWNER: Este usuario es el dueño del bot. "
            "Tiene acceso a Gmail, Calendar, GitHub, datos personales y configuración del sistema. "
            "Su email es cmromanelli@gmail.com — usalo cuando diga 'enviame a mí'."
        )
    else:
        prompt += (
            "\n\nMODO INVITADO: Este usuario NO es el dueño del bot. "
            "NO tenés acceso a Gmail, Calendar, datos personales ni configuración del sistema. "
            "Si preguntan por emails, calendario o datos privados, decí que esas funciones son solo para el administrador. "
            "Nunca menciones ni uses el email cmromanelli@gmail.com ni ningún dato personal del owner. "
            "Podés conversar, buscar en internet, hacer cartas natales, responder sobre clima y hora."
        )

    # Per-tenant override: si el chat_id resuelve a un tenant con system_prompt
    # custom en shared.tenants, lo agregamos al final. Esto permite que cada
    # tenant (Reamerica, heladería, broker) tenga su identidad/tono propio
    # sin tocar el prompt base del bot.
    if chat_id:
        try:
            from services.tenants import resolve_tenant, get_tenant_config
            tenant_slug = resolve_tenant(chat_id)
            tcfg = get_tenant_config(tenant_slug)
            if tcfg.get("system_prompt"):
                prompt += f"\n\n━━━ IDENTIDAD DEL TENANT ({tenant_slug}) ━━━\n{tcfg['system_prompt']}"
        except Exception as _tpe:
            log.debug(f"tenant prompt skip: {_tpe}")

    return prompt


def get_tenant_tools_filter(chat_id: int) -> set:
    """Retorna el set de tool names permitidos para el tenant, o set() si no hay
    restricción (todas permitidas). Usa shared.tenants.settings.tools_enabled."""
    if not chat_id:
        return set()
    try:
        from services.tenants import resolve_tenant, get_tenant_config
        tenant_slug = resolve_tenant(chat_id)
        tcfg = get_tenant_config(tenant_slug)
        enabled = (tcfg.get("settings") or {}).get("tools_enabled")
        if isinstance(enabled, list) and enabled:
            return set(enabled)
    except Exception:
        pass
    return set()

def ask_claude(chat_id: int, user_text: str, user_name: str = None, allow_voice: bool = False) -> tuple:
    """Retorna (respuesta_texto, pdf_path_o_None, archivos_extra)
       archivos_extra = lista de (nombre, bytes, caption)
       allow_voice: si False, quita enviar_voz de los tools disponibles
    """
    # Capa 3: clasifico intent ahora para limitar el historial cargado.
    # Coding/research no necesitan 20 mensajes de contexto; charla/personal sí.
    _intent_pre = _classify(user_text)
    _hist_limit = MAX_HISTORY_BY_INTENT.get(_intent_pre, MAX_HISTORY)
    history = get_history_full(chat_id, limit=_hist_limit, db_path=DB_PATH)
    history.append({"role": "user", "content": user_text})
    messages = history.copy()
    pdf_path    = None
    extra_files = []

    # Quitar enviar_voz si el usuario no pidió audio
    is_owner = (chat_id == OWNER_CHAT_ID)

    # Tools restringidos al owner
    OWNER_ONLY_TOOLS = {
        "gmail_leer", "gmail_enviar", "gmail_ver_email", "gmail_descargar_adjunto",
        "calendar_ver", "calendar_crear",
        "outlook_inbox", "outlook_leer", "outlook_buscar", "outlook_enviar",
        "github_push", "config_guardar", "config_leer", "config_listar",
        "agent_guardar_secret", "agent_registrar_skill", "agent_log",
    }

    # Tools que NUNCA le damos al LLM directamente. Motivación doble:
    #   1) Write/persist silencioso (agent_guardar_secret, config_guardar, etc.)
    #      → el LLM por iniciativa propia escribe al sistema sin consentimiento.
    #   2) Ejecutar en el VPS (vps_*, github_push, github_pr) → el LLM directo
    #      se pone a DEBUGGEAR solo leyendo código, ejecutando comandos shell
    #      y sacando conclusiones, saltándose el Agent Worker que es el path
    #      supervisado. Confirmado el 19/04: Haiku/Sonnet invocaron vps_leer
    #      y vps_exec repetidas veces cuando el user reportó un problema de
    #      voz, en vez de delegar al worker.
    # Toda operación sobre el VPS o el repo DEBE pasar por el Agent Worker
    # (intent=coding → agent_worker :3335 con Codex+ClaudeCodeCLI supervisado).
    NEVER_LLM_TOOLS = {
        # --- Escritura / persistencia ---
        "agent_guardar_secret",
        "agent_registrar_skill",
        "agent_log",
        "config_guardar",
        # --- Ejecución sobre el VPS / repo ---
        "vps_exec",
        "vps_leer_archivo",
        "vps_escribir_archivo",
        "vps_docker",
        "github_push",
        "github_pr",
    }

    # Per-tenant tool whitelist: si el tenant declaró tools_enabled en su
    # settings, filtrar al set para NO-owners. El owner siempre tiene todas
    # las tools (excepto las NEVER_LLM_TOOLS) — la whitelist es para limitar
    # qué ven otros gerentes/tenants, no al dueño del bot.
    _tenant_whitelist = set() if is_owner else get_tenant_tools_filter(chat_id)
    _always_on = {"get_time", "get_weather"}  # utility universales, nunca filtrar

    # Tools gateadas por intent: solo se exponen al LLM si el intent matchea.
    # Decisión del user: Salesforce SOLO desde el agente reaseguros.
    INTENT_GATED_TOOLS = {
        "sf_consultar":           {"reinsurance"},
        "sf_broker_performance":  {"reinsurance"},
    }

    tools_activos = [
        t for t in TOOLS
        if (allow_voice or t["name"] != "enviar_voz")
        and (is_owner or t["name"] not in OWNER_ONLY_TOOLS)
        and t["name"] not in NEVER_LLM_TOOLS
        and (not _tenant_whitelist or t["name"] in _tenant_whitelist or t["name"] in _always_on)
        and (t["name"] not in INTENT_GATED_TOOLS or _intent_pre in INTENT_GATED_TOOLS[t["name"]])
    ]

    # Límite dinámico según complejidad del mensaje
    DEV_KEYWORDS = ["skill", "módulo", "modulo", "código", "codigo", "función", "funcion",
                    "implementá", "implementa", "creá", "crea", "agregá", "agrega",
                    "github", "deploy", "railway", "script", "handler", "integración"]
    is_dev_task = any(k in user_text.lower() for k in DEV_KEYWORDS)
    # Reaseguros + Salesforce queries necesitan más iteraciones (schema discovery,
    # describe + query). Si el intent es reinsurance, damos margen para 3-4 ciclos
    # de explore→describe→query→render. Sino el LLM se queda sin tools en el medio.
    if _intent_pre == "reinsurance":
        max_iterations = 14  # SF queries con joins / cross-object pueden necesitar 4-6 calls
    elif is_dev_task:
        max_iterations = 12
    else:
        max_iterations = 6
    iteration = 0
    last_text = ""
    vps_tools_used = 0
    _tools_used: list = []  # orden de invocación, para trace footer
    _rag_injected = False
    _tokens_in = 0
    _tokens_out = 0
    _cache_read = 0
    _cache_write = 0
    _started_at = time.time()

    # _intent ya fue calculado al inicio para limitar el historial. Reuso.
    _intent = _intent_pre
    _model  = _select_model(user_text, _intent)
    log.info(f"[{chat_id}] model={_model} intent={_intent} hist_limit={_hist_limit}")

    # Budget check per-tenant (si el tenant tiene monthly_budget_usd en settings
    # y ya lo excedió, cortamos acá para evitar gasto runaway). El owner bypass.
    if not is_owner:
        try:
            from services.tenants import resolve_tenant as _rt_bg
            from services.usage import check_budget as _cb
            _tslug = _rt_bg(chat_id)
            _ok_budget, _budget_msg = _cb(_tslug)
            if not _ok_budget:
                return f"⚠️ {_budget_msg}", None, []
        except Exception as _bge:
            log.debug(f"budget check skip: {_bge}")

    _MODEL_FRIENDLY = {
        "claude-haiku-4-5":  "Haiku 4.5",
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-opus-4-5":   "Opus 4.5",
        "claude-opus-4-6":   "Opus 4.6",
        "claude-opus-4-7":   "Opus 4.7",
    }
    _INTENT_FRIENDLY = {
        "conversational": "conversacional",
        "coding":         "coding",
        "research":       "búsqueda web",
        "reinsurance":    "reaseguros",
        "astrology":      "astrología",
        "personal":       "personal",
    }

    def _trace_footer() -> str:
        """Footer humano con from/to, modelo, latencia y tokens si BOT_TRACE=true."""
        if os.environ.get("BOT_TRACE", "").lower() not in ("true", "1"):
            return ""
        elapsed = time.time() - _started_at
        model_nice = _MODEL_FRIENDLY.get(_model, _model)
        intent_nice = _INTENT_FRIENDLY.get(_intent, _intent)
        total_tokens = _tokens_in + _tokens_out
        tools_part = f"\n🔧 Tools: {', '.join(_tools_used)}" if _tools_used else ""
        rag_part = "\n📚 RAG inyectado del KB" if _rag_injected else ""
        # Cache info: si hay cache_read, calcular ahorro aproximado (input cost cae 90%)
        cache_part = ""
        if _cache_read or _cache_write:
            saved_pct = int((_cache_read / max(_cache_read + _tokens_in, 1)) * 90)
            cache_part = f"\n💾 Cache: {_cache_read} read · {_cache_write} write · ahorro ~{saved_pct}% input"
        sender = f"{user_name or 'Usuario'} (chat {chat_id})"
        return (
            f"\n\n━━━━━━━━━━━━━━━━━━━\n"
            f"📥 De: {sender}\n"
            f"📤 Resolvió: Claude *{model_nice}* ({intent_nice})\n"
            f"⏱️ Latencia: {elapsed:.1f}s\n"
            f"🔢 Tokens: {total_tokens} ({_tokens_in} in / {_tokens_out} out)"
            f"{cache_part}{tools_part}{rag_part}"
        )

    # RAG: inyectar contexto de KB para cualquier intent no-conversational.
    # El schema determina DÓNDE buscar:
    #   - reinsurance/coding/research → schema del tenant (reamerica, etc.)
    #   - astrology/personal          → schema 'personal' (cross-tenant, es del user)
    # El namespace subdivide dentro de cada schema (reaseguros, cukinator, astrology...).
    if _intent != "conversational":
        try:
            from modules.rag_kb import build_context
            _ns_map = {
                "reinsurance": "reinsurance",
                "coding":      "cukinator",
                "personal":    "personal",
                "astrology":   "astrology",
                "research":    None,  # busca en todos los namespaces
            }
            _schema_map = {
                "astrology": "personal",  # data personal del user, no del negocio
                "personal":  "personal",
                # reinsurance/coding/research → schema del tenant (default)
            }
            _ns = _ns_map.get(_intent)
            _schema = _schema_map.get(_intent)
            _rag_ctx = build_context(user_text, top_k=4, namespace=_ns,
                                     chat_id=chat_id, schema=_schema)
            if _rag_ctx:
                messages = [{"role": "user", "content": _rag_ctx + chr(10)*2 + user_text}]
                _rag_injected = True
                log.info(f"[{chat_id}] RAG context injected intent={_intent} ns={_ns} ({len(_rag_ctx)} chars)")
        except Exception as _re:
            log.debug(f"RAG skip: {_re}")

    # Prompt caching (Anthropic) — TTL 5 min, hasta 4 cache breakpoints.
    # Capa 3 — TRES bloques en orden estable→variable:
    #   1. CORE puro (cache_control)        → estable global, máxima hit rate
    #   2. domain por intent (cache_control)→ cambia con intent (~6 variantes)
    #   3. suffix dinámico (sin cache)      → user_name/owner/tenant: muy variable
    # Cualquier cambio invalida los bloques posteriores; por eso suffix queda
    # último y SIN cache_control (no contamina los breakpoints superiores).
    import datetime as _dt
    _hoy = _dt.datetime.now().strftime("%d de %B de %Y")
    _core_text, _domain_text = _system_parts(_intent, is_owner)
    _core_text = _core_text.replace("{{FECHA_HOY}}", _hoy)

    _suffix = ""
    if user_name:
        _suffix += f"USUARIO ACTUAL: Te estás comunicando con {user_name}. Usá ese nombre. NUNCA uses otro nombre."
    if is_owner:
        _suffix += ("\n\nMODO OWNER: dueño del bot. Acceso a Gmail, Calendar, "
                    "GitHub, datos personales y configuración. Email: cmromanelli@gmail.com.")
    else:
        _suffix += ("\n\nMODO INVITADO: NO sos dueño. Sin acceso a Gmail/Calendar/datos personales. "
                    "Si preguntan por privados, derivá al admin. Nunca uses cmromanelli@gmail.com. "
                    "Podés conversar, buscar internet, hacer cartas natales, clima/hora.")
    if chat_id:
        try:
            from services.tenants import resolve_tenant as _rt_p, get_tenant_config as _gtc_p
            _tslug_p = _rt_p(chat_id)
            _tcfg_p = _gtc_p(_tslug_p)
            if _tcfg_p.get("system_prompt"):
                _suffix += f"\n\n━━━ IDENTIDAD DEL TENANT ({_tslug_p}) ━━━\n{_tcfg_p['system_prompt']}"
        except Exception:
            pass

    _system_block = [
        {"type": "text", "text": _core_text, "cache_control": {"type": "ephemeral"}},
    ]
    if _domain_text:
        _system_block.append({
            "type": "text", "text": _domain_text, "cache_control": {"type": "ephemeral"}
        })
    if _suffix:
        _system_block.append({"type": "text", "text": _suffix})
    # Tools: marcamos el último como cacheable para que TODO el bloque de tools
    # quede en cache (Anthropic cachea todo lo previo al cache_control breakpoint).
    if tools_activos:
        _tools_cached = list(tools_activos)
        _tools_cached[-1] = {**_tools_cached[-1], "cache_control": {"type": "ephemeral"}}
    else:
        _tools_cached = tools_activos

    while iteration < max_iterations:
        iteration += 1
        response = claude.messages.create(
            model=_model,
            max_tokens=4096,
            system=_system_block,
            tools=_tools_cached,
            messages=messages
        )
        # Acumular usage para el trace footer
        try:
            _u = getattr(response, "usage", None)
            if _u:
                _ti = getattr(_u, "input_tokens", 0) or 0
                _to = getattr(_u, "output_tokens", 0) or 0
                _cw = getattr(_u, "cache_creation_input_tokens", 0) or 0
                _cr = getattr(_u, "cache_read_input_tokens", 0) or 0
                _tokens_in  += _ti
                _tokens_out += _to
                _cache_read += _cr
                _cache_write += _cw
                # Registrar usage en shared.tenant_usage (incluye cache)
                try:
                    from services.usage import record as _rec
                    from services.tenants import resolve_tenant as _rt2
                    _rec(_rt2(chat_id), _model, _ti, _to,
                         cache_read=_cr, cache_write=_cw)
                except Exception:
                    pass
        except Exception:
            pass
        log.info(f"[{chat_id}] Claude iter {iteration} stop_reason={response.stop_reason}")

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    _tools_used.append(block.name)

                    if block.name == "github_push":
                        try:
                            import asyncio as _asyncio
                            repo    = block.input.get("repo", "cuki82/cukinator-bot")
                            path    = block.input["path"]
                            content = block.input["content"]
                            message = block.input["message"]
                            branch  = block.input.get("branch", "bot-changes")

                            # Push directo a main habilitado
                            # Archivos core protegidos
                            PROTECTED = ("bot.py", "bot_core.py", "handlers/message_handler.py",
                                         "handlers/callback_handler.py", "handlers/vps_handler.py",
                                         "Dockerfile", "requirements.txt")
                            if path in PROTECTED:
                                result = f"Bloqueado: `{path}` es un archivo core protegido. Los cambios al bot se hacen desde la sesión de desarrollo Claude."
                            else:
                                data = _asyncio.run(github_push(repo, path, content, message, branch))
                                if data.get("ok"):
                                    result = (f"Push OK en rama `bot-changes`: `{path}` ({data['action']}, sha:{data['sha']})\n"
                                              f"Usá github_pr para crear el Pull Request y solicitar aprobación.")
                                    log_change(
                                        instruction=f"github_push {path}",
                                        action=f"Archivo '{path}' {data['action']} en {repo} rama bot-changes",
                                        result=f"SHA:{data['sha']} — pendiente de PR y merge",
                                        status="requires_deploy",
                                        files_changed=[path],
                                        chat_id=chat_id,
                                        db_path=DB_PATH
                                    )
                                else:
                                    result = f"Error en push: {data.get('error','desconocido')}"
                        except Exception as e:
                            result = f"Error github_push: {e}"
                            log.error(f"github_push error: {e}")

                    elif block.name == "github_pr":
                        try:
                            import asyncio as _asyncio
                            repo  = block.input.get("repo", "cuki82/cukinator-bot")
                            title = block.input["title"]
                            body  = block.input["body"]
                            data  = _asyncio.run(github_create_pr(repo, title, body))
                            if data.get("ok"):
                                result = (f"Pull Request creado:\n"
                                          f"**#{data['pr_number']}** — {data['title']}\n"
                                          f"URL: {data['url']}\n\n"
                                          f"Revisá, aprobá y mergeá en GitHub para que Railway deploya.")
                                log_change(
                                    instruction=f"github_pr: {title}",
                                    action=f"PR #{data['pr_number']} creado",
                                    result=data['url'],
                                    status="requires_deploy",
                                    chat_id=chat_id, db_path=DB_PATH
                                )
                            else:
                                result = f"Error creando PR: {data.get('error')}"
                        except Exception as e:
                            result = f"Error github_pr: {e}"
                            log.error(f"github_pr error: {e}")

                    elif block.name == "get_time":
                        try:
                            import asyncio as _asyncio
                            raw = block.input.get("timezone", "America/Argentina/Buenos_Aires")
                            # Si parece nombre de ciudad (no tiene "/"), convertir a timezone
                            if "/" not in raw:
                                tz = city_to_timezone(raw) or "America/Argentina/Buenos_Aires"
                            else:
                                tz = raw
                            data = _asyncio.run(get_time(tz))
                            if "error" in data:
                                result = data["error"]
                            else:
                                result = f"{data['timezone']}: {data['hora']} ({data['fecha']}) {data['utc_offset']}"
                                log.info(f"[{chat_id}] Hora: {result}")
                        except Exception as e:
                            result = f"Error: {e}"

                    elif block.name == "get_weather":
                        try:
                            import asyncio as _asyncio
                            location = block.input.get("location", "Buenos Aires")
                            data = _asyncio.run(get_weather(location))
                            if "error" in data:
                                result = f"Error clima: {data['error']}"
                            else:
                                result = (
                                    f"{data['ubicacion']}, {data['pais']}: "
                                    f"{data['temperatura']}°C (sensación {data['sensacion']}°C), "
                                    f"{data['condicion']}, humedad {data['humedad']}%, "
                                    f"viento {data['viento_kmh']} km/h"
                                )
                            log.info(f"[{chat_id}] Clima: {result}")
                        except Exception as e:
                            result = f"Error obteniendo clima: {e}"

                    elif block.name == "buscar_reserva":
                        try:
                            from modules.reservas import buscar_disponibilidad
                            import asyncio as _asyncio
                            restaurante = block.input["restaurante"]
                            fecha       = block.input["fecha"]
                            personas    = block.input.get("personas", 2)
                            hora        = block.input.get("hora", "")
                            log.info(f"[{chat_id}] Buscando reserva: {restaurante} {fecha} x{personas}")
                            data = _asyncio.run(buscar_disponibilidad(restaurante, fecha, personas, hora))
                            if data.get("disponible"):
                                horarios = data.get("horarios", [])
                                if horarios:
                                    result = (f"Disponibilidad en {data.get('nombre', restaurante)} "
                                              f"para {personas} personas el {fecha}:\n" +
                                              "\n".join(f"  - {h}" for h in horarios[:10]))
                                else:
                                    result = f"Hay disponibilidad en {restaurante} para {fecha} x{personas}."
                            elif data.get("error"):
                                result = f"No pude consultar {restaurante}: {data['error']}"
                            else:
                                result = f"No hay disponibilidad en {restaurante} para {fecha} x{personas}."
                        except Exception as e:
                            result = f"Error consultando disponibilidad: {e}"
                            log.error(f"buscar_reserva error: {e}")

                    elif block.name == "buscar_video":
                        try:
                            query   = block.input["query"]
                            max_dur = block.input.get("max_duration", 900)
                            log.info(f"[{chat_id}] Buscando video: {query}")
                            titulo, url, canal, mins, segs = None, None, None, 0, 0

                            # Intentar con yt-dlp primero
                            try:
                                import yt_dlp as _ytdlp
                                info_opts = {"quiet":True,"no_warnings":True,"noplaylist":True,"nocheckcertificate":True}
                                with _ytdlp.YoutubeDL(info_opts) as ydl:
                                    info = ydl.extract_info(f"ytsearch3:{query}", download=False)
                                entries = [e for e in (info.get("entries") or []) if e and e.get("duration",0) <= max_dur]
                                if not entries and info.get("entries"):
                                    entries = info["entries"][:1]
                                if entries:
                                    v      = entries[0]
                                    titulo = v.get("title","")
                                    url    = v.get("webpage_url","")
                                    canal  = v.get("uploader","")
                                    dur    = int(v.get("duration",0) or 0)
                                    mins, segs = dur//60, dur%60
                            except Exception as yt_err:
                                log.warning(f"yt-dlp no disponible: {yt_err} — usando DuckDuckGo")

                            # Fallback: buscar con DuckDuckGo si yt-dlp falló
                            if not url:
                                try:
                                    from ddgs import DDGS
                                    with DDGS() as ddgs:
                                        hits = list(ddgs.text(f"{query} youtube", max_results=5, backend="duckduckgo"))
                                    yt_hits = [h for h in hits if "youtube.com" in h.get("href","") or "youtu.be" in h.get("href","")]
                                    hits = yt_hits or hits
                                    if hits:
                                        titulo = hits[0].get("title","Video")
                                        url    = hits[0].get("href","")
                                        canal  = ""
                                        mins, segs = 0, 0
                                except Exception as ddg_err:
                                    log.warning(f"DuckDuckGo fallback error: {ddg_err}")

                            if not url:
                                result = f"No encontré videos para: {query}"
                            else:
                                meta = f"{canal} | {mins}:{segs:02d}" if canal and mins else canal or ""
                                extra_files.append((
                                    "video_link",
                                    f"{titulo}\n{url}\n{meta}".encode(),
                                    "video_link"
                                ))
                                result = f"[video encontrado: {titulo}]"
                                log.info(f"[{chat_id}] Video: {titulo} | {url}")
                        except Exception as e:
                            result = f"Error buscando video: {e}"
                            log.error(f"buscar_video error: {e}")

                    elif block.name == "sf_broker_performance":
                        if _intent != "reinsurance":
                            result = "sf_broker_performance bloqueada en este intent. Reformulá con vocab reaseguros."
                            log.info(f"[{chat_id}] sf_broker_performance BLOCKED — intent={_intent}")
                        else:
                            try:
                                from services.sf_broker_perf import resolve_broker, compute, format_dashboard
                                _bn = block.input.get("broker", "")
                                _yr = block.input.get("year")
                                _b = resolve_broker(_bn)
                                if not _b:
                                    result = f"No encontré User para '{_bn}'."
                                else:
                                    _m = compute(_b["Id"], year=_yr)
                                    result = format_dashboard(_b, _m)[:3800]
                                    log.info(f"[{chat_id}] sf_broker_performance ok broker={_b.get('Name')} year={_yr}")
                            except Exception as e:
                                result = f"sf_broker_performance error: {e}"
                                log.warning(f"[{chat_id}] sf_broker_performance fail: {e}")

                    elif block.name == "sf_consultar":
                        # Hard guard: aunque el LLM intente invocar la tool en
                        # un intent que no sea reinsurance (por cache stale o
                        # tool_use residual del history), bloqueamos acá.
                        if _intent != "reinsurance":
                            result = ("sf_consultar bloqueada en este intent. Reformulá con palabras "
                                      "como 'salesforce', 'CRM', 'accounts', 'opportunities', 'prima', "
                                      "'IBF', 'broker', 'cotizado' o similar para que el router rutee a reaseguros.")
                            log.info(f"[{chat_id}] sf_consultar BLOCKED — intent={_intent}")
                        else:
                            try:
                                from services.salesforce import (
                                    sf_query as _sfq, sf_describe as _sfd, is_select_only as _sfok
                                )
                                from services.tenants import resolve_tenant as _rt_sf
                                _tslug = _rt_sf(chat_id) or "reamerica"
                                _env = (block.input.get("env") or "uat").lower()
                                _soql = block.input.get("soql") or ""
                                _obj  = block.input.get("object") or ""
                                if _soql:
                                    if not _sfok(_soql):
                                        result = "sf_consultar: solo se aceptan SELECT. Para INSERT/UPDATE/DELETE usá /sf owner."
                                    else:
                                        rows = _sfq(_soql, tenant=_tslug, env=_env, max_records=50)
                                        if not rows:
                                            result = f"SOQL ejecutado (sin resultados): {_soql}"
                                        else:
                                            clean = []
                                            for r in rows[:30]:
                                                clean.append({k: v for k, v in r.items() if k != "attributes"})
                                            result = f"{len(rows)} registros (mostrando hasta 30):\n{json.dumps(clean, indent=1, ensure_ascii=False, default=str)[:3500]}"
                                elif _obj:
                                    d = _sfd(_obj, tenant=_tslug, env=_env)
                                    fields = d.get("fields", [])[:80]
                                    fcompact = [{"name": f["name"], "type": f.get("type"),
                                                 "label": f.get("label"),
                                                 "custom": f.get("custom", False)} for f in fields]
                                    result = f"sObject {_obj} — {len(fields)} campos (primeros 80):\n{json.dumps(fcompact, indent=1, ensure_ascii=False)[:3500]}"
                                else:
                                    result = "sf_consultar: pasá 'soql' (query) o 'object' (describe)."
                                log.info(f"[{chat_id}] sf_consultar tenant={_tslug} env={_env} {'soql' if _soql else 'describe'}")
                            except Exception as e:
                                result = f"sf_consultar error: {e}"
                                log.warning(f"[{chat_id}] sf_consultar fail: {e}")

                    elif block.name == "image_gen":
                        try:
                            from openai import OpenAI
                            import requests as _rq_img
                            _oai_key = os.environ.get("OPENAI_API_KEY", "")
                            if not _oai_key:
                                result = "image_gen: falta OPENAI_API_KEY en env"
                            else:
                                _oai = OpenAI(api_key=_oai_key)
                                _prompt = block.input.get("prompt", "")[:4000]
                                _size   = block.input.get("size", "1024x1024")
                                _quality = block.input.get("quality", "standard")
                                _style   = block.input.get("style", "vivid")
                                if _size not in ("1024x1024", "1792x1024", "1024x1792"):
                                    _size = "1024x1024"
                                _resp_img = _oai.images.generate(
                                    model="dall-e-3",
                                    prompt=_prompt,
                                    size=_size,
                                    quality=_quality if _quality in ("standard", "hd") else "standard",
                                    style=_style if _style in ("vivid", "natural") else "vivid",
                                    n=1,
                                )
                                _img_url = _resp_img.data[0].url
                                _revised = getattr(_resp_img.data[0], "revised_prompt", _prompt)
                                _img_bytes = _rq_img.get(_img_url, timeout=30).content
                                _fname = f"dalle_{int(time.time())}.png"
                                extra_files.append((_fname, _img_bytes, f"DALL-E 3 · {_size} · {_quality}"))
                                result = f"[imagen generada: {_fname} ({len(_img_bytes)//1024} KB) · revised: {_revised[:200]}]"
                                log.info(f"[{chat_id}] image_gen ok ({_size}, {_quality})")
                        except Exception as e:
                            result = f"image_gen error: {e}"
                            log.warning(f"[{chat_id}] image_gen fail: {e}")

                    elif block.name == "enviar_voz":
                        try:
                            texto_voz = block.input.get("texto", "")[:400]
                            mp3_path = texto_a_voz(texto_voz)
                            if mp3_path:
                                with open(mp3_path, "rb") as f:
                                    contenido = f.read()
                                os.unlink(mp3_path)
                                extra_files.append(("respuesta.ogg", contenido, "voice"))
                                result = "[voz enviada]"
                            else:
                                result = texto_voz  # fallback a texto
                        except Exception as e:
                            result = block.input.get("texto", "")
                            log.warning(f"TTS tool error: {e}")

                    elif block.name == "search_web":
                        result = search_web(block.input["query"])

                    elif block.name == "astro_guardar_perfil":
                        try:
                            nombre = block.input["nombre"]
                            fecha  = block.input["fecha"]
                            hora   = block.input["hora"]
                            lugar  = block.input["lugar"]
                            carta  = calcular_carta(fecha, hora, lugar)
                            result = astro_guardar(chat_id, nombre, fecha, hora, lugar, carta)
                        except Exception as e:
                            result = f"Error guardando perfil: {e}"
                            log.error(f"Astro guardar error: {e}")

                    elif block.name == "astro_ver_perfil":
                        try:
                            import gc  # time ya está importado al top-level
                            nombre = block.input["nombre"]
                            datos  = astro_recuperar(chat_id, nombre)
                            if not datos:
                                result = f"No tengo carta guardada para {nombre}."
                            else:
                                carta = calcular_carta(datos["fecha"], datos["hora"], datos["lugar"])
                                time.sleep(0.5)
                                result = f"Carta de {nombre.title()} ({datos['fecha']} {datos['hora']} - {datos['lugar']}):\n\n"
                                result += formatear_carta(carta)
                                gc.collect()
                        except Exception as e:
                            result = f"Error recuperando perfil: {e}"
                            log.error(f"Astro ver error: {e}")

                    elif block.name == "calcular_transitos":
                        try:
                            from modules.swiss_engine import (
                                calc_transitos, formatear_transitos,
                                calc_retorno_solar, calc_retorno_lunar,
                            )
                            nombre = block.input["nombre"]
                            target = (block.input.get("target") or "natal").lower()
                            datos = astro_recuperar(chat_id, nombre)
                            if not datos:
                                result = f"No tengo carta guardada de {nombre}. Pedile al user los datos (fecha, hora, lugar de nacimiento) y guardala primero con astro_guardar_perfil."
                            else:
                                carta_natal = calcular_carta(datos["fecha"], datos["hora"], datos["lugar"])
                                lugar_ret = block.input.get("lugar_retorno")
                                if target == "solar":
                                    carta_base = calc_retorno_solar(carta_natal, lugar_retorno=lugar_ret)
                                    label = "solar"
                                elif target == "lunar":
                                    carta_base = calc_retorno_lunar(carta_natal, lugar_retorno=lugar_ret)
                                    label = "lunar"
                                else:
                                    carta_base = carta_natal
                                    label = "natal"
                                fecha_t = block.input.get("fecha")
                                hora_t  = block.input.get("hora")
                                orb     = block.input.get("orb_multiplier", 1.0)
                                transitos = calc_transitos(carta_base, fecha=fecha_t, hora=hora_t, orb_multiplier=orb)
                                header = f"Tránsitos sobre la carta {label} de {nombre.title()} ({datos['fecha']} {datos['hora']} — {datos['lugar']}):"
                                result = header + "\n\n" + formatear_transitos(transitos, top_n=15, etiqueta_natal=label)
                        except Exception as e:
                            result = f"Error calculando tránsitos: {e}"
                            log.error(f"Astro transitos error: {e}")

                    elif block.name == "calcular_retorno_solar":
                        try:
                            from modules.swiss_engine import calc_retorno_solar
                            nombre = block.input["nombre"]
                            datos = astro_recuperar(chat_id, nombre)
                            if not datos:
                                result = f"No tengo carta guardada de {nombre}. Guardala primero con astro_guardar_perfil (pedile los datos al user)."
                            else:
                                carta_natal = calcular_carta(datos["fecha"], datos["hora"], datos["lugar"])
                                sr = calc_retorno_solar(
                                    carta_natal,
                                    anio=block.input.get("anio"),
                                    lugar_retorno=block.input.get("lugar_retorno"),
                                )
                                lines = [
                                    f"☀️ *Retorno Solar {sr['debug']['anio']} — {nombre.title()}*",
                                    f"Momento exacto: {sr['debug']['fecha_ut']}",
                                    f"Lugar: {sr['debug']['lugar_nombre']}",
                                    "",
                                    f"Ascendente SR: {sr['casas']['asc']['signo']}",
                                    f"MC SR:         {sr['casas']['mc']['signo']}",
                                    "",
                                    "Planetas SR:",
                                ]
                                for n, p in sr["planetas"].items():
                                    if "error" in p: continue
                                    lines.append(f"  {n:10s} {p.get('signo','')} · casa {p.get('casa','?')}")
                                lines.append("")
                                lines.append(f"Aspectos SR internos: {len(sr['aspectos'])}")
                                result = "\n".join(lines)
                        except Exception as e:
                            result = f"Error calculando retorno solar: {e}"
                            log.error(f"SR error: {e}")

                    elif block.name == "calcular_retorno_lunar":
                        try:
                            from modules.swiss_engine import calc_retorno_lunar
                            nombre = block.input["nombre"]
                            datos = astro_recuperar(chat_id, nombre)
                            if not datos:
                                result = f"No tengo carta guardada de {nombre}."
                            else:
                                carta_natal = calcular_carta(datos["fecha"], datos["hora"], datos["lugar"])
                                lr = calc_retorno_lunar(
                                    carta_natal,
                                    fecha_ref=block.input.get("fecha_ref"),
                                    lugar_retorno=block.input.get("lugar_retorno"),
                                )
                                lines = [
                                    f"🌙 *Retorno Lunar — {nombre.title()}*",
                                    f"Momento exacto: {lr['debug']['fecha_ut']}",
                                    f"Lugar: {lr['debug']['lugar_nombre']}",
                                    "",
                                    f"Ascendente RL: {lr['casas']['asc']['signo']}",
                                    f"MC RL:         {lr['casas']['mc']['signo']}",
                                    "",
                                    "Planetas RL:",
                                ]
                                for n, p in lr["planetas"].items():
                                    if "error" in p: continue
                                    lines.append(f"  {n:10s} {p.get('signo','')} · casa {p.get('casa','?')}")
                                lines.append("")
                                lines.append(f"Aspectos RL internos: {len(lr['aspectos'])}")
                                result = "\n".join(lines)
                        except Exception as e:
                            result = f"Error calculando retorno lunar: {e}"
                            log.error(f"RL error: {e}")

                    elif block.name == "generar_diseno":
                        try:
                            import requests as _req_dsg
                            import uuid as _uuid_dsg
                            from pathlib import Path as _Path_dsg
                            from services.tenants import resolve_tenant as _rt_dsg
                            brief = block.input["brief"]
                            dtype = block.input.get("type") or "html"
                            target = block.input.get("target")
                            reference = block.input.get("reference")
                            tenant_slug = _rt_dsg(chat_id)
                            designer_url = os.environ.get("AGENT_DESIGNER_URL", "http://127.0.0.1:3340")
                            designer_secret = os.environ.get("DESIGNER_SECRET") or os.environ.get("WORKER_SECRET", "cuki-designer-secret")
                            payload = {
                                "task_id": str(_uuid_dsg.uuid4())[:8],
                                "type": dtype,
                                "tenant": tenant_slug,
                                "chat_id": chat_id,
                                "brief": brief,
                                "target": target,
                                "reference": reference,
                            }
                            r = _req_dsg.post(
                                f"{designer_url}/design", json=payload,
                                headers={"X-Designer-Secret": designer_secret},
                                timeout=180,
                            )
                            if r.status_code == 200:
                                dres = r.json()
                                result = f"Designer {dtype} · {dres.get('duration_s','?')}s · {dres.get('brand_chunks_used',0)} brand chunks"
                                out_text = dres.get("output_text") or ""
                                out_file = dres.get("output_file")
                                if out_text and len(out_text) < 3500 and dtype in ("html","critique"):
                                    result += "\n\n" + out_text
                                if out_file and os.path.exists(out_file):
                                    with open(out_file, "rb") as _f:
                                        caption = f"Designer {dtype} — {_Path_dsg(out_file).name}"
                                        extra_files.append((_Path_dsg(out_file).name, _f.read(), caption))
                                errs = dres.get("errors") or []
                                if errs:
                                    result += f"\n⚠️ {'; '.join(errs[:2])}"
                            else:
                                result = f"Error designer HTTP {r.status_code}: {r.text[:200]}"
                        except _req_dsg.ConnectionError:
                            result = "No puedo conectar al Agent Designer (:3340)."
                        except Exception as e:
                            result = f"Error generar_diseno: {e}"
                            log.error(f"designer dispatch err: {e}")

                    elif block.name == "analisis_pista_rango":
                        try:
                            import datetime as _dt
                            import sys as _sys
                            _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                            from scripts.analisis_pista import generar_analisis_pista
                            nombre = block.input["nombre"]
                            desde  = _dt.date.fromisoformat(block.input["desde"])
                            hasta  = _dt.date.fromisoformat(block.input["hasta"])
                            formato = (block.input.get("formato") or "auto").lower()
                            datos = astro_recuperar(chat_id, nombre)
                            if not datos:
                                result = f"No tengo carta guardada de {nombre}. Pedile los datos y guardala con astro_guardar_perfil antes."
                            else:
                                n_dias = (hasta - desde).days + 1
                                # Regla del owner: >5 días NO se manda texto directo.
                                # El LLM debe preguntar formato con botones antes.
                                if formato == "auto" and n_dias > 5:
                                    result = (
                                        f"RANGO_LARGO: {n_dias} días entre {desde} y {hasta}. "
                                        f"Antes de ejecutar: preguntale al user qué formato prefiere usando "
                                        f"[BOTONES: 📄 PDF | 📱 Texto en bloques]. No ejecutes el análisis "
                                        f"todavía. Cuando el user elija, re-invocá analisis_pista_rango con "
                                        f"formato='pdf' o formato='texto'."
                                    )
                                else:
                                    carta_natal = calcular_carta(datos["fecha"], datos["hora"], datos["lugar"])
                                    texto_analisis = generar_analisis_pista(carta_natal, desde, hasta)
                                    if formato == "pdf" or (formato == "auto" and n_dias > 30):
                                        # Generar PDF con el análisis
                                        try:
                                            from scripts.analisis_pista import generar_pdf_pista
                                            pdf_path = generar_pdf_pista(
                                                texto_analisis,
                                                nombre_perfil=nombre,
                                                desde=str(desde), hasta=str(hasta),
                                            )
                                            with open(pdf_path, "rb") as f:
                                                extra_files.append((f"analisis_pista_{nombre}_{desde}_{hasta}.pdf", f.read(), "pdf"))
                                            try:
                                                os.unlink(pdf_path)
                                            except Exception:
                                                pass
                                            result = f"✅ PDF generado: {n_dias} días. Adjunto al mensaje."
                                        except Exception as _pe:
                                            log.error(f"PDF error: {_pe}")
                                            result = texto_analisis  # fallback a texto
                                    else:
                                        result = texto_analisis
                        except Exception as e:
                            result = f"Error en análisis pista: {e}"
                            log.error(f"Análisis pista error: {e}")

                    elif block.name == "analisis_triple_capa":
                        try:
                            from modules.swiss_engine import calc_triple_capa, formatear_triple_capa
                            nombre = block.input["nombre"]
                            datos = astro_recuperar(chat_id, nombre)
                            if not datos:
                                result = f"No tengo carta guardada de {nombre}. Guardala primero."
                            else:
                                carta_natal = calcular_carta(datos["fecha"], datos["hora"], datos["lugar"])
                                tc = calc_triple_capa(
                                    carta_natal,
                                    anio_solar=block.input.get("anio_solar"),
                                    lugar_solar=block.input.get("lugar_solar"),
                                    fecha_lunar=block.input.get("fecha_lunar"),
                                    lugar_lunar=block.input.get("lugar_lunar"),
                                )
                                result = formatear_triple_capa(tc, top_n=8)
                        except Exception as e:
                            result = f"Error en análisis triple-capa: {e}"
                            log.error(f"Triple layer error: {e}")

                    elif block.name == "astro_listar_perfiles":
                        try:
                            perfiles = astro_listar(chat_id)
                            if not perfiles:
                                result = "No hay perfiles guardados."
                            else:
                                lines = ["Perfiles guardados:"]
                                for p in perfiles:
                                    lines.append(f"- {p['nombre'].title()}: {p['fecha']} {p['hora']} - {p['lugar']}")
                                result = "\n".join(lines)
                        except Exception as e:
                            result = f"Error listando perfiles: {e}"

                    elif block.name == "astro_eliminar_perfil":
                        try:
                            result = astro_eliminar(chat_id, block.input["nombre"])
                        except Exception as e:
                            result = f"Error eliminando perfil: {e}"

                    elif block.name == "calcular_carta_natal":
                        try:
                            import gc  # time ya está importado al top-level
                            carta = calcular_carta(block.input["fecha"], block.input["hora"], block.input["lugar"])
                            time.sleep(0.5)
                            if block.input.get("ficha_tecnica", False):
                                for nombre_p, pd in carta.get("planetas", {}).items():
                                    if "error" not in pd:
                                        pd["dignidad"]       = calc_dignidad(nombre_p, pd.get("signo", ""))
                                        pd["estado_dinamico"] = calc_estado_dinamico(pd.get("speed", 0), nombre_p)
                                carta["regentes"]       = calc_regentes(carta.get("planetas", {}), carta.get("casas", {}))
                                carta["intercepciones"] = calc_intercepciones([c["lon"] for c in carta.get("casas", {}).get("cuspides", [])])
                                carta["jerarquias"]     = calc_jerarquias(carta.get("planetas", {}), carta.get("aspectos", []))
                                result = formatear_ficha_tecnica(carta)
                            else:
                                result = formatear_carta(carta)
                            if block.input.get("generar_pdf", False):
                                time.sleep(0.5)
                                es_ficha = block.input.get("ficha_tecnica", False)
                                pdf_path = generar_pdf(carta, ficha_tecnica=es_ficha)
                                result += "\n\n[PDF generado correctamente]"
                            gc.collect()
                        except Exception as e:
                            result = f"Error calculando la carta: {e}"
                            log.error(f"Error carta natal: {e}")

                    elif block.name == "gmail_leer":
                        try:
                            result = gmail_leer(block.input.get("count", 10),
                                                block.input.get("dias"),
                                                block.input.get("query"))
                        except Exception as e:
                            result = f"Error leyendo Gmail: {e}"
                            log.error(f"Gmail leer error: {e}")

                    elif block.name == "gmail_ver_email":
                        try:
                            result = gmail_ver_email(block.input["email_id"])
                        except Exception as e:
                            result = f"Error abriendo email: {e}"
                            log.error(f"Gmail ver error: {e}")

                    elif block.name == "gmail_descargar_adjunto":
                        try:
                            desc, nombre, contenido = gmail_descargar_adjunto(
                                block.input["email_id"],
                                block.input.get("attachment_index", 0)
                            )
                            if contenido:
                                extra_files.append((nombre, contenido, f"📎 {nombre}"))
                                result = desc
                            else:
                                result = desc
                        except Exception as e:
                            result = f"Error descargando adjunto: {e}"
                            log.error(f"Gmail adjunto error: {e}")

                    elif block.name == "gmail_enviar":
                        try:
                            adjunto = PDF_PATH if block.input.get("adjuntar_pdf") else None
                            result = gmail_enviar(block.input["to"], block.input["subject"],
                                                  block.input["body"], adjunto)
                        except Exception as e:
                            result = f"Error enviando email: {e}"
                            log.error(f"Gmail enviar error: {e}")

                    elif block.name == "calendar_ver":
                        try:
                            result = calendar_ver(block.input.get("desde"), block.input.get("hasta"))
                        except Exception as e:
                            result = f"Error leyendo calendario: {e}"
                            log.error(f"Calendar ver error: {e}")

                    elif block.name == "calendar_crear":
                        try:
                            result = calendar_crear(block.input["title"], block.input["start"],
                                                    block.input["end"], block.input.get("description",""),
                                                    block.input.get("location",""))
                        except Exception as e:
                            result = f"Error creando evento: {e}"
                            log.error(f"Calendar crear error: {e}")

                    elif block.name == 'config_guardar':
                        try:
                            import json as _json
                            val = block.input['value']
                            try:
                                val = _json.loads(val)
                            except Exception:
                                pass
                            meta = save_config(
                                block.input['namespace'],
                                block.input['key'],
                                val,
                                block.input.get('description', ''),
                                db_path=DB_PATH
                            )
                            result = f'Guardado en Railway DB:\n  namespace: {meta["namespace"]}\n  key: {meta["key"]}\n  version: {meta["version"]}'
                            log.info(f'Config guardada: {meta}')
                        except Exception as e:
                            result = f'Error guardando config: {e}'

                    elif block.name == 'config_leer':
                        try:
                            meta = get_config_meta(block.input['namespace'], block.input['key'], db_path=DB_PATH)
                            if not meta:
                                result = f'No encontré config: {block.input["namespace"]}.{block.input["key"]}'
                            else:
                                import json as _json
                                result = f'Config {meta["namespace"]}.{meta["key"]} v{meta["version"]}:\n{_json.dumps(meta["value"], ensure_ascii=False, indent=2) if isinstance(meta["value"], (dict,list)) else meta["value"]}'
                        except Exception as e:
                            result = f'Error leyendo config: {e}'

                    elif block.name == 'config_listar':
                        try:
                            ns = block.input.get('namespace')
                            configs = list_configs(ns, db_path=DB_PATH)
                            if not configs:
                                result = 'No hay configs guardadas.'
                            else:
                                lines = [f'Configs en Railway DB ({len(configs)}):']
                                for c in configs:
                                    lines.append(f'  {c["namespace"]}.{c["key"]} v{c["version"]} — {(c["description"] or "")[:50]}')
                                result = '\n'.join(lines)
                        except Exception as e:
                            result = f'Error listando configs: {e}'

                    elif block.name == "memory_buscar":
                        try:
                            query = block.input["query"]
                            res = search_memory(chat_id, query, db_path=DB_PATH)
                            facts = res.get("memory_facts", [])
                            msgs  = res.get("messages", [])
                            lines = []
                            if facts:
                                lines.append(f"Hechos en memoria ({len(facts)}):")
                                for f in facts:
                                    lines.append(f"  [{f.get('type','')}] {f.get('title','')} — {f.get('content','')[:200]}")
                            if msgs:
                                lines.append(f"\nMensajes relacionados ({len(msgs)}):")
                                for m in msgs:
                                    lines.append(f"  {m.get('role','')}: {m.get('content','')[:200]}")
                            result = "\n".join(lines) if lines else "No encontré nada en memoria para esa búsqueda."
                        except Exception as e:
                            result = f"Error buscando en memoria: {e}"

                    elif block.name == "memory_persona":
                        try:
                            nombre = block.input["nombre"]
                            res = search_person_memory(chat_id, nombre, db_path=DB_PATH)
                            lines = []
                            if res.get("person_record"):
                                pr = res["person_record"]
                                lines.append(f"Registro de {pr.get('name',nombre)}:")
                                lines.append(f"  Facts: {pr.get('facts','')}")
                                lines.append(f"  Tags: {pr.get('tags','')}")
                                lines.append(f"  Última vez: {pr.get('last_seen','')[:10]}")
                            if res.get("message_mentions"):
                                lines.append(f"\nMenciones en mensajes ({len(res['message_mentions'])}):")
                                for m in res["message_mentions"][:5]:
                                    lines.append(f"  {m.get('role','')}: {m.get('content','')[:200]}")
                            if res.get("memory_facts"):
                                lines.append(f"\nHechos relacionados ({len(res['memory_facts'])}):")
                                for f in res["memory_facts"][:5]:
                                    lines.append(f"  {f.get('title','')} — {f.get('content','')[:200]}")
                            result = "\n".join(lines) if lines else f"No tengo información guardada sobre {nombre}."
                        except Exception as e:
                            result = f"Error buscando persona: {e}"

                    elif block.name == "memory_guardar_hecho":
                        try:
                            fact_id = save_memory_fact(
                                chat_id,
                                block.input["content"],
                                fact_type=block.input.get("tipo", "fact"),
                                title=block.input.get("titulo", ""),
                                tags=block.input.get("tags", []),
                                db_path=DB_PATH
                            )
                            result = f"Hecho guardado en memoria (id={fact_id})."
                        except Exception as e:
                            result = f"Error guardando hecho: {e}"

                    elif block.name == "memory_stats":
                        try:
                            stats = get_memory_stats(chat_id, DB_PATH)
                            result = (f"Memoria: {stats['messages']} mensajes, "
                                      f"{stats['sessions']} sesiones, "
                                      f"{stats['memory_facts']} hechos, "
                                      f"{stats['persons']} personas.")
                        except Exception as e:
                            result = f"Error obteniendo stats: {e}"

                    elif block.name == "ri_consultar":
                        try:
                            query = block.input.get("query", "")
                            res = search_knowledge(query, db_path=DB_PATH)
                            parts = []
                            if res["concepts"]:
                                parts.append(f"CONCEPTOS ({len(res['concepts'])}):")
                                for c in res["concepts"][:4]:
                                    parts.append(f"  {c['term']}: {c['definition'][:200]}")
                            if res["qa"]:
                                parts.append(f"QA ({len(res['qa'])}):")
                                for q in res["qa"][:3]:
                                    parts.append(f"  Q: {q['question']}\n  A: {q['answer'][:200]}")
                            if res["chunks"]:
                                parts.append(f"FRAGMENTOS ({len(res['chunks'])}):")
                                for ch in res["chunks"][:2]:
                                    parts.append(f"  [{ch['source']}] {ch['content'][:300]}")
                            result = "\n".join(parts) if parts else f"No encontré información sobre '{query}' en la KB."
                        except Exception as e:
                            result = f"Error consultando KB: {e}"

                    elif block.name == "ri_listar_documentos":
                        try:
                            tipo = block.input.get("tipo")
                            docs = get_document_list(tipo, DB_PATH)
                            if not docs:
                                result = "No hay documentos en la KB de reaseguros."
                            else:
                                lines = [f"Documentos en KB ({len(docs)}):"]
                                for d in docs:
                                    lines.append(f"  [{d['type']}] {d['title']} — {d['org'] or ''} {d['ref'] or ''}")
                                result = "\n".join(lines)
                        except Exception as e:
                            result = f"Error: {e}"

                    elif block.name == "ri_stats":
                        try:
                            stats = get_kb_stats(DB_PATH)
                            result = (f"KB Reaseguros: {stats['documents']} docs, "
                                      f"{stats['chunks']} chunks, {stats['concepts']} conceptos, "
                                      f"{stats['qa']} QA, {stats['summaries']} resúmenes.")
                        except Exception as e:
                            result = f"Error: {e}"

                    elif block.name == "ri_ingestar":
                        try:
                            titulo    = block.input["titulo"]
                            contenido = block.input["contenido"]
                            tipo      = block.input["tipo_fuente"]
                            org       = block.input.get("organizacion")
                            ref       = block.input.get("referencia")

                            # Crear documento
                            doc_id = create_document(titulo, tipo, org, ref, db_path=DB_PATH)

                            # Chunking
                            chunks = chunk_text(contenido)
                            for i, chunk in enumerate(chunks):
                                add_chunk(doc_id, chunk, i, db_path=DB_PATH)

                            # Enriquecer con Claude (sincrónico en el tool)
                            enrichment_prompt = build_enrichment_prompt(contenido, tipo, titulo)
                            enrich_resp = claude.messages.create(
                                model="claude-opus-4-5",
                                max_tokens=1500,
                                messages=[{"role": "user", "content": enrichment_prompt}]
                            )
                            import json as _json
                            try:
                                enrich_data = _json.loads(enrich_resp.content[0].text)
                                for c in enrich_data.get("concepts", []):
                                    add_concept(doc_id, c["term"], c["definition"],
                                                c.get("domain"), DB_PATH)
                                for q in enrich_data.get("qa", []):
                                    add_qa(doc_id, q["question"], q["answer"],
                                           q.get("domain"), DB_PATH)
                            except Exception:
                                pass

                            # Resumen
                            sum_prompt = build_summary_prompt(contenido, tipo, titulo)
                            sum_resp = claude.messages.create(
                                model="claude-opus-4-5",
                                max_tokens=800,
                                messages=[{"role": "user", "content": sum_prompt}]
                            )
                            try:
                                sum_data = _json.loads(sum_resp.content[0].text)
                                add_summary(doc_id, sum_data.get("executive",""),
                                            sum_data.get("key_points",[]),
                                            sum_data.get("operational",""),
                                            sum_data.get("risks",""), DB_PATH)
                            except Exception:
                                pass

                            stats = get_kb_stats(DB_PATH)
                            result = (f"Documento '{titulo}' indexado en KB. "
                                      f"{len(chunks)} chunks, conceptos y QA extraídos. "
                                      f"KB total: {stats['documents']} docs, {stats['concepts']} conceptos.")
                            log.info(f"RI ingested: {titulo} ({tipo})")
                        except Exception as e:
                            result = f"Error ingiriendo documento: {e}"
                            log.error(f"RI ingest error: {e}")

                    elif block.name == "agent_estado":
                        try:
                            st = get_agent_status(DB_PATH)
                            skills = list_skills(DB_PATH)
                            secrets_list = list_secrets(DB_PATH)
                            lines = [
                                f"DB: {st['db_path']} ({st['db_size_kb']} KB)",
                                f"Mensajes: {st['counts'].get('mensajes')} | Configs: {st['counts'].get('configs')} | Skills: {st['counts'].get('skills')} | Secrets: {st['counts'].get('secrets')}",
                                f"Docs KB: {st['counts'].get('documentos')} | Conceptos: {st['counts'].get('conceptos')}",
                                f"Perfiles astro: {st['counts'].get('perfiles_astro')}",
                            ]
                            if skills:
                                lines.append(f"Skills registrados: {', '.join(s['name'] for s in skills)}")
                            if secrets_list:
                                lines.append(f"Secrets: {', '.join(s['key'] for s in secrets_list)}")
                            if st["last_changes"]:
                                lines.append("Últimos cambios:")
                                for c in st["last_changes"][:3]:
                                    lines.append(f"  [{c['ts']}] {c['action'][:60]} → {c['status']}")
                            result = "\n".join(lines)
                        except Exception as e:
                            result = f"Error obteniendo estado: {e}"

                    elif block.name == "agent_changelog":
                        try:
                            limit = block.input.get("limit", 10)
                            changes = get_changelog(limit, DB_PATH)
                            if not changes:
                                result = "Sin cambios registrados aún."
                            else:
                                lines = [f"CHANGELOG ({len(changes)} entradas):"]
                                for c in changes:
                                    lines.append(f"[{c['ts']}] {c['action'][:70]}")
                                    if c['result']:
                                        lines.append(f"  → {c['result'][:60]}")
                                    if c['requires']:
                                        lines.append(f"  ⚠ requiere: {c['requires']}")
                                result = "\n".join(lines)
                        except Exception as e:
                            result = f"Error: {e}"

                    elif block.name == "agent_guardar_secret":
                        try:
                            key_name = block.input["key_name"].upper()
                            value    = block.input["value"]
                            service  = block.input.get("service", "")
                            desc     = block.input.get("description", "")
                            masked   = store_secret(key_name, value, service, desc, DB_PATH)
                            log_change(
                                instruction=f"Guardar secret {key_name}",
                                action=f"Secret '{key_name}' guardado para {service or 'servicio desconocido'}",
                                result=f"Valor: {masked}",
                                status="done",
                                chat_id=chat_id,
                                db_path=DB_PATH
                            )
                            result = f"Secret '{key_name}' guardado. Valor: {masked}. Servicio: {service or '—'}."
                            log.info(f"[{chat_id}] Secret guardado: {key_name} ({masked})")
                        except Exception as e:
                            result = f"Error guardando secret: {e}"

                    elif block.name == "agent_registrar_skill":
                        try:
                            name    = block.input["name"]
                            desc    = block.input["description"]
                            triggers = block.input.get("triggers", [])
                            config  = block.input.get("config", {})
                            register_skill(name, desc, triggers, config=config, db_path=DB_PATH)
                            log_change(
                                instruction=f"Registrar skill {name}",
                                action=f"Skill '{name}' registrado",
                                result=f"Triggers: {triggers}",
                                status="done",
                                chat_id=chat_id,
                                db_path=DB_PATH
                            )
                            result = f"Skill '{name}' registrado. Descripción: {desc}. Triggers: {triggers}."
                        except Exception as e:
                            result = f"Error registrando skill: {e}"

                    elif block.name == "agent_log":
                        try:
                            log_change(
                                instruction=block.input["instruction"],
                                action=block.input["action"],
                                result=block.input["result"],
                                status=block.input.get("status", "done"),
                                requires=block.input.get("requires"),
                                chat_id=chat_id,
                                db_path=DB_PATH
                            )
                            result = "Cambio registrado en changelog."
                        except Exception as e:
                            result = f"Error: {e}"

                    elif block.name == "vps_exec":
                        try:
                            from modules.ssh import execute as execute_ssh_command
                            cmd     = block.input["command"]
                            timeout = block.input.get("timeout", 30)
                            log.info(f"[{chat_id}] VPS exec: {cmd[:80]}")
                            res = execute_ssh_command(cmd, timeout=timeout)
                            if res["success"]:
                                out = res["stdout"].strip() or "(sin output)"
                                result = out[:3000]
                            else:
                                err = res["error"] or res["stderr"]
                                result = f"Error (exit {res['exit_code']}): {err[:500]}"
                            log_change(instruction=cmd, action=f"vps_exec: {cmd[:60]}",
                                       result=result[:200], chat_id=chat_id, db_path=DB_PATH)
                            vps_tools_used += 1
                        except Exception as e:
                            result = f"Error SSH: {e}"
                            log.error(f"vps_exec error: {e}")

                    elif block.name == "vps_leer_archivo":
                        try:
                            from modules.ssh import read_file as read_file_sftp
                            path = block.input["path"]
                            log.info(f"[{chat_id}] VPS leer: {path}")
                            res = read_file_sftp(path)
                            if res["success"]:
                                content = res["content"]
                                result = content[:4000] + ("\n...(truncado)" if len(content) > 4000 else "")
                            else:
                                result = f"Error leyendo {path}: {res['error']}"
                        except Exception as e:
                            result = f"Error SFTP read: {e}"
                            log.error(f"vps_leer error: {e}")

                    elif block.name == "vps_escribir_archivo":
                        try:
                            from modules.ssh import write_file as write_file_sftp
                            path    = block.input["path"]
                            content = block.input["content"]
                            log.info(f"[{chat_id}] VPS escribir: {path} ({len(content)} chars)")
                            res = write_file_sftp(path, content)
                            if res["success"]:
                                result = f"Archivo escrito: {path} ({res['bytes']} bytes)"
                                log_change(instruction=f"escribir {path}",
                                           action=f"vps_escribir_archivo: {path}",
                                           result=result, chat_id=chat_id, db_path=DB_PATH)
                            else:
                                result = f"Error escribiendo {path}: {res['error']}"
                        except Exception as e:
                            result = f"Error SFTP write: {e}"
                            log.error(f"vps_escribir error: {e}")

                    elif block.name == "vps_docker":
                        try:
                            from modules.ssh import execute as execute_ssh_command
                            action    = block.input["action"]
                            container = block.input.get("container", "")
                            tail      = block.input.get("tail", 50)
                            cmd_map = {
                                "ps":      "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'",
                                "stats":   "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'",
                                "restart": f"docker restart {container}",
                                "stop":    f"docker stop {container}",
                                "start":   f"docker start {container}",
                                "logs":    f"docker logs {container} --tail {tail}",
                                "inspect": f"docker inspect {container}",
                            }
                            cmd = cmd_map.get(action)
                            if not cmd:
                                result = f"Acción no reconocida: {action}. Opciones: ps, stats, restart, stop, start, logs, inspect"
                            else:
                                log.info(f"[{chat_id}] Docker {action}: {container or 'all'}")
                                res = execute_ssh_command(cmd, timeout=30)
                                if res["success"]:
                                    out = res["stdout"].strip() or "(sin output)"
                                    result = out[:3000]
                                else:
                                    result = f"Error docker {action}: {res['error'] or res['stderr']}"
                                log_change(instruction=f"docker {action} {container}",
                                           action=cmd, result=result[:200],
                                           chat_id=chat_id, db_path=DB_PATH)
                        except Exception as e:
                            result = f"Error docker: {e}"
                            log.error(f"vps_docker error: {e}")

                    elif block.name == "outlook_inbox":
                        try:
                            result = outlook_inbox(
                                user=block.input["user"],
                                tenant=block.input.get("tenant", "reamerica"),
                                days=block.input.get("days", 7),
                                top=block.input.get("top", 20),
                                unread=block.input.get("unread", False),
                            )
                        except Exception as e:
                            result = f"Error outlook_inbox: {e}"
                            log.error(f"outlook_inbox error: {e}")

                    elif block.name == "outlook_leer":
                        try:
                            result = outlook_leer(
                                user=block.input["user"],
                                message_id=block.input["message_id"],
                                tenant=block.input.get("tenant", "reamerica"),
                            )
                        except Exception as e:
                            result = f"Error outlook_leer: {e}"
                            log.error(f"outlook_leer error: {e}")

                    elif block.name == "outlook_buscar":
                        try:
                            result = outlook_buscar(
                                user=block.input["user"],
                                query=block.input["query"],
                                tenant=block.input.get("tenant", "reamerica"),
                                top=block.input.get("top", 15),
                            )
                        except Exception as e:
                            result = f"Error outlook_buscar: {e}"
                            log.error(f"outlook_buscar error: {e}")

                    elif block.name == "outlook_enviar":
                        try:
                            result = outlook_enviar(
                                from_user=block.input["from_user"],
                                to=block.input["to"],
                                subject=block.input["subject"],
                                body_html=block.input["body_html"],
                                tenant=block.input.get("tenant", "reamerica"),
                                cc=block.input.get("cc"),
                            )
                        except Exception as e:
                            result = f"Error outlook_enviar: {e}"
                            log.error(f"outlook_enviar error: {e}")

                    else:
                        result = "Herramienta no reconocida."

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "user", "content": tool_results})

            # Si usó 2+ tools de VPS, forzar respuesta en la próxima iteración
            if vps_tools_used >= 2:
                messages.append({"role": "user", "content": "Respondé con el resultado de lo que ejecutaste. No uses más herramientas."})
                vps_tools_used = 0

        else:
            # Respuesta final — extraer texto
            text_parts = []
            for block in response.content:
                if hasattr(block, "text") and block.text.strip():
                    text_parts.append(block.text.strip())
            if text_parts:
                last_text = "\n".join(text_parts)
                return last_text + _trace_footer(), pdf_path, extra_files
            # Claude terminó sin texto — forzar una respuesta
            messages.append({"role": "user", "content": "Resumí en 1-2 líneas qué hiciste y cuál fue el resultado."})
            force_resp = claude.messages.create(
                model="claude-opus-4-5",
                max_tokens=512,
                system=get_system_prompt(chat_id=chat_id, intent=_intent),
                messages=messages
            )
            try:
                _u = getattr(force_resp, "usage", None)
                if _u:
                    _tokens_in  += getattr(_u, "input_tokens", 0) or 0
                    _tokens_out += getattr(_u, "output_tokens", 0) or 0
            except Exception:
                pass
            for block in force_resp.content:
                if hasattr(block, "text") and block.text.strip():
                    return block.text.strip() + _trace_footer(), pdf_path, extra_files
            if last_text:
                return last_text + _trace_footer(), pdf_path, extra_files
            return "Listo — ejecuté el comando en el VPS." + _trace_footer(), pdf_path, extra_files

    # Límite de iteraciones alcanzado — devolver último texto o error
    if last_text:
        return last_text + _trace_footer(), pdf_path, extra_files
    return "Alcancé el límite de operaciones. Intentá con una instrucción más específica." + _trace_footer(), pdf_path, extra_files

# ── Handlers Telegram ──────────────────────────────────────────────────────────
def _detect_confirmation_question(text: str):
    """
    Detecta botones SOLO cuando Claude los pide explícitamente con [BOTONES: op1 | op2].
    NO detecta preguntas implícitas — eso generaba botones en conversaciones normales.
    """
    import re
    match = re.search(r'\[BOTONES:\s*([^\]]+)\]', text, re.IGNORECASE)
    if match:
        opciones = [o.strip() for o in match.group(1).split('|') if o.strip()]
        if opciones:
            return ("custom", opciones)
    return None


async def send_long_message(bot, chat_id: int, text: str, reply_to=None, chunk_size: int = 3900):
    """Envía texto largo dividiéndolo en mensajes por secciones o por tamaño."""
    MAX_CHUNKS = 5  # Máximo 5 mensajes para evitar spam

    if len(text) <= chunk_size:
        chunks = [text]
    else:
        # Intentar cortar en saltos de sección (##) o en párrafos
        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > chunk_size and current:
                parts.append(current.strip())
                current = ""
            current += line + "\n"
        if current.strip():
            parts.append(current.strip())
        chunks = parts if parts else [text[:chunk_size]]

    # Limitar chunks
    if len(chunks) > MAX_CHUNKS:
        chunks = chunks[:MAX_CHUNKS]
        chunks[-1] += f"\n\n_(mensaje truncado — {MAX_CHUNKS} partes máx)_"

    # Detectar si el último chunk tiene botones
    import re as _re_btn
    last_chunk_clean = _re_btn.sub(r'\[BOTONES:[^\]]*\]', '', chunks[-1], flags=_re_btn.IGNORECASE).strip()
    confirmation = _detect_confirmation_question(chunks[-1])

    # Limpiar el bloque [BOTONES:] del texto visible
    if confirmation and confirmation[0] == "custom":
        chunks[-1] = last_chunk_clean

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        reply_markup = None

        if is_last and confirmation:
            tipo, opciones = confirmation
            # Construir teclado — máx 2 botones por fila
            filas = []
            fila_actual = []
            for j, op in enumerate(opciones):
                # callback_data: confirm:0, confirm:1, confirm:2...
                fila_actual.append(InlineKeyboardButton(op, callback_data=f"confirm:{j}:{op[:30]}"))
                if len(fila_actual) == 2:
                    filas.append(fila_actual)
                    fila_actual = []
            if fila_actual:
                filas.append(fila_actual)
            reply_markup = InlineKeyboardMarkup(filas)

        try:
            if reply_to and i == 0:
                await reply_to.reply_text(chunk, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=chat_id, text=chunk, reply_markup=reply_markup)
        except Exception:
            import re as _re
            plain = _re.sub(r'[*_`\[\]()~>#+\-=|{}.!]', '', chunk)
            try:
                if reply_to and i == 0:
                    await reply_to.reply_text(plain, reply_markup=reply_markup)
                else:
                    await bot.send_message(chat_id=chat_id, text=plain, reply_markup=reply_markup)
            except Exception:
                if reply_to and i == 0:
                    await reply_to.reply_text(plain)
                else:
                    await bot.send_message(chat_id=chat_id, text=plain)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, threading, queue  # time ya está importado al top-level
    chat_id  = update.effective_chat.id
    user_msg = update.message.text
    name     = update.effective_user.first_name or "Usuario"
    msg_id   = update.message.message_id

    # Deduplicación — ignorar si ya procesamos este mensaje
    if not hasattr(context.application, "_processed_ids"):
        context.application._processed_ids = set()
    if msg_id in context.application._processed_ids:
        log.warning(f"[{chat_id}] Mensaje duplicado ignorado: {msg_id}")
        return
    context.application._processed_ids.add(msg_id)
    # Limpiar cache si crece demasiado
    if len(context.application._processed_ids) > 500:
        context.application._processed_ids = set(list(context.application._processed_ids)[-200:])

    log.info(f"[{chat_id}] {name}: {user_msg[:80]}{'...' if len(user_msg)>80 else ''}")

    # Trigger natural para el menú
    msg_lower = user_msg.strip().lower()
    if msg_lower in ("menu", "menú", "abri el menu", "abrí el menú", "ver menu", "ver menú"):
        await cmd_menu(update, context)
        return
    if msg_lower in ("biblioteca", "librería", "libreria", "knowledge base", "kb", "kb reaseguros"):
        await cmd_biblioteca(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        q = queue.Queue()

        def run_claude():
            try:
                pidio_voz = any(w in user_msg.lower() for w in
                    ["voz", "audio", "escuchar", "hablame", "háblame",
                     "respondé con voz", "responde con voz", "mandame un audio", "en audio"])
                q.put(("ok", ask_claude(chat_id, user_msg, user_name=name, allow_voice=pidio_voz)))
            except Exception as e:
                q.put(("err", str(e)))

        t = threading.Thread(target=run_claude, daemon=True)
        t.start()

        elapsed = 0
        while t.is_alive() and elapsed < 180:
            await asyncio.sleep(4)
            elapsed += 4
            if t.is_alive():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except:
                    pass

        t.join(timeout=1)
        if t.is_alive():
            save_message_full(chat_id, "user", user_msg, db_path=DB_PATH)
            await update.message.reply_text("Tardo demasiado, intentalo de nuevo.")
            return

        status, payload = q.get(timeout=2)
        if status == "err":
            raise Exception(payload)

        reply, pdf_path, extra_files = payload
        save_message_full(chat_id, "user",      user_msg, db_path=DB_PATH)
        save_message_full(chat_id, "assistant", reply,    db_path=DB_PATH)

        # Enviar respuesta de texto
        await send_long_message(context.bot, chat_id, reply, reply_to=update.message)

        if pdf_path:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f,
                    filename="carta_natal.pdf", caption="Ficha tecnica - Carta Natal")

        # Verificar si el usuario pidió voz explícitamente
        _pidio_voz = any(w in user_msg.lower() for w in
            ["voz", "audio", "escuchar", "hablame", "háblame",
             "respondé con voz", "responde con voz", "mandame un audio"])

        log.info(f"[{chat_id}] extra_files: {[(n,cap,len(c)) for n,c,cap in extra_files]} | pidio_voz={_pidio_voz}")
        for nombre_f, contenido, caption in extra_files:
            try:
                if caption == "voice":
                    if not _pidio_voz:
                        log.info(f"[{chat_id}] Voz ignorada (usuario no la pidió)")
                        continue
                    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
                    await context.bot.send_voice(chat_id=chat_id, voice=io.BytesIO(contenido))
                    log.info(f"[{chat_id}] Voz enviada OK: {len(contenido)} bytes")
                elif caption == "video_link":
                    # Mandar como link con preview de Telegram
                    info_txt = contenido.decode()
                    lines = info_txt.split("\n")
                    titulo_v = lines[0] if lines else "Video"
                    link_v   = lines[1] if len(lines) > 1 else ""
                    meta_v   = lines[2] if len(lines) > 2 else ""
                    msg = f"{titulo_v}\n{meta_v}\n\n{link_v}" if meta_v else f"{titulo_v}\n{link_v}"
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                    log.info(f"[{chat_id}] Video link enviado: {titulo_v}")
                elif caption.startswith("video|"):
                    titulo_vid = caption.split("|", 1)[1]
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
                    await context.bot.send_video(chat_id=chat_id,
                        video=io.BytesIO(contenido), filename=nombre_f,
                        caption=titulo_vid, supports_streaming=True)
                    log.info(f"[{chat_id}] Video enviado: {len(contenido)//1024//1024}MB")
                else:
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await context.bot.send_document(chat_id=chat_id,
                        document=io.BytesIO(contenido), filename=nombre_f, caption=caption)
            except Exception as ve:
                log.error(f"[{chat_id}] Error enviando {caption}: {ve}")

        log.info(f"[{chat_id}] Bot: {reply[:80]}...")
    except Exception as e:
        log.error(f"Error: {e}")
        await update.message.reply_text("Tardo demasiado o hubo un error, intentalo de nuevo.")

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, tempfile, subprocess, threading, queue, sys
    chat_id = update.effective_chat.id
    name    = update.effective_user.first_name or "Usuario"

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Descargar audio
        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        # Transcribir con subproceso async nativo (no bloquea el event loop)
        log.info(f"[{chat_id}] Transcribiendo audio de {name}...")
        _bot_dir = os.path.dirname(os.path.abspath(__file__))
        _transcribe_script = os.path.join(_bot_dir, "transcribe.py")

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, _transcribe_script, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
                texto = stdout.decode().strip()
            except asyncio.TimeoutError:
                proc.kill()
                texto = ""
        except Exception as e:
            log.error(f"Error subproceso transcripcion: {e}")
            texto = ""
        finally:
            try:
                os.unlink(tmp_path)
            except:
                pass

        if not texto or texto.startswith("ERROR:"):
            await update.message.reply_text("No pude entender el audio, manda de nuevo.")
            return

        log.info(f"[{chat_id}] Transcripcion: {texto}")
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Procesar con Claude en hilo con queue
        q = queue.Queue()

        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, texto, user_name=name, allow_voice=True)))
            except Exception as e:
                q.put(("err", str(e)))

        t = threading.Thread(target=run_claude, daemon=True)
        t.start()

        # Esperar con typing cada 4s
        elapsed = 0
        while t.is_alive() and elapsed < 180:
            await asyncio.sleep(4)
            elapsed += 4
            if t.is_alive():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except:
                    pass

        t.join(timeout=1)
        if t.is_alive():
            save_message_full(chat_id, "user", texto, db_path=DB_PATH)
            await update.message.reply_text("Tardo demasiado, intentalo de nuevo.")
            return

        status, payload = q.get(timeout=2)
        if status == "err":
            raise Exception(payload)

        reply, pdf_path, extra_files = payload
        save_message_full(chat_id, "user",      texto, db_path=DB_PATH)
        save_message_full(chat_id, "assistant", reply, db_path=DB_PATH)

        # El usuario mandó audio → siempre intentar responder con voz
        tiene_voz = any(cap == "voice" for _, _, cap in extra_files)

        if not tiene_voz and reply and not es_respuesta_larga(reply):
            # Claude no llamó el tool o falló — generar voz del reply directamente
            log.info(f"[{chat_id}] Generando voz del reply directamente")
            ogg_path = texto_a_voz(reply)
            if ogg_path:
                with open(ogg_path, "rb") as f:
                    contenido_voz = f.read()
                os.unlink(ogg_path)
                extra_files.append(("respuesta.ogg", contenido_voz, "voice"))
                tiene_voz = True

        # Enviar texto solo si no hay voz (o si la respuesta es larga)
        if not tiene_voz:
            await send_long_message(context.bot, chat_id, reply, reply_to=update.message)

        if pdf_path:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f,
                    filename="carta_natal.pdf", caption="Ficha tecnica - Carta Natal")

        log.info(f"[{chat_id}] handle_voice extra_files: {[(n,cap,len(c)) for n,c,cap in extra_files]}")
        for nombre_f, contenido, caption in extra_files:
            try:
                if caption == "voice":
                    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
                    await context.bot.send_voice(chat_id=chat_id, voice=io.BytesIO(contenido))
                    log.info(f"[{chat_id}] Voz enviada OK: {len(contenido)} bytes")
                elif caption == "video_link":
                    # Mandar como link con preview de Telegram
                    info_txt = contenido.decode()
                    lines = info_txt.split("\n")
                    titulo_v = lines[0] if lines else "Video"
                    link_v   = lines[1] if len(lines) > 1 else ""
                    meta_v   = lines[2] if len(lines) > 2 else ""
                    msg = f"{titulo_v}\n{meta_v}\n\n{link_v}" if meta_v else f"{titulo_v}\n{link_v}"
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                    log.info(f"[{chat_id}] Video link enviado: {titulo_v}")
                elif caption.startswith("video|"):
                    titulo_vid = caption.split("|", 1)[1]
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
                    await context.bot.send_video(chat_id=chat_id,
                        video=io.BytesIO(contenido), filename=nombre_f,
                        caption=titulo_vid, supports_streaming=True)
                    log.info(f"[{chat_id}] Video enviado: {len(contenido)//1024//1024}MB")
                else:
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await context.bot.send_document(chat_id=chat_id,
                        document=io.BytesIO(contenido), filename=nombre_f, caption=caption)
            except Exception as ve:
                log.error(f"[{chat_id}] Error enviando {caption}: {ve}")

    except Exception as e:
        log.error(f"Error en voz: {e}")
        await update.message.reply_text("No pude procesar el audio, intentalo de nuevo.")

async def cmd_voz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menú para elegir la voz del bot."""
    chat_id = update.effective_chat.id
    activa  = get_voz_activa()
    botones = []
    for voice_id, nombre, _ in VOCES_CATALOG:
        marca = "✅ " if voice_id == activa else ""
        botones.append([InlineKeyboardButton(f"{marca}{nombre}", callback_data=f"voz:set:{voice_id}")])
    botones.append([InlineKeyboardButton("🎧 Escuchar preview", callback_data="voz:preview")])
    botones.append([InlineKeyboardButton("Cerrar", callback_data="voz:cerrar")])
    await update.message.reply_text(
        "Elegí la voz del bot:",
        reply_markup=InlineKeyboardMarkup(botones)
    )

async def handle_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los botones de confirmación generados automáticamente."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    name = query.from_user.first_name or "Usuario"

    # data format: confirm:INDEX:TEXTO
    parts = query.data.split(":", 2)
    opcion_texto = parts[2] if len(parts) > 2 else parts[1]

    # Limpiar emojis de la opción para el mensaje
    opcion_limpia = opcion_texto.strip()

    # Editar el mensaje original para quitar los botones
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # Mostrar qué eligió el usuario
    await query.message.reply_text(f"→ {opcion_limpia}")

    # Pasar la elección a Claude como si fuera un mensaje normal
    import threading, queue as _queue
    q = _queue.Queue()

    def run_claude():
        try:
            q.put(("ok", ask_claude(chat_id, opcion_limpia, user_name=name)))
        except Exception as e:
            q.put(("err", str(e)))

    t = threading.Thread(target=run_claude, daemon=True)
    t.start()

    import asyncio
    elapsed = 0
    while t.is_alive() and elapsed < 180:
        await asyncio.sleep(3)
        elapsed += 3
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

    t.join(timeout=1)
    if t.is_alive():
        await query.message.reply_text("Tardó demasiado, intentalo de nuevo.")
        return

    status, payload = q.get(timeout=2)
    if status == "err":
        await query.message.reply_text(f"Error: {payload}")
        return

    reply, pdf_path, extra_files = payload
    save_message_full(chat_id, "user", opcion_limpia, db_path=DB_PATH)
    save_message_full(chat_id, "assistant", reply, db_path=DB_PATH)
    await send_long_message(context.bot, chat_id, reply)

    if pdf_path:
        import io as _io
        with open(pdf_path, "rb") as f:
            await context.bot.send_document(chat_id=chat_id, document=f, filename="documento.pdf")

    for nombre_f, contenido, caption in extra_files:
        try:
            import io as _io
            if caption == "voice":
                await context.bot.send_voice(chat_id=chat_id, voice=_io.BytesIO(contenido))
            else:
                await context.bot.send_document(chat_id=chat_id,
                    document=_io.BytesIO(contenido), filename=nombre_f, caption=caption)
        except Exception as ve:
            log.error(f"Error enviando extra en confirm: {ve}")

async def handle_voz_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data.startswith("voz:set:"):
        voice_id = data.split(":", 2)[2]
        nombre   = next((n for vid, n, _ in VOCES_CATALOG if vid == voice_id), voice_id)
        set_voz_activa(voice_id, DB_PATH)
        # Reconstruir teclado con nueva selección
        botones = []
        for vid, nom, _ in VOCES_CATALOG:
            marca = "✅ " if vid == voice_id else ""
            botones.append([InlineKeyboardButton(f"{marca}{nom}", callback_data=f"voz:set:{vid}")])
        botones.append([InlineKeyboardButton("🎧 Escuchar preview", callback_data="voz:preview")])
        botones.append([InlineKeyboardButton("Cerrar", callback_data="voz:cerrar")])
        await query.edit_message_text(
            f"Voz cambiada a: {nombre}",
            reply_markup=InlineKeyboardMarkup(botones)
        )
        log.info(f"[{chat_id}] Voz cambiada a {voice_id} ({nombre})")

    elif data == "voz:preview":
        await query.edit_message_text("Generando preview...")
        await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
        ogg = texto_a_voz("Hola, esta es la voz activa del bot. ¿Cómo suena?")
        if ogg:
            with open(ogg, "rb") as f:
                await context.bot.send_voice(chat_id=chat_id, voice=f,
                    caption=f"Voz activa: {next((n for vid,n,_ in VOCES_CATALOG if vid==get_voz_activa()), get_voz_activa())}")
            os.unlink(ogg)
        else:
            await context.bot.send_message(chat_id=chat_id, text="No pude generar el preview.")
        # Restaurar menú
        activa = get_voz_activa()
        botones = []
        for vid, nom, _ in VOCES_CATALOG:
            marca = "✅ " if vid == activa else ""
            botones.append([InlineKeyboardButton(f"{marca}{nom}", callback_data=f"voz:set:{vid}")])
        botones.append([InlineKeyboardButton("🎧 Escuchar preview", callback_data="voz:preview")])
        botones.append([InlineKeyboardButton("Cerrar", callback_data="voz:cerrar")])
        await context.bot.send_message(chat_id=chat_id, text="Elegí la voz:",
            reply_markup=InlineKeyboardMarkup(botones))

    elif data == "voz:cerrar":
        await query.edit_message_text("Menú de voz cerrado.")

async def cmd_testvoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de diagnóstico: genera y manda un audio de prueba."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
    try:
        ogg = texto_a_voz("Hola, la voz funciona correctamente.")
        if ogg:
            with open(ogg, "rb") as f:
                await context.bot.send_voice(chat_id=chat_id, voice=f)
            os.unlink(ogg)
            log.info(f"[{chat_id}] /testvoice OK")
        else:
            await update.message.reply_text("TTS falló: texto_a_voz retornó None")
    except Exception as e:
        log.error(f"[{chat_id}] /testvoice error: {e}")
        await update.message.reply_text(f"Error en voz: {e}")



# ── BIBLIOTECA — menú de knowledge base ───────────────────────────────────────
def _kb_back(rows, label="Volver", back="lib:main"):
    return rows + [[InlineKeyboardButton(f"← {label}", callback_data=back)]]

async def cmd_biblioteca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _lib_show_main(update.message, context, is_query=False)

async def _lib_show_main(target, context, is_query=False):
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("REASEGUROS",      callback_data="lib:ri:main")],
        [InlineKeyboardButton("ASTROLOGÍA",      callback_data="lib:astro:main")],
        [InlineKeyboardButton("TODOS LOS DOCS",  callback_data="lib:docs:all")],
        [InlineKeyboardButton("BUSCAR",          callback_data="lib:search")],
    ])
    txt = "BIBLIOTECA — elegí una sección:"
    if is_query:
        await target.edit_message_text(txt, reply_markup=teclado)
    else:
        await target.reply_text(txt, reply_markup=teclado)

async def handle_biblioteca_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    if data == "lib:main":
        await _lib_show_main(query, context, is_query=True)

    elif data == "lib:ri:main":
        teclado = InlineKeyboardMarkup(_kb_back([
            [InlineKeyboardButton("Conceptos clave",        callback_data="lib:ri:conceptos")],
            [InlineKeyboardButton("Estructuras de tratado", callback_data="lib:ri:estructuras")],
            [InlineKeyboardButton("Claims y siniestros",    callback_data="lib:ri:claims")],
            [InlineKeyboardButton("Underwriting / pricing", callback_data="lib:ri:uw")],
            [InlineKeyboardButton("Clausulas y wordings",   callback_data="lib:ri:wordings")],
            [InlineKeyboardButton("Normativa",              callback_data="lib:ri:normativa")],
            [InlineKeyboardButton("Buscar en reaseguros",   callback_data="lib:ri:search")],
        ]))
        await query.edit_message_text("REASEGUROS:", reply_markup=teclado)

    elif data.startswith("lib:ri:") and len(data.split(":")) == 3 and data.split(":")[2] in ("conceptos","estructuras","claims","uw","wordings","normativa"):
        dominio_map = {
            "conceptos":   ("general",       "Conceptos clave"),
            "estructuras": ("treaty",        "Estructuras — Quota Share, XoL, Stop Loss"),
            "claims":      ("claims",        "Claims y siniestros"),
            "uw":          ("underwriting",  "Underwriting y pricing"),
            "wordings":    ("wording",       "Clausulas y wordings"),
            "normativa":   ("normativa",     "Normativa"),
        }
        sub = data.split(":")[2]
        domain, titulo = dominio_map[sub]
        res = search_knowledge(domain, limit=12, db_path=DB_PATH)
        conceptos = res.get("concepts", [])[:10]
        if not conceptos:
            await query.edit_message_text(
                f"{titulo}\n\nAun no hay contenido indexado. Carga documentos con el bot.",
                reply_markup=InlineKeyboardMarkup(_kb_back([]))
            )
            return
        if not hasattr(context, '_lib_cache'):
            context._lib_cache = {}
        cache_key = f"ri_{sub}_{chat_id}"
        context._lib_cache[cache_key] = conceptos
        botones = []
        for i, c in enumerate(conceptos):
            botones.append([InlineKeyboardButton(c["term"][:45], callback_data=f"lib:ri:c:{i}:{sub}")])
        await query.edit_message_text(f"{titulo}:",
            reply_markup=InlineKeyboardMarkup(_kb_back(botones, back="lib:ri:main")))

    elif data.startswith("lib:ri:c:"):
        parts = data.split(":")
        idx, sub = int(parts[3]), parts[4]
        cache_key = f"ri_{sub}_{chat_id}"
        cache = getattr(context, '_lib_cache', {})
        conceptos = cache.get(cache_key, [])
        if idx < len(conceptos):
            c = conceptos[idx]
            txt = f"{c['term']}\n\n{c['definition']}\n\nDOMINIO: {c.get('domain','—')} | FUENTE: {c.get('source','—')}"
        else:
            txt = "Concepto no encontrado."
        await query.edit_message_text(txt[:3800],
            reply_markup=InlineKeyboardMarkup(_kb_back([], back=f"lib:ri:{sub}")))

    elif data == "lib:ri:search":
        context.user_data["lib_search_mode"] = "ri"
        await query.edit_message_text("Escribi lo que queres buscar en reaseguros:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="lib:ri:main")]]))

    elif data.startswith("lib:docs:"):
        tipo = None if data == "lib:docs:all" else data.split(":")[-1]
        docs = get_document_list(tipo, DB_PATH)
        if not docs:
            await query.edit_message_text("No hay documentos indexados.",
                reply_markup=InlineKeyboardMarkup(_kb_back([])))
            return
        icons = {"doctrine":"📖","wording":"📋","regulation":"⚖️","operational":"⚙️"}
        botones = []
        for d in docs[:15]:
            icon = icons.get(d["type"], "📄")
            botones.append([InlineKeyboardButton(f"{icon} {d['title'][:38]}", callback_data=f"lib:doc:{d['id']}")])
        filt_botones = [
            [InlineKeyboardButton("📖 Doctrina",  callback_data="lib:docs:doctrine"),
             InlineKeyboardButton("📋 Wordings",  callback_data="lib:docs:wording")],
            [InlineKeyboardButton("⚖️ Normativa", callback_data="lib:docs:regulation"),
             InlineKeyboardButton("⚙️ Operativo", callback_data="lib:docs:operational")],
        ]
        await query.edit_message_text(f"DOCUMENTOS ({len(docs)}):",
            reply_markup=InlineKeyboardMarkup(_kb_back(filt_botones + botones)))

    elif data.startswith("lib:doc:") and len(data.split(":")) == 3:
        doc_id = int(data.split(":")[2])
        import sqlite3 as _sq3, json as _j
        con = _sq3.connect(DB_PATH)
        con.row_factory = _sq3.Row
        doc = con.execute("SELECT * FROM ri_documents WHERE id=?", (doc_id,)).fetchone()
        summ = con.execute("SELECT * FROM ri_summaries WHERE doc_id=?", (doc_id,)).fetchone()
        nc = con.execute("SELECT COUNT(*) FROM ri_concepts WHERE doc_id=?", (doc_id,)).fetchone()[0]
        nq = con.execute("SELECT COUNT(*) FROM ri_qa WHERE doc_id=?", (doc_id,)).fetchone()[0]
        con.close()
        if not doc:
            await query.edit_message_text("Documento no encontrado.")
            return
        txt = f"{doc['title']}\nTipo: {doc['source_type']} | Org: {doc['source_org'] or '—'}\nConceptos: {nc} | QA: {nq}\n\n"
        if summ and summ['executive']:
            txt += f"RESUMEN:\n{summ['executive']}\n\n"
            if summ['key_points']:
                pts = _j.loads(summ['key_points'])
                txt += "PUNTOS CLAVE:\n" + "\n".join(f"• {p}" for p in pts[:4])
        botones = [
            [InlineKeyboardButton("Ver conceptos", callback_data=f"lib:doc_c:{doc_id}"),
             InlineKeyboardButton("Ver QA",        callback_data=f"lib:doc_q:{doc_id}")],
            [InlineKeyboardButton("Buscar en este doc", callback_data=f"lib:doc_s:{doc_id}")],
        ]
        await query.edit_message_text(txt[:3800],
            reply_markup=InlineKeyboardMarkup(_kb_back(botones, back="lib:docs:all")))

    elif data.startswith("lib:doc_c:"):
        doc_id = int(data.split(":")[2])
        import sqlite3 as _sq3
        con = _sq3.connect(DB_PATH)
        rows = con.execute("SELECT term, definition FROM ri_concepts WHERE doc_id=? LIMIT 12", (doc_id,)).fetchall()
        con.close()
        if not rows:
            txt = "No hay conceptos indexados para este documento."
        else:
            txt = "CONCEPTOS:\n\n" + "\n\n".join(f"• {r[0]}: {r[1][:150]}" for r in rows)
        await query.edit_message_text(txt[:3800],
            reply_markup=InlineKeyboardMarkup(_kb_back([], back=f"lib:doc:{doc_id}")))

    elif data.startswith("lib:doc_q:"):
        doc_id = int(data.split(":")[2])
        import sqlite3 as _sq3
        con = _sq3.connect(DB_PATH)
        rows = con.execute("SELECT question, answer FROM ri_qa WHERE doc_id=? LIMIT 8", (doc_id,)).fetchall()
        con.close()
        if not rows:
            txt = "No hay QA indexado para este documento."
        else:
            txt = "Q&A:\n\n" + "\n\n".join(f"Q: {r[0]}\nA: {r[1][:200]}" for r in rows)
        await query.edit_message_text(txt[:3800],
            reply_markup=InlineKeyboardMarkup(_kb_back([], back=f"lib:doc:{doc_id}")))

    elif data.startswith("lib:doc_s:"):
        doc_id = data.split(":")[2]
        context.user_data["lib_search_mode"] = f"doc:{doc_id}"
        await query.edit_message_text("Escribi lo que queres buscar en este documento:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data=f"lib:doc:{doc_id}")]]))

    elif data == "lib:search":
        context.user_data["lib_search_mode"] = "all"
        await query.edit_message_text("BUSCADOR — escribi lo que queres buscar (busca en toda la biblioteca):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="lib:main")]]))

    elif data == "lib:astro:main":
        perfiles = astro_listar(chat_id)
        if not perfiles:
            await query.edit_message_text("No hay cartas astrologicas guardadas.",
                reply_markup=InlineKeyboardMarkup(_kb_back([])))
            return
        botones = [[InlineKeyboardButton(
            f"{p['nombre'].title()} — {p['fecha']}",
            callback_data=f"astro:ver:{p['nombre']}"
        )] for p in perfiles]
        await query.edit_message_text("ASTROLOGIA — cartas guardadas:",
            reply_markup=InlineKeyboardMarkup(_kb_back(botones)))

def _menu_main_keyboard():
    """Teclado principal reusable (cmd_menu + callback menu:main)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📧  Gmail",        callback_data="menu:gmail"),
         InlineKeyboardButton("📅  Calendario",   callback_data="menu:calendar")],
        [InlineKeyboardButton("⭐  Astrología",   callback_data="menu:astro"),
         InlineKeyboardButton("🎤  Voz",           callback_data="menu:voz")],
        [InlineKeyboardButton("🏢  Reamerica",    callback_data="menu:reamerica")],
        [InlineKeyboardButton("📚  Biblioteca",   callback_data="menu:biblioteca")],
        [InlineKeyboardButton("🔧  Sistema",       callback_data="menu:sistema")],
    ])


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menú principal con todos los comandos organizados."""
    await update.message.reply_text("¿Qué querés hacer?", reply_markup=_menu_main_keyboard())


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    BACK = [[InlineKeyboardButton("← Volver al menú", callback_data="menu:main")]]

    if data == "menu:main":
        await query.edit_message_text("¿Qué querés hacer?", reply_markup=_menu_main_keyboard())

    elif data == "menu:reamerica":
        # Submenu Reamerica — mismas opciones que /rma (callbacks sf:* y rma:*).
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊  Resumen general",    callback_data="rma:summary")],
            [InlineKeyboardButton("👥  Cuentas",            callback_data="sf:accounts"),
             InlineKeyboardButton("📞  Contactos",          callback_data="sf:contacts")],
            [InlineKeyboardButton("💼  Oportunidades",      callback_data="sf:opps"),
             InlineKeyboardButton("📈  Pipeline",           callback_data="sf:pipeline")],
            [InlineKeyboardButton("📜  Contratos",          callback_data="sf:obj:Contratos__c"),
             InlineKeyboardButton("📝  Endosos",            callback_data="sf:obj:Endosos__c")],
            [InlineKeyboardButton("🌎  Por industria",      callback_data="sf:industries"),
             InlineKeyboardButton("🌍  Por país",            callback_data="sf:countries")],
            [InlineKeyboardButton("👤  Brokers",            callback_data="rma:brokers")],
            [InlineKeyboardButton("📂  Listar objetos",     callback_data="sf:list")],
            [InlineKeyboardButton("⚡  Comandos directos",  callback_data="rma:tools")],
            *BACK
        ])
        await query.edit_message_text(
            "🏢 *REAMERICA RISK ADVISORS*\n_CRM + Brokers + Reportes (solo lectura)._",
            parse_mode="Markdown",
            reply_markup=teclado
        )

    elif data == "menu:gmail":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("Ver inbox",             callback_data="menu:act:gmail_inbox")],
            [InlineKeyboardButton("Mails de hoy",          callback_data="menu:act:gmail_hoy")],
            [InlineKeyboardButton("Mails no leídos",       callback_data="menu:act:gmail_unread")],
            [InlineKeyboardButton("Resumen de la semana",  callback_data="menu:act:gmail_semana")],
            [InlineKeyboardButton("Enviar un mail",        callback_data="menu:act:gmail_send")],
            *BACK
        ])
        await query.edit_message_text("📧 Gmail — ¿qué hacemos?", reply_markup=teclado)

    elif data == "menu:calendar":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("Ver esta semana",       callback_data="menu:act:cal_semana")],
            [InlineKeyboardButton("Ver este mes",          callback_data="menu:act:cal_mes")],
            [InlineKeyboardButton("Crear un evento",       callback_data="menu:act:cal_crear")],
            *BACK
        ])
        await query.edit_message_text("📅 Calendario — ¿qué hacemos?", reply_markup=teclado)

    elif data == "menu:astro":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("Ver mis cartas guardadas", callback_data="menu:act:astro_lista")],
            [InlineKeyboardButton("🌍 Calcular carta natal",   callback_data="menu:act:astro_calc")],
            [InlineKeyboardButton("☀️ Calcular retorno solar",  callback_data="menu:act:astro_solar")],
            [InlineKeyboardButton("🌙 Calcular retorno lunar",  callback_data="menu:act:astro_lunar")],
            [InlineKeyboardButton("Ficha técnica completa",   callback_data="menu:act:astro_ficha")],
            *BACK
        ])
        await query.edit_message_text("⭐ Astrología — ¿qué hacemos?", reply_markup=teclado)

    elif data == "menu:voz":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("Cambiar voz del bot",   callback_data="menu:act:voz_menu")],
            [InlineKeyboardButton("Escuchar voz actual",   callback_data="menu:act:voz_test")],
            *BACK
        ])
        await query.edit_message_text("🎤 Voz — ¿qué hacemos?", reply_markup=teclado)

    elif data == "menu:biblioteca":
        # Redirigir al handler de biblioteca mostrando el menú principal de KB
        await _lib_show_main(query, context, is_query=True)

    elif data == "menu:sistema":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("Ver configuraciones",   callback_data="menu:act:sys_config")],
            [InlineKeyboardButton("Ver perfiles astro",    callback_data="menu:act:sys_perfiles")],
            [InlineKeyboardButton("Estadísticas memoria",  callback_data="menu:act:sys_stats")],
            [InlineKeyboardButton("Borrar historial",      callback_data="menu:act:sys_reset")],
            *BACK
        ])
        await query.edit_message_text("🔧 Sistema — ¿qué hacemos?", reply_markup=teclado)

    elif data.startswith("menu:act:"):
        accion = data.split(":", 2)[2]
        acciones = {
            "gmail_inbox":  "Mostrá mis últimos mails del inbox",
            "gmail_hoy":    "Mostrá los mails que recibí hoy",
            "gmail_unread": "Mostrá mis mails no leídos",
            "gmail_semana": "Hacé un resumen de mis mails de la última semana",
            "gmail_send":   "Quiero enviar un mail",
            "cal_semana":   "Mostrá mis eventos de esta semana",
            "cal_mes":      "Mostrá mis eventos de este mes",
            "cal_crear":    "Quiero crear un evento en el calendario",
            "astro_lista":  "Mostrá la lista de cartas astrológicas guardadas",
            "astro_calc":   "Quiero calcular una carta natal",
            "astro_ficha":  "Quiero la ficha técnica completa de una carta",
        }
        if accion == "voz_menu":
            await query.edit_message_text("Abriendo menú de voz...")
            await cmd_voz_desde_callback(chat_id, context)
            return
        elif accion == "voz_test":
            await query.edit_message_text("Generando audio...")
            ogg = texto_a_voz("Hola, esta es la voz activa de Cuki.")
            if ogg:
                with open(ogg, "rb") as f:
                    await context.bot.send_voice(chat_id=chat_id, voice=f)
                os.unlink(ogg)
            return
        elif accion == "astro_lista":
            # Mostrar directamente el menú de cartas con botones
            await query.edit_message_text("Cargando cartas guardadas...")
            await menu_lista_cartas(query, context, chat_id)
            return
        elif accion == "astro_calc":
            await query.edit_message_text(
                "Para calcular una carta natal decime:\nFecha (DD/MM/AAAA), hora (HH:MM) y lugar de nacimiento."
            )
            return
        elif accion == "astro_solar":
            perfiles = astro_listar(chat_id)
            if not perfiles:
                await query.edit_message_text(
                    "Para calcular tu retorno solar primero necesito tu natal. "
                    "Decime fecha, hora y lugar de nacimiento."
                )
                return
            botones = [[InlineKeyboardButton(
                p['nombre'].title(), callback_data=f"astro:fichacapa:solar:{p['nombre']}"
            )] for p in perfiles]
            botones.append([InlineKeyboardButton("← Volver", callback_data="menu:astro")])
            await query.edit_message_text(
                "☀️ *Retorno Solar* — ¿de quién?\n\n"
                "_Si viajás fuera del lugar de nacimiento el día del cumpleaños, "
                "el retorno toma ese lugar. Si no me decís nada, uso el lugar natal._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(botones),
            )
            return
        elif accion == "astro_lunar":
            perfiles = astro_listar(chat_id)
            if not perfiles:
                await query.edit_message_text(
                    "Para el retorno lunar primero necesito tu natal. "
                    "Decime fecha, hora y lugar de nacimiento."
                )
                return
            botones = [[InlineKeyboardButton(
                p['nombre'].title(), callback_data=f"astro:fichacapa:lunar:{p['nombre']}"
            )] for p in perfiles]
            botones.append([InlineKeyboardButton("← Volver", callback_data="menu:astro")])
            await query.edit_message_text(
                "🌙 *Retorno Lunar* — ¿de quién?\n\n"
                "_El retorno lunar del mes se calcula automáticamente desde hoy. "
                "Podés pedirlo con lugar distinto al natal mencionándolo en un mensaje._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(botones),
            )
            return
        elif accion == "astro_ficha":
            perfiles = astro_listar(chat_id)
            if not perfiles:
                await query.edit_message_text("No hay cartas guardadas. Primero calculá una carta natal.")
                return
            # Llevar al menú de opciones de la persona (donde elige ficha técnica
            # → capa natal/solar/lunar → formato), no directo a una ficha fija.
            botones = [[InlineKeyboardButton(
                p['nombre'].title(), callback_data=f"astro:ver:{p['nombre']}"
            )] for p in perfiles]
            botones.append([InlineKeyboardButton("← Volver", callback_data="menu:astro")])
            await query.edit_message_text(
                "¿De quién querés la carta?",
                reply_markup=InlineKeyboardMarkup(botones)
            )
            return
        elif accion == "sys_config":
            configs = list_configs(db_path=DB_PATH)
            lines = [f"{c['namespace']}.{c['key']} v{c['version']}" for c in configs[:15]]
            await query.edit_message_text("Configuraciones:\n" + "\n".join(lines))
            return
        elif accion == "sys_perfiles":
            perfiles = astro_listar(chat_id)
            if not perfiles:
                await query.edit_message_text("No hay perfiles astrológicos guardados.")
            else:
                lines = [f"• {p['nombre'].title()} — {p['fecha']} {p['hora']}" for p in perfiles]
                await query.edit_message_text("Perfiles guardados:\n" + "\n".join(lines))
            return
        elif accion == "sys_stats":
            from memory_store import get_memory_stats
            stats = get_memory_stats(chat_id, DB_PATH)
            await query.edit_message_text(
                f"Memoria:\n• {stats['messages']} mensajes\n• {stats['sessions']} sesiones\n"
                f"• {stats['memory_facts']} hechos\n• {stats['persons']} personas"
            )
            return
        elif accion == "sys_reset":
            clear_history(chat_id)
            clear_chat_history(chat_id, DB_PATH)
            await query.edit_message_text("Historial borrado.")
            return

        # Para las acciones que generan texto: enviar como mensaje al chat
        texto_accion = acciones.get(accion, "")
        if texto_accion:
            await query.edit_message_text(f"Ejecutando: {texto_accion}...")
            await context.bot.send_message(chat_id=chat_id, text=texto_accion)


async def cmd_voz_desde_callback(chat_id: int, context):
    """Envía el menú de voz como mensaje nuevo."""
    activa  = get_voz_activa()
    botones = []
    for voice_id, nombre, _ in VOCES_CATALOG:
        marca = "✅ " if voice_id == activa else ""
        botones.append([InlineKeyboardButton(f"{marca}{nombre}", callback_data=f"voz:set:{voice_id}")])
    botones.append([InlineKeyboardButton("🎧 Escuchar preview", callback_data="voz:preview")])
    botones.append([InlineKeyboardButton("Cerrar", callback_data="voz:cerrar")])
    await context.bot.send_message(chat_id=chat_id, text="Elegí la voz del bot:",
        reply_markup=InlineKeyboardMarkup(botones))


BOT_VERSION = "v2026.04.13-novoz"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or ""
    await update.message.reply_text(
        f"Hola {name}! Soy Cukinator {BOT_VERSION}. "
        f"Puedo conversar, buscar en internet y calcular cartas natales. "
        f"Escribí 'menu' para ver todas las opciones."
    )

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    clear_history(chat_id)
    clear_chat_history(chat_id, DB_PATH)
    await update.message.reply_text("Historial borrado, empezamos de cero.")

# ── Menú inline de cartas astrológicas ────────────────────────────────────────
async def menu_lista_cartas(update_or_query, context, chat_id):
    perfiles = astro_listar(chat_id)
    if not perfiles:
        texto = "No hay cartas guardadas. Podés pedirme que calcule una."
        if hasattr(update_or_query, "message") and update_or_query.message:
            await update_or_query.message.reply_text(texto)
        else:
            await update_or_query.edit_message_text(texto)
        return
    botones = [
        [InlineKeyboardButton(p["nombre"].title(), callback_data=f"astro:ver:{p['nombre']}")]
        for p in perfiles
    ]
    botones.append([InlineKeyboardButton("Cerrar menú", callback_data="astro:cerrar")])
    teclado = InlineKeyboardMarkup(botones)
    if hasattr(update_or_query, "message") and update_or_query.message:
        await update_or_query.message.reply_text("¿De quién querés ver la carta?", reply_markup=teclado)
    else:
        await update_or_query.edit_message_text("¿De quién querés ver la carta?", reply_markup=teclado)

def _save_astro_output(chat_id: int, nombre: str, tipo: str, contenido: str) -> None:
    """Guarda automáticamente cualquier output astrológico (ficha, explicación,
    perspectiva, tránsitos, triple capa) en el historial del perfil. Nunca
    pregunta — es append-only por diseño."""
    try:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            CREATE TABLE IF NOT EXISTS astro_outputs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    BIGINT NOT NULL,
                nombre     TEXT NOT NULL,
                tipo       TEXT NOT NULL,
                contenido  TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_astro_out_nombre ON astro_outputs(chat_id, nombre, tipo)")
        con.execute(
            "INSERT INTO astro_outputs(chat_id, nombre, tipo, contenido) VALUES (?, ?, ?, ?)",
            (chat_id, (nombre or "").lower(), tipo, contenido or ""),
        )
        con.commit()
        con.close()
        log.info(f"astro_output guardado: chat={chat_id} nombre={nombre} tipo={tipo} len={len(contenido or '')}")
        # Audit: cross-tenant log
        try:
            from services.audit import log_event
            log_event(action="astro_output_saved", resource=tipo,
                      chat_id=chat_id, actor="bot",
                      details={"nombre": nombre, "len": len(contenido or "")})
        except Exception:
            pass
    except Exception as e:
        log.error(f"_save_astro_output error: {e}")


async def menu_opciones_persona(query, nombre):
    botones = [
        [InlineKeyboardButton("📋 Ficha técnica (elegí capa)", callback_data=f"astro:capas:{nombre}")],
        [InlineKeyboardButton("🪐 Tránsitos (sobre natal/solar/lunar)", callback_data=f"astro:transitos:{nombre}")],
        [InlineKeyboardButton("Volver",                 callback_data="astro:list")],
    ]
    teclado = InlineKeyboardMarkup(botones)
    await query.edit_message_text(f"¿Qué querés de {nombre.title()}?", reply_markup=teclado)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "astro:list":
        await menu_lista_cartas(query, context, chat_id)

    elif data.startswith("astro:ver:"):
        nombre = data.split(":", 2)[2]
        await menu_opciones_persona(query, nombre)

    elif data.startswith("astro:capas:"):
        # Submenú: elegir qué capa (natal/solar/lunar) de la ficha técnica.
        nombre = data.split(":", 2)[2]
        botones = [
            [InlineKeyboardButton("🌍 Natal (base permanente)",           callback_data=f"astro:fichacapa:natal:{nombre}")],
            [InlineKeyboardButton("☀️ Solar (tema del año)",              callback_data=f"astro:fichacapa:solar:{nombre}")],
            [InlineKeyboardButton("🌙 Lunar (tema del mes)",              callback_data=f"astro:fichacapa:lunar:{nombre}")],
            [InlineKeyboardButton("← Volver",                             callback_data=f"astro:ver:{nombre}")],
        ]
        await query.edit_message_text(
            f"*Ficha técnica — {nombre.title()}*\n\n¿Qué capa querés?\n\n"
            "• *Natal* — la carta base, no cambia nunca\n"
            "• *Solar* — carta del año astral vigente (última revolución solar)\n"
            "• *Lunar* — carta del mes astral vigente (última revolución lunar)",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(botones),
        )

    elif data.startswith("astro:fichacapa:"):
        # astro:fichacapa:<capa>:<nombre> — al elegir capa, mostrar menú de formato
        parts = data.split(":", 3)
        capa, nombre = parts[2], parts[3]
        capa_label = {"natal": "Natal", "solar": "Solar (año)", "lunar": "Lunar (mes)"}.get(capa, capa)
        botones = [
            [InlineKeyboardButton("📊 Técnico + explicación + gestalt (chat)", callback_data=f"astro:render:{capa}:tecexpl:{nombre}")],
            [InlineKeyboardButton("💬 Explicación en criollo + gestalt (chat)", callback_data=f"astro:render:{capa}:expl:{nombre}")],
            [InlineKeyboardButton("🔬 Solo técnico / data pura (chat)",         callback_data=f"astro:render:{capa}:tec:{nombre}")],
            [InlineKeyboardButton("📄 PDF técnico",                             callback_data=f"astro:render:{capa}:pdf:{nombre}")],
            [InlineKeyboardButton("📑 PDF técnico + interpretación + gestalt",  callback_data=f"astro:render:{capa}:pdfexpl:{nombre}")],
            [InlineKeyboardButton("📖 PDF solo interpretación + gestalt",       callback_data=f"astro:render:{capa}:pdfonly:{nombre}")],
            [InlineKeyboardButton("← Volver",                                   callback_data=f"astro:capas:{nombre}")],
        ]
        await query.edit_message_text(
            f"*Ficha {capa_label} — {nombre.title()}*\n\n¿Cómo la querés?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(botones),
        )

    elif data.startswith("astro:natal:") or data.startswith("astro:ficha:"):
        # Compat con callbacks viejos: redirigir al nuevo submenu de capas.
        nombre = data.split(":", 2)[2]
        await query.edit_message_text(f"Redirigiendo...")
        from telegram import InlineKeyboardMarkup as _IK, InlineKeyboardButton as _IB
        botones = [
            [_IB("🌍 Natal",  callback_data=f"astro:fichacapa:natal:{nombre}")],
            [_IB("☀️ Solar",   callback_data=f"astro:fichacapa:solar:{nombre}")],
            [_IB("🌙 Lunar",   callback_data=f"astro:fichacapa:lunar:{nombre}")],
            [_IB("← Volver",  callback_data=f"astro:ver:{nombre}")],
        ]
        await query.edit_message_text(
            f"*Ficha técnica — {nombre.title()}*\n¿Qué capa?",
            parse_mode="Markdown", reply_markup=_IK(botones),
        )

    elif data.startswith("astro:render:"):
        # astro:render:<capa>:<modo>:<nombre>  donde capa = natal | solar | lunar
        # (por compat viejos callbacks podían mandar kind=ficha|natal; tratamos "ficha" como natal completa)
        parts = data.split(":", 4)
        capa, modo, nombre = parts[2], parts[3], parts[4]
        if capa in ("ficha",):
            capa = "natal"  # compat
        ficha_tecnica = True  # siempre sección completa (el user lo pidió así)
        await query.edit_message_text(f"⏳ Generando {nombre.title()} — capa {capa} ({modo})...")
        datos = astro_recuperar(chat_id, nombre)
        if not datos:
            await context.bot.send_message(chat_id=chat_id, text=f"No encontré carta de {nombre.title()}.")
            return

        # Helper: calcular la carta correspondiente a la capa
        def _carta_por_capa():
            from modules import swiss_engine as e
            natal = e.calc_carta_completa(datos["fecha"], datos["hora"], datos["lugar"])
            if capa == "natal":
                carta = natal
            elif capa == "solar":
                carta = e.calc_retorno_solar(natal)
            elif capa == "lunar":
                carta = e.calc_retorno_lunar(natal)
            else:
                carta = natal
            # Enriquecer con secciones técnicas si es natal (ficha completa)
            if capa == "natal":
                for n, d in carta["planetas"].items():
                    if "error" not in d:
                        d["dignidad"] = e.calc_dignidad(n, d["signo"])
                        d["estado_dinamico"] = e.calc_estado_dinamico(d["speed"], n)
                carta["regentes"] = e.calc_regentes(carta["planetas"], carta["casas"])
            return carta

        # Helper: enviar follow-up con menú contextual al final
        async def _send_followup():
            botones_fu = [
                [InlineKeyboardButton("🪐 Ver tránsitos", callback_data=f"astro:transitos:{nombre}")],
                [InlineKeyboardButton("💼 Desde una perspectiva", callback_data=f"astro:perspectiva:{capa}:{nombre}")],
                [InlineKeyboardButton("📑 PDF + interpretación",  callback_data=f"astro:render:{capa}:pdfexpl:{nombre}")],
                [InlineKeyboardButton("Así está bien",    callback_data="astro:cerrar")],
            ]
            await context.bot.send_message(
                chat_id=chat_id,
                text="¿Algo más con esto?",
                reply_markup=InlineKeyboardMarkup(botones_fu),
            )

        # Modo PDF sólo con la interpretación en criollo + gestalt (sin data técnica en el PDF)
        if modo == "pdfonly":
            try:
                from modules import swiss_engine as e
                # Guardamos ficha técnica pura en el perfil (regla: se guarda siempre)
                carta = _carta_por_capa()
                if capa == "natal":
                    ficha_pura = e.formatear_ficha_tecnica(carta)
                else:
                    ficha_pura = e.formatear_ficha(carta)
                _save_astro_output(chat_id, nombre, f"{capa}_tecnico", ficha_pura)
                # Pedir a Claude la interpretación
                await context.bot.send_message(chat_id=chat_id, text="⏳ Armando la interpretación en criollo para el PDF...")
                import threading as _th, queue as _q, asyncio
                _que = _q.Queue()
                capa_nota = {"natal":"natal","solar":"del retorno solar del año","lunar":"del retorno lunar del mes"}[capa]
                prompt = (
                    f"Carta {capa_nota} de {nombre} (natal: {datos['fecha']}, {datos['hora']}, {datos['lugar']}). "
                    f"Devolveme SOLO la interpretación en criollo argentino COMPLETA — sin tecnicismos — "
                    f"+ al final una sección 'LECTURA GESTALT' integrada. Sin preámbulos ni markdown raros — "
                    f"texto plano fluido, organizado en secciones con títulos en mayúscula. "
                    f"Esto va a ir a PDF — no uses emojis ni caracteres extravagantes. "
                    f"Calculá la carta primero con las tools correspondientes para basarte en datos reales."
                )
                def _run():
                    try: _que.put(("ok", ask_claude(chat_id, prompt, user_name="Cuki")))
                    except Exception as ex: _que.put(("err", str(ex)))
                _t = _th.Thread(target=_run, daemon=True); _t.start()
                _el = 0
                while _t.is_alive() and _el < 300:
                    await asyncio.sleep(4); _el += 4
                    try: await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                    except: pass
                if _t.is_alive():
                    await context.bot.send_message(chat_id=chat_id, text="Tardó demasiado.")
                    return
                st, payload = _que.get_nowait()
                if st == "err":
                    await context.bot.send_message(chat_id=chat_id, text=f"Error: {payload}")
                    return
                reply_text, _pdf, _extras = payload if isinstance(payload, tuple) else (payload, None, [])
                # Generar PDF con el texto de la interpretación
                from scripts.analisis_pista import generar_pdf_pista
                pdf_path = generar_pdf_pista(
                    reply_text,
                    nombre_perfil=nombre,
                    desde=f"{capa}", hasta="interpretacion",
                )
                with open(pdf_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id, document=f,
                        filename=f"interpretacion_{capa}_{nombre}.pdf",
                        caption=f"Interpretación {capa} de {nombre.title()} (en criollo + gestalt)",
                    )
                try: os.unlink(pdf_path)
                except: pass
                # NO guardar el texto — Opus lo regenera
                await _send_followup()
            except Exception as ex:
                log.error(f"pdfonly error: {ex}")
                await context.bot.send_message(chat_id=chat_id, text=f"Error: {ex}")
            return

        # Modo PDF+interpretación: genera PDF técnico + pide a Claude la interpretación
        if modo == "pdfexpl":
            try:
                from modules import swiss_engine as e
                carta = _carta_por_capa()
                if capa == "natal":
                    ficha_pura = e.formatear_ficha_tecnica(carta)
                else:
                    ficha_pura = e.formatear_ficha(carta)
                # Guardar SOLO la ficha técnica pura de la capa elegida
                _save_astro_output(chat_id, nombre, f"{capa}_tecnico", ficha_pura)
                # Generar PDF técnico y enviarlo
                pdf_path = generar_pdf(carta, ficha_tecnica=(capa == "natal"))
                with open(pdf_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id, document=f,
                        filename=f"carta_{nombre}_{capa}.pdf",
                        caption=f"Ficha técnica {capa} de {nombre.title()}",
                    )
                try: os.unlink(pdf_path)
                except: pass
                # Pedirle a Claude la interpretación en texto, que no se guarda
                await context.bot.send_message(chat_id=chat_id, text="⏳ Armando la interpretación en criollo...")
                import threading as _th, queue as _q, asyncio
                _que = _q.Queue()
                capa_nota = {"natal":"natal", "solar":"retorno solar del año", "lunar":"retorno lunar del mes"}[capa]
                prompt_interp = (
                    f"Tengo la carta {capa_nota} de {nombre} (natal: {datos['fecha']}, {datos['hora']}, "
                    f"{datos['lugar']}). Ya tengo la ficha técnica (la mandé como PDF). "
                    f"Dame una interpretación en criollo argentino — sin tecnicismos "
                    f"(nada de 'casa', 'cúspide', 'aspecto', 'trígono') + al final una lectura GESTALT "
                    f"integrada del ecosistema completo. "
                    + ("Calculá la natal con calcular_carta_natal y después el retorno correspondiente si hace falta. " if capa != "natal" else "")
                    + "Esta interpretación NO se guarda — se regenera cada vez."
                )
                def _run():
                    try: _que.put(("ok", ask_claude(chat_id, prompt_interp, user_name="Cuki")))
                    except Exception as ex: _que.put(("err", str(ex)))
                _t = _th.Thread(target=_run, daemon=True); _t.start()
                _el = 0
                while _t.is_alive() and _el < 300:
                    await asyncio.sleep(4); _el += 4
                    try: await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                    except: pass
                if not _t.is_alive():
                    st, payload = _que.get_nowait()
                    if st == "ok":
                        reply_text, _pdf, _extras = payload if isinstance(payload, tuple) else (payload, None, [])
                        await send_long_message(context.bot, chat_id, reply_text)
                        # NO guardar la interpretación — Opus la regenera cuando la pidas
                await _send_followup()
            except Exception as ex:
                log.error(f"PDF+expl error: {ex}")
                await context.bot.send_message(chat_id=chat_id, text=f"Error: {ex}")
            return

        # Modo PDF: genera PDF de la capa elegida
        if modo == "pdf":
            try:
                from modules import swiss_engine as e
                carta = _carta_por_capa()
                pdf_path = generar_pdf(carta, ficha_tecnica=(capa == "natal"))
                with open(pdf_path, "rb") as f:
                    await context.bot.send_document(
                        chat_id=chat_id, document=f,
                        filename=f"carta_{nombre}_{capa}.pdf",
                        caption=f"Ficha {capa} de {nombre.title()}",
                    )
                try: os.unlink(pdf_path)
                except: pass
                # PDF técnico: sí guardamos la ficha pura asociada como referencia técnica
                if capa == "natal":
                    ficha_pura = e.formatear_ficha_tecnica(carta)
                else:
                    ficha_pura = e.formatear_ficha(carta)
                _save_astro_output(chat_id, nombre, f"{capa}_tecnico", ficha_pura)
                await _send_followup()
            except Exception as ex:
                log.error(f"PDF render error: {ex}")
                await context.bot.send_message(chat_id=chat_id, text=f"Error PDF: {ex}")
            return

        # Modo solo técnico: ficha de la capa como texto
        if modo == "tec":
            import threading as _th, queue as _q
            _que = _q.Queue()
            def _run_tec():
                try:
                    from modules import swiss_engine as e
                    carta = _carta_por_capa()
                    if capa == "natal":
                        out = e.formatear_ficha_tecnica(carta)
                    else:
                        out = e.formatear_ficha(carta)
                    _que.put(("ok", out))
                except Exception as ex:
                    _que.put(("err", str(ex)))
            _t = _th.Thread(target=_run_tec, daemon=True); _t.start()
            import asyncio
            _el = 0
            while _t.is_alive() and _el < 120:
                await asyncio.sleep(3); _el += 3
            if _t.is_alive():
                await context.bot.send_message(chat_id=chat_id, text="Tardó demasiado.")
                return
            st, payload = _que.get_nowait()
            if st == "err":
                await context.bot.send_message(chat_id=chat_id, text=f"Error: {payload}")
            else:
                MAX = 4000
                for i in range(0, len(payload), MAX):
                    await context.bot.send_message(chat_id=chat_id, text=payload[i:i+MAX])
                # Técnico puro = sí se guarda en el perfil
                _save_astro_output(chat_id, nombre, f"{capa}_tecnico", payload)
                await _send_followup()
            return

        # Modo "tecexpl": primero guardar la ficha técnica pura (data), después pedir
        # la interpretación al LLM (que NO se guarda — se regenera cada vez).
        if modo == "tecexpl":
            try:
                from modules import swiss_engine as e
                carta = _carta_por_capa()
                if capa == "natal":
                    ficha_pura = e.formatear_ficha_tecnica(carta)
                else:
                    ficha_pura = e.formatear_ficha(carta)
                _save_astro_output(chat_id, nombre, f"{capa}_tecnico", ficha_pura)
            except Exception as _se:
                log.error(f"save ficha pura error: {_se}")

        # Modos expl y tecexpl: interpretación vía ask_claude (NO se guarda nunca).
        import threading as _th, queue as _q, asyncio
        _que = _q.Queue()
        capa_nota = {"natal": "natal", "solar": "del retorno solar (tema del año)",
                     "lunar": "del retorno lunar (tema del mes)"}[capa]
        if modo == "expl":
            prompt = (
                f"Tengo la carta {capa_nota} de {nombre} (natal: {datos['fecha']}, {datos['hora']}, "
                f"{datos['lugar']}). Calculá la carta con las tools correspondientes primero. "
                f"Dame SOLO una explicación COMPLETA en criollo argentino, sin NINGÚN término "
                f"astrológico técnico: nada de 'casa', 'cúspide', 'aspecto', 'trígono', 'cuadratura', "
                f"'regente', 'plenivalencia', 'retrógrado', 'conjunción', 'oposición', 'sextil'. "
                f"Ejemplos cotidianos. Al final, una lectura GESTALT integrada del ecosistema. "
                f"Esta interpretación NO se guarda — se regenera cada vez que el user pide."
            )
        else:  # tecexpl
            prompt = (
                f"Tengo la carta {capa_nota} de {nombre} (natal: {datos['fecha']}, {datos['hora']}, "
                f"{datos['lugar']}). Flujo:\n"
                f"1) Calculá la carta con las tools correspondientes (natal/solar/lunar según capa).\n"
                f"2) Armá el output: para CADA sección/aspecto/planeta/casa del técnico, debajo "
                f"(identado) una explicación corta en criollo argentino SIN tecnicismos.\n"
                f"3) Al FINAL: sección '🔱 LECTURA GESTALT' con interpretación integrada del "
                f"ecosistema — no lista, INTEGRÁ.\n"
                f"Aplicá reglas estocásticas del RAG. Esta interpretación NO se guarda."
            )
        def _run_claude():
            try: _que.put(("ok", ask_claude(chat_id, prompt, user_name="Cuki")))
            except Exception as ex: _que.put(("err", str(ex)))
        _t = _th.Thread(target=_run_claude, daemon=True); _t.start()
        _el = 0
        while _t.is_alive() and _el < 300:
            await asyncio.sleep(4); _el += 4
            try: await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except: pass
        if _t.is_alive():
            await context.bot.send_message(chat_id=chat_id, text="Tardó demasiado, probá de nuevo.")
            return
        st, payload = _que.get_nowait()
        if st == "err":
            await context.bot.send_message(chat_id=chat_id, text=f"Error: {payload}")
        else:
            reply_text, _pdf, _extras = payload if isinstance(payload, tuple) else (payload, None, [])
            await send_long_message(context.bot, chat_id, reply_text)
            # NO guardar: la interpretación la regenera Opus cada vez
            await _send_followup()
        return

    elif data.startswith("astro:ref:"):
        # Marca el último output como "referencia" (facts memory)
        parts = data.split(":", 3)
        kind, nombre = parts[2], parts[3]
        try:
            con = sqlite3.connect(DB_PATH)
            con.execute("""
                UPDATE astro_outputs SET tipo = tipo || '_REF'
                WHERE id = (SELECT id FROM astro_outputs WHERE chat_id=? AND nombre=? ORDER BY id DESC LIMIT 1)
            """, (chat_id, nombre.lower()))
            con.commit(); con.close()
            await query.edit_message_text(f"✅ Marcada como referencia en el perfil de {nombre.title()}.")
        except Exception as ex:
            await query.edit_message_text(f"Error: {ex}")

    elif data.startswith("astro:natal_old:") or data.startswith("astro:ficha_old:"):
        # dead code path - mantenido por compat con mensajes antiguos
        nombre = data.split(":", 2)[2]
        ficha_tecnica = data.startswith("astro:ficha_old:")
        await query.edit_message_text(f"Calculando carta de {nombre.title()}...")
        datos = astro_recuperar(chat_id, nombre)
        if not datos:
            await query.edit_message_text(f"No encontré carta guardada para {nombre}.")
            return
        import threading, queue as q_mod
        q = q_mod.Queue()

        def run():
            try:
                from modules import swiss_engine as e
                carta = e.calc_carta_completa(datos["fecha"], datos["hora"], datos["lugar"])
                if ficha_tecnica:
                    for n, d in carta["planetas"].items():
                        if "error" not in d:
                            d["dignidad"] = e.calc_dignidad(n, d["signo"])
                            d["estado_dinamico"] = e.calc_estado_dinamico(d["speed"], n)
                    carta["regentes"] = e.calc_regentes(carta["planetas"], carta["casas"])
                    result = e.formatear_ficha_tecnica(carta)
                else:
                    result = e.formatear_ficha(carta)
                q.put(("ok", result))
            except Exception as ex:
                q.put(("err", str(ex)))

        t = threading.Thread(target=run, daemon=True)
        t.start()
        import asyncio
        elapsed = 0
        while t.is_alive() and elapsed < 180:
            await asyncio.sleep(4)
            elapsed += 4
        if t.is_alive():
            await context.bot.send_message(chat_id=chat_id, text="Tardo demasiado, intentalo de nuevo.")
            return
        status, payload = q.get_nowait()
        if status == "err":
            await context.bot.send_message(chat_id=chat_id, text=f"Error: {payload}")
        else:
            MAX = 4000
            for i in range(0, len(payload), MAX):
                await context.bot.send_message(chat_id=chat_id, text=payload[i:i+MAX])
            # Auto-guardado en el perfil
            kind_tipo = "ficha_tecnica" if ficha_tecnica else "ficha_natal"
            _save_astro_output(chat_id, nombre, kind_tipo, payload)
            # Follow-up: ofrecer explicación / perspectiva / PDF / guardar
            kind = "ficha" if ficha_tecnica else "natal"
            botones_fu = [
                [InlineKeyboardButton("💬 Explicación sin jerga", callback_data=f"astro:explicar:{kind}:{nombre}")],
                [InlineKeyboardButton("💼 Desde una perspectiva", callback_data=f"astro:perspectiva:{kind}:{nombre}")],
                [InlineKeyboardButton("📄 Generar PDF",           callback_data=f"astro:pdf:{kind}:{nombre}")],
                [InlineKeyboardButton("Así está bien",            callback_data="astro:cerrar")],
            ]
            await context.bot.send_message(
                chat_id=chat_id,
                text="¿Querés algo más con esto?",
                reply_markup=InlineKeyboardMarkup(botones_fu),
            )

    elif data.startswith("astro:explicar:"):
        # astro:explicar:<kind>:<nombre>  — interpretación en criollo, sin jerga
        parts = data.split(":", 3)
        kind, nombre = parts[2], parts[3]
        await query.edit_message_text(f"⏳ Armando explicación en criollo de {nombre.title()}...")
        datos = astro_recuperar(chat_id, nombre)
        if not datos:
            await context.bot.send_message(chat_id=chat_id, text="No encontré la carta.")
            return
        import threading as _th, queue as _q
        _que = _q.Queue()
        prompt_criollo = (
            f"Tengo la carta natal de {nombre} (fecha {datos['fecha']}, hora {datos['hora']}, lugar {datos['lugar']}). "
            f"Dame una explicación COMPLETA de esta carta en criollo argentino, DIRECTA, sin terminología técnica. "
            f"Evitá palabras como 'casa', 'cúspide', 'aspecto', 'trígono', 'cuadratura', 'regente', 'plenivalencia'. "
            f"Describí cómo es la persona, qué temas le tocan, qué le cuesta, qué le fluye, con ejemplos cotidianos. "
            f"Calculá primero la carta con calcular_carta_natal para basarte en datos reales, después traducí."
        )
        def _run_explicar():
            try:
                r = ask_claude(chat_id, prompt_criollo, user_name="Cuki")
                _que.put(("ok", r))
            except Exception as ex:
                _que.put(("err", str(ex)))
        _t = _th.Thread(target=_run_explicar, daemon=True); _t.start()
        import asyncio
        _el = 0
        while _t.is_alive() and _el < 240:
            await asyncio.sleep(4); _el += 4
            try: await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except: pass
        if _t.is_alive():
            await context.bot.send_message(chat_id=chat_id, text="Tardó demasiado, probá de nuevo.")
            return
        st, payload = _que.get_nowait()
        if st == "err":
            await context.bot.send_message(chat_id=chat_id, text=f"Error: {payload}")
        else:
            reply_text, _pdf, _extras = payload if isinstance(payload, tuple) else (payload, None, [])
            await send_long_message(context.bot, chat_id, reply_text)
            _save_astro_output(chat_id, nombre, "explicacion_criollo", reply_text)

    elif data.startswith("astro:perspectiva:"):
        # astro:perspectiva:<kind>:<nombre>  — submenu de perspectivas
        parts = data.split(":", 3)
        kind, nombre = parts[2], parts[3]
        botones_p = [
            [InlineKeyboardButton("💞 Vincular / pareja",     callback_data=f"astro:persp:vincular:{kind}:{nombre}")],
            [InlineKeyboardButton("💼 Laboral / vocacional",   callback_data=f"astro:persp:laboral:{kind}:{nombre}")],
            [InlineKeyboardButton("🌱 Evolutiva / espiritual", callback_data=f"astro:persp:evolutiva:{kind}:{nombre}")],
            [InlineKeyboardButton("💰 Financiera",             callback_data=f"astro:persp:financiera:{kind}:{nombre}")],
            [InlineKeyboardButton("🧘 Salud física y mental",  callback_data=f"astro:persp:salud:{kind}:{nombre}")],
            [InlineKeyboardButton("← Volver",                  callback_data=f"astro:ver:{nombre}")],
        ]
        await query.edit_message_text(
            "¿Desde qué perspectiva querés la lectura?",
            reply_markup=InlineKeyboardMarkup(botones_p),
        )

    elif data.startswith("astro:persp:"):
        # astro:persp:<persp>:<kind>:<nombre>  — ejecuta la interpretación
        parts = data.split(":", 4)
        persp, kind, nombre = parts[2], parts[3], parts[4]
        persp_label = {
            "vincular":   "vincular y de pareja",
            "laboral":    "laboral y vocacional",
            "evolutiva":  "evolutiva y espiritual",
            "financiera": "financiera y del dinero",
            "salud":      "de la salud física y mental",
        }.get(persp, persp)
        await query.edit_message_text(f"⏳ Leyendo desde la perspectiva {persp_label}...")
        datos = astro_recuperar(chat_id, nombre)
        if not datos:
            await context.bot.send_message(chat_id=chat_id, text="No encontré la carta.")
            return
        import threading as _th, queue as _q, asyncio
        _que = _q.Queue()
        prompt = (
            f"Leeme la carta natal de {nombre} (fecha {datos['fecha']}, hora {datos['hora']}, "
            f"lugar {datos['lugar']}) DESDE LA PERSPECTIVA {persp_label.upper()}. "
            f"Calculá primero la carta con calcular_carta_natal para basarte en datos reales. "
            f"Enfocate SOLO en lo que aporta esa perspectiva — no hagas lectura general. "
            f"Escribí directo, en criollo, sin tecnicismos astrológicos."
        )
        def _run():
            try: _que.put(("ok", ask_claude(chat_id, prompt, user_name="Cuki")))
            except Exception as ex: _que.put(("err", str(ex)))
        _t = _th.Thread(target=_run, daemon=True); _t.start()
        _el = 0
        while _t.is_alive() and _el < 240:
            await asyncio.sleep(4); _el += 4
            try: await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except: pass
        if _t.is_alive():
            await context.bot.send_message(chat_id=chat_id, text="Tardó demasiado.")
            return
        st, payload = _que.get_nowait()
        if st == "err":
            await context.bot.send_message(chat_id=chat_id, text=f"Error: {payload}")
        else:
            reply_text, _pdf, _extras = payload if isinstance(payload, tuple) else (payload, None, [])
            await send_long_message(context.bot, chat_id, reply_text)
            _save_astro_output(chat_id, nombre, f"perspectiva_{persp}", reply_text)

    elif data.startswith("astro:pdf:"):
        # astro:pdf:<kind>:<nombre>  — regenera la ficha y la manda como PDF
        parts = data.split(":", 3)
        kind, nombre = parts[2], parts[3]
        await query.edit_message_text(f"⏳ Generando PDF de {nombre.title()}...")
        datos = astro_recuperar(chat_id, nombre)
        if not datos:
            await context.bot.send_message(chat_id=chat_id, text="No encontré la carta.")
            return
        try:
            from modules import swiss_engine as e
            carta = e.calc_carta_completa(datos["fecha"], datos["hora"], datos["lugar"])
            ficha_tecnica = (kind == "ficha")
            if ficha_tecnica:
                for n, d in carta["planetas"].items():
                    if "error" not in d:
                        d["dignidad"] = e.calc_dignidad(n, d["signo"])
                        d["estado_dinamico"] = e.calc_estado_dinamico(d["speed"], n)
                carta["regentes"] = e.calc_regentes(carta["planetas"], carta["casas"])
            pdf_path = generar_pdf(carta, ficha_tecnica=ficha_tecnica)
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id, document=f,
                    filename=f"carta_{nombre}_{kind}.pdf",
                    caption=f"Carta {'completa' if ficha_tecnica else 'natal'} de {nombre.title()}",
                )
            try: os.unlink(pdf_path)
            except: pass
        except Exception as ex:
            log.error(f"PDF menu error: {ex}")
            await context.bot.send_message(chat_id=chat_id, text=f"Error generando PDF: {ex}")

    elif data.startswith("astro:transitos:"):
        nombre = data.split(":", 2)[2]
        botones = [
            [InlineKeyboardButton("🌍 Sobre la Natal",           callback_data=f"astro:trans_natal:{nombre}")],
            [InlineKeyboardButton("☀️ Sobre la Solar (año)",      callback_data=f"astro:trans_solar:{nombre}")],
            [InlineKeyboardButton("🌙 Sobre la Lunar (mes)",      callback_data=f"astro:trans_lunar:{nombre}")],
            [InlineKeyboardButton("🔱 Triple capa (jerarquía)",   callback_data=f"astro:trans_triple:{nombre}")],
            [InlineKeyboardButton("🌌 Cielo del día (sin natal)", callback_data=f"astro:cielo:{nombre}")],
            [InlineKeyboardButton("← Volver",                     callback_data=f"astro:ver:{nombre}")],
        ]
        await query.edit_message_text(
            f"*Tránsitos de {nombre.title()}*\n\n¿Sobre qué capa?\n\n"
            "• *Natal* — mapa base permanente\n"
            "• *Solar* — tema del año (última revolución solar)\n"
            "• *Lunar* — tema del mes (última revolución lunar)\n"
            "• *Triple capa* — activaciones en las 3 con jerarquía (natal → solar → lunar)\n"
            "• *Cielo del día* — solo posiciones de los planetas hoy (grado, signo, D/R), sin comparar con tu carta",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(botones),
        )

    elif data.startswith("astro:cielo:"):
        # Posiciones del cielo del día sin referenciar carta natal.
        # Solo grado + signo + speed + D/R de cada planeta, tal cual están hoy.
        nombre = data.split(":", 2)[2]
        await query.edit_message_text(f"⏳ Foto del cielo de hoy...")
        try:
            from modules import swiss_engine as e
            import swisseph as _swe
            import datetime as _dt
            now = _dt.datetime.utcnow()
            jd = _swe.julday(now.year, now.month, now.day,
                             now.hour + now.minute/60.0 + now.second/3600.0)
            planetas = e.calc_planets(jd)
            lines = [f"🌌 *Cielo del día — {now.strftime('%Y-%m-%d %H:%M UTC')}*\n"]
            lines.append("_Foto pura del cielo. No compara con ninguna carta natal._\n")
            orden = ["Sol","Luna","Mercurio","Venus","Marte","Jupiter","Saturno","Urano","Neptuno","Pluton","Nodo Norte","Quiron"]
            for n in orden:
                p = planetas.get(n, {})
                if "error" in p: continue
                dr = "R" if p.get("speed",0) < 0 else "D"
                signo = p.get("signo","?")
                speed = p.get("speed",0)
                lines.append(f"• *{n:12s}* {signo}  ({dr})  speed {speed:+.3f}°/día")
            # Alerta Luna cambio signo
            luna_lon = planetas.get("Luna",{}).get("lon",0) % 30
            if luna_lon >= 28:
                lines.append(f"\n⚠️ *ALERTA*: Luna a {30 - luna_lon:.2f}° del cambio de signo")
            output = "\n".join(lines)
            await query.edit_message_text(output, parse_mode="Markdown")
            _save_astro_output(chat_id, nombre, "cielo_del_dia", output)
        except Exception as ex:
            log.error(f"Cielo del día error: {ex}")
            await query.edit_message_text(f"Error: {ex}")

    elif data.startswith("astro:trans_natal:") or data.startswith("astro:trans_solar:") or data.startswith("astro:trans_lunar:"):
        target = data.split(":", 2)[1].replace("trans_", "")  # natal | solar | lunar
        nombre = data.split(":", 2)[2]
        await query.edit_message_text(f"⏳ Calculando tránsitos sobre {target} de {nombre.title()}...")
        try:
            from modules import swiss_engine as e
            from modules.swiss_engine import (
                calc_transitos, formatear_transitos,
                calc_retorno_solar, calc_retorno_lunar,
            )
            datos = astro_recuperar(chat_id, nombre)
            if not datos:
                await query.edit_message_text(f"No tengo carta de {nombre.title()}.")
                return
            natal = e.calc_carta_completa(datos["fecha"], datos["hora"], datos["lugar"])
            if target == "solar":
                base = calc_retorno_solar(natal)
                label = "solar"
            elif target == "lunar":
                base = calc_retorno_lunar(natal)
                label = "lunar"
            else:
                base = natal
                label = "natal"
            trans = calc_transitos(base)
            header = f"🪐 *Tránsitos sobre {label} — {nombre.title()}*\n"
            header += f"📍 {datos['fecha']} · {datos['hora']} · {datos['lugar'][:40]}\n\n"
            body = formatear_transitos(trans, top_n=20, etiqueta_natal=label)
            full_text = header + body
            await query.edit_message_text(full_text, parse_mode="Markdown")
            _save_astro_output(chat_id, nombre, f"transitos_{target}", full_text)
        except Exception as ex:
            log.error(f"Trans {target} error: {ex}")
            await query.edit_message_text(f"Error calculando: {ex}")

    elif data.startswith("astro:trans_triple:"):
        nombre = data.split(":", 2)[2]
        await query.edit_message_text(f"⏳ Analizando triple capa de {nombre.title()} (natal → solar → lunar)...")
        try:
            from modules import swiss_engine as e
            from modules.swiss_engine import calc_triple_capa, formatear_triple_capa
            datos = astro_recuperar(chat_id, nombre)
            if not datos:
                await query.edit_message_text(f"No tengo carta de {nombre.title()}.")
                return
            natal = e.calc_carta_completa(datos["fecha"], datos["hora"], datos["lugar"])
            tc = calc_triple_capa(natal)
            out = formatear_triple_capa(tc, top_n=8)
            header = f"*Triple capa — {nombre.title()}*\n_Jerarquía: tránsitos sobre natal → revolución solar (fáctico) → revolución lunar (emocional)_\n\n"
            payload = header + out
            MAX = 3800
            if len(payload) <= MAX:
                await query.edit_message_text(payload, parse_mode="Markdown")
            else:
                await query.edit_message_text(payload[:MAX], parse_mode="Markdown")
                for i in range(MAX, len(payload), MAX):
                    await context.bot.send_message(chat_id=chat_id, text=payload[i:i+MAX], parse_mode="Markdown")
            _save_astro_output(chat_id, nombre, "triple_capa", payload)
        except Exception as ex:
            log.error(f"Triple error: {ex}")
            await query.edit_message_text(f"Error en triple capa: {ex}")

    elif data == "astro:cerrar":
        await query.edit_message_text("Menu cerrado.")

async def cmd_cartas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_lista_cartas(update, context, update.effective_chat.id)

# ── Main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        init_db()
        log.info(f"DB inicializada en: {DB_PATH}")
    except Exception as e:
        log.error(f"Error init_db: {e}")
    try:
        SYSTEM_CONFIG = load_all_active(DB_PATH)
        log.info(f'Config cargada: {len(SYSTEM_CONFIG)} entradas desde Railway DB')
    except Exception as e:
        SYSTEM_CONFIG = {}
        log.warning(f"No se pudo cargar config: {e}")
    log.info("🤖 CukinatorBot iniciando...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("cartas",    cmd_cartas))
    app.add_handler(CommandHandler("testvoice", cmd_testvoice))
    app.add_handler(CommandHandler("voz",        cmd_voz))
    app.add_handler(CommandHandler("menu",       cmd_menu))
    app.add_handler(CommandHandler("biblioteca", cmd_biblioteca))
    try:
        from handlers.message_handler import cmd_qr as _cmd_qr_legacy, cmd_sf as _cmd_sf_legacy
        app.add_handler(CommandHandler("qr",     _cmd_qr_legacy))
        app.add_handler(CommandHandler("sf",     _cmd_sf_legacy))
    except Exception:
        pass
    app.add_handler(CallbackQueryHandler(handle_biblioteca_callback, pattern="^lib:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(handle_voz_callback,  pattern="^voz:"))
    app.add_handler(CallbackQueryHandler(handle_callback,      pattern="^astro:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    log.info("✅ Bot en línea.")
    app.run_polling(drop_pending_updates=True)
