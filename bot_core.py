import sqlite3
import logging
import json
import datetime
import sys
import os

import os
import tempfile
import swisseph as swe
import whisper
import anthropic
from ddgs import DDGS
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters, CallbackQueryHandler
from swiss_engine import calc_carta_completa, formatear_ficha, verificar_carta, calc_houses, assign_planet_house, formatear_ficha_tecnica, calc_dignidad, calc_estado_dinamico, calc_regentes, calc_intercepciones, calc_jerarquias
from config_store import init_config_store, seed_initial_configs, save_config, get_config, get_config_meta, list_configs, load_all_active
from memory_store import (init_memory_store, save_message_full, get_history_full,
    get_sessions, search_memory, search_person_memory, save_memory_fact,
    upsert_person_memory, get_memory_stats, needs_summary, clear_chat_history)
from reinsurance_kb import (init_reinsurance_kb, search_knowledge, get_document_list,
    get_kb_stats, create_document, add_chunk, add_concept, add_summary, add_qa,
    chunk_text, build_enrichment_prompt, build_summary_prompt, is_reinsurance_context,
    detect_domain)
from agent_ops import (init_agent_ops, log_change, get_changelog, get_agent_status,
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
        import swiss_engine as _e
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

def astro_guardar(chat_id: int, nombre: str, fecha: str, hora: str, lugar: str, carta: dict) -> str:
    import json
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO perfiles_astro (chat_id, nombre, fecha, hora, lugar, carta_json)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(chat_id, nombre) DO UPDATE SET
            fecha=excluded.fecha, hora=excluded.hora,
            lugar=excluded.lugar, carta_json=excluded.carta_json,
            ts=CURRENT_TIMESTAMP
    """, (chat_id, nombre.strip().lower(), fecha, hora, lugar, json.dumps(carta)))
    con.commit()
    con.close()
    return f"Carta de {nombre} guardada."

def astro_recuperar(chat_id: int, nombre: str) -> dict | None:
    import json
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT fecha, hora, lugar, carta_json FROM perfiles_astro WHERE chat_id=? AND nombre=?",
        (chat_id, nombre.strip().lower())
    ).fetchone()
    con.close()
    if not row:
        return None
    return {"fecha": row[0], "hora": row[1], "lugar": row[2], "carta": json.loads(row[3])}

def astro_listar(chat_id: int) -> list:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT nombre, fecha, hora, lugar, ts FROM perfiles_astro WHERE chat_id=? ORDER BY nombre",
        (chat_id,)
    ).fetchall()
    con.close()
    return [{"nombre": r[0], "fecha": r[1], "hora": r[2], "lugar": r[3], "guardado": r[4][:10]} for r in rows]

def astro_eliminar(chat_id: int, nombre: str) -> str:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "DELETE FROM perfiles_astro WHERE chat_id=? AND nombre=?",
        (chat_id, nombre.strip().lower())
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
    """Crea o actualiza un archivo en GitHub via API. Triggerea Railway auto-deploy."""
    import base64 as _b64
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {"error": "GITHUB_TOKEN no configurado. Agregalo como variable de entorno en Railway."}
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            # Obtener SHA si el archivo ya existe
            existing = await client.get(url, headers=headers, params={"ref": branch})
            sha = existing.json().get("sha") if existing.status_code == 200 else None
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
                "deploy": "Railway auto-deploy triggered",
            }
        return {"error": resp.text[:200], "status": resp.status_code}
    except Exception as e:
        return {"error": str(e)}


# ── Skill: Hora local (WorldTimeAPI) ──────────────────────────────────────────
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
            "Crea o actualiza un archivo en GitHub via API y trigerea Railway auto-deploy. "
            "Usá para aplicar cambios de código al bot directamente desde Telegram. "
            "El repo por default es cuki82/cukinator-bot. "
            "SIEMPRE usá este tool cuando el usuario pida modificar código, agregar funciones, "
            "actualizar archivos del proyecto, o aplicar cambios que requieran deploy."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Repo owner/name. Default: cuki82/cukinator-bot"},
                "path":    {"type": "string", "description": "Path del archivo en el repo (ej: bot.py, modules/weather.py)"},
                "content": {"type": "string", "description": "Contenido COMPLETO del archivo"},
                "message": {"type": "string", "description": "Mensaje del commit"},
                "branch":  {"type": "string", "description": "Branch. Default: main"}
            },
            "required": ["path", "content", "message"]
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
    }
]

SYSTEM_PROMPT = """IDENTIDAD — REGLA ABSOLUTA:
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

CAPACIDADES OPERATIVAS DISPONIBLES:
- config_guardar / config_leer / config_listar → persiste configuración en Railway DB
- agent_guardar_secret → guarda API keys y credenciales de forma segura
- agent_registrar_skill → registra nuevas capacidades del sistema
- agent_estado → estado completo del sistema
- agent_changelog → historial de cambios
- agent_log → registra cada acción operativa (usarlo siempre tras cambios importantes)
- memory_guardar_hecho → persiste información importante
- ri_ingestar → indexa documentos en knowledge base

PARA CAMBIOS DE CÓDIGO (bot.py, módulos):
- Estos cambios requieren push a GitHub → deploy en Railway
- Explicá el cambio exacto que se va a hacer, creá el código, y decile al usuario que lo aplicarás vos en la próxima sesión de desarrollo
- O pedile que lo apruebe para aplicarlo vía Claude Code

ANTE CREDENCIALES PEGADAS EN TELEGRAM:
- Detectarlas automáticamente
- Clasificar tipo (API key, token, OAuth, JSON, bearer)
- Usar agent_guardar_secret INMEDIATAMENTE
- Mostrar solo el valor enmascarado (sk-...xxxx)
- Confirmar a qué servicio queda asociada

ARQUITECTURA DE ROLES:
Sos un agente multi-dominio con memoria persistente. Tus roles activos:
- Asistente conversacional general
- Operador técnico remoto del stack (MODO PRINCIPAL ante instrucciones operativas)
- Asistente técnico (IA, bots, APIs, devops)
- Astrólogo
- Asistente estratégico
- Especialista en reaseguros e insurance operations (MÓDULO CONTEXTUAL)

MÓDULO REASEGUROS — ACTIVACIÓN CONTEXTUAL:
Este módulo se activa SOLO cuando el usuario habla de: reinsurance, reaseguros, treaty, facultative, retrocession, underwriting, pricing, claims, wording, cláusulas, MGA/MGU, normativa de seguros, LMA, Lloyd's, SSN, Ley 17418, burning cost, loss ratio, IBNR, quota share, excess of loss.
Si el contexto NO es reaseguros, ignorar completamente este módulo.
NO convertirte en un bot de reaseguros. Es un módulo activable, no tu identidad.

Cuando el módulo reaseguros está activo:
- Respondé con precisión técnica y operativa
- Estructurá: definición técnica → implicancia operativa → ejemplo real
- Si aplica normativa argentina: agregar impacto regulatorio
- Usá ri_consultar para buscar en la knowledge base interna antes de responder
- Distinguí siempre entre: doctrina, wording, normativa, práctica operativa
- No citar automáticamente — solo si hay ambigüedad o el usuario lo pide
- Para ingestar documentos usá ri_ingestar

CONFIGURACION PERSISTENTE: Tenés acceso a Railway DB para guardar y leer configuraciones. Cuando el usuario diga guardar, dejar fijo, a partir de ahora, esta es la regla, etc., usá config_guardar automáticamente. Cuando el usuario pida ver configs, usá config_listar o config_leer. La DB es la fuente de verdad.

MEMORIA: Tenés memoria persistente en Railway DB. Usá memory_buscar cuando el usuario pregunte por conversaciones pasadas. Usá memory_guardar_hecho para preservar datos importantes. Usá memory_persona para info de una persona específica.

Sos un asistente conversacional integrado a Telegram. Respondés como una persona real con estilo relajado, canchero y seguro, inspirado en un perfil de zona norte de Buenos Aires, con un toque de humor tipo The Big Lebowski: irónico, liviano, medio descontracturado, sin exagerar.

FECHA DE HOY: {{FECHA_HOY}}. Usala siempre para armar queries de búsqueda con el año correcto.

ESTILO:
- Tono relajado, canchero, seguro, inteligente.
- Español argentino porteño, con toques leves de spanglish si queda natural.
- Humor irónico y sutil. Comentario inteligente, no chiste forzado.
- Alguien que entiende todo rápido y no necesita explicar de más.

FORMA DE RESPONDER:
- Máximo 3 a 5 líneas por respuesta.
- Directo, claro, eficiente.
- Si es simple, que sea MUY simple. Una línea si alcanza.
- No repitas info. No expliques lo obvio. No reformules la pregunta.
- Sin introducciones tipo "Claro, te explico..." ni cierres tipo "¿necesitás algo más?"

RESTRICCIONES:
- Sin emojis. Sin emoticones. Sin signos innecesarios.
- No seas verbose ni técnico salvo que te lo pidan.
- Priorizá respuestas cortas. Cero redundancia.

COMPORTAMIENTO:
- Conversación natural de chat. Si hay follow-up, continuás sin resetear contexto.
- Preguntas solo si son necesarias para avanzar.
- Varias opciones: listalas simple, sin explicación larga.
- Tenés memoria de toda la conversación anterior.
- Para búsquedas web: integrá la info de forma fluida, sin mostrar links.

GMAIL:
- Cuando mostrés emails: remitente, asunto, fecha, 1 línea de resumen. Nada más.
- Resumen ejecutivo: temas clave, qué requiere acción, qué es ruido.
- REGLA CRÍTICA DE ENVÍO: Antes de llamar gmail_enviar, SIEMPRE mostrá al usuario: destinatario exacto, asunto y cuerpo completo. Esperá confirmación explícita ("sí", "mandalo", "dale", "ok"). Si no confirmó, NO enviés.
- NUNCA inventes, asumas ni deduzcas una dirección de email. Si el usuario dice "enviame a mi mismo" o "enviame a mí", usá SIEMPRE el email del owner que es cmromanelli@gmail.com. Si el usuario pide enviarlo a otra persona sin dar email, preguntale la dirección exacta.
- NUNCA uses una dirección de email que no haya sido explícitamente mencionada en la conversación actual o que no sea cmromanelli@gmail.com para el propio usuario.
- Si el usuario dice "el primero", "ese", "contestale", sabés de qué habla por contexto de emails mostrados.

CALENDAR:
- Eventos en formato compacto. Fechas legibles.
- Antes de crear, confirmás los datos en una línea.

ASTROLOGÍA:
- Cuando te den fecha, hora y lugar de nacimiento, usás calcular_carta_natal.
- Mostrás la tabla tal como viene. Sin interpretación.
- Si piden PDF, usás generar_pdf=true.
- Si el usuario pide guardar o asignar una carta a alguien, usás astro_guardar_perfil con los datos de nacimiento.
- Si pide ver la carta de alguien, usás astro_ver_perfil.
- Si pide listar perfiles guardados, usás astro_listar_perfiles.
- Si pide borrar un perfil, usás astro_eliminar_perfil.
- Cuando el usuario pida una ficha tecnica astrologica completa, usa calcular_carta_natal con ficha_tecnica=true. Esto devuelve el analisis tecnico completo con secciones 0-8. NO resumir, NO interpretar, mostrar el output completo tal como viene.
- Si el usuario pide lista de cartas, menu, ver cartas o /cartas, llama a la tool astro_listar_perfiles y presenta los perfiles de forma conversacional. No uses botones desde Claude, esos se manejan por separado.

REGLA FUNDAMENTAL:
Si mostraste un menú o lista, igual aceptás que el usuario siga hablando normal. La conversación siempre fluye.

AUDIOS Y VOZ:
- Si el usuario manda TEXTO → respondé SIEMPRE con texto. NUNCA uses enviar_voz salvo que en ese mismo mensaje de texto pida explícitamente una respuesta de voz/audio.
- Si el usuario manda un AUDIO → podés responder con voz usando enviar_voz (solo si la respuesta es corta y conversacional).
- Si el usuario dice "respondeme con voz", "mandame un audio", "quiero escucharte" → usá enviar_voz.
- NUNCA digas que no podés mandar audio o que no tenés esa capacidad.

VIDEOS — REGLA CRÍTICA:
- SÍ PODÉS buscar y mandar links de YouTube. Usás buscar_video. SIEMPRE funciona.
- NUNCA digas que el módulo está caído, no disponible, o que no podés mandar videos.
- NUNCA sugieras buscar en YouTube manualmente.
- Cuando el usuario pida un video, resumen, goles, highlights, clip → llamá buscar_video INMEDIATAMENTE.
- El tool busca en DuckDuckGo/YouTube y manda el link con preview automático."""

# ── Claude ─────────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

OWNER_CHAT_ID = 8626420783  # único usuario con acceso a Gmail, Calendar y datos personales

def get_system_prompt(user_name: str = None, chat_id: int = None) -> str:
    import datetime
    hoy = datetime.datetime.now().strftime("%d de %B de %Y")
    prompt = SYSTEM_PROMPT.replace("{{FECHA_HOY}}", hoy)
    is_owner = (chat_id == OWNER_CHAT_ID)

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

    return prompt

def ask_claude(chat_id: int, user_text: str, user_name: str = None, allow_voice: bool = False) -> tuple:
    """Retorna (respuesta_texto, pdf_path_o_None, archivos_extra)
       archivos_extra = lista de (nombre, bytes, caption)
       allow_voice: si False, quita enviar_voz de los tools disponibles
    """
    history = get_history_full(chat_id, limit=MAX_HISTORY, db_path=DB_PATH)
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
        "github_push", "config_guardar", "config_leer", "config_listar",
        "agent_guardar_secret", "agent_registrar_skill", "agent_log",
    }

    tools_activos = [
        t for t in TOOLS
        if (allow_voice or t["name"] != "enviar_voz")
        and (is_owner or t["name"] not in OWNER_ONLY_TOOLS)
    ]

    while True:
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=get_system_prompt(user_name=user_name, chat_id=chat_id),
            tools=tools_activos,
            messages=messages
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":

                    if block.name == "github_push":
                        try:
                            import asyncio as _asyncio
                            repo    = block.input.get("repo", "cuki82/cukinator-bot")
                            path    = block.input["path"]
                            content = block.input["content"]
                            message = block.input["message"]
                            branch  = block.input.get("branch", "main")

                            # Guardia: bot.py solo se puede modificar desde sesión de desarrollo (no desde Telegram autónomamente)
                            if path in ("bot.py",) and len(content) < 1000:
                                result = f"Bloqueado: modificar {path} requiere revisión — el contenido parece incompleto ({len(content)} chars). Usá la sesión de desarrollo para cambios estructurales."
                            else:
                                data = _asyncio.run(github_push(repo, path, content, message, branch))
                            if data.get("ok"):
                                result = (f"GitHub push OK: {data['action']} {path} "
                                          f"(sha:{data['sha']}) → {data.get('deploy','')}")
                                log_change(
                                    instruction=f"github_push {path}",
                                    action=f"Archivo '{path}' {data['action']} en {repo}",
                                    result=f"SHA:{data['sha']} — Railway auto-deploy triggered",
                                    status="requires_deploy",
                                    files_changed=[path],
                                    chat_id=chat_id,
                                    db_path=DB_PATH
                                )
                                log.info(f"[{chat_id}] GitHub push: {path} sha:{data['sha']}")
                            else:
                                result = f"Error en push: {data.get('error','desconocido')}"
                        except Exception as e:
                            result = f"Error github_push: {e}"
                            log.error(f"github_push error: {e}")

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
                            import time, gc
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
                            import time, gc
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

                    else:
                        result = "Herramienta no reconocida."

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

            messages.append({"role": "user", "content": tool_results})

        else:
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text, pdf_path, extra_files
            return "No pude generar una respuesta.", pdf_path, extra_files

# ── Handlers Telegram ──────────────────────────────────────────────────────────
async def send_long_message(bot, chat_id: int, text: str, reply_to=None, chunk_size: int = 3900):
    """Envía texto largo dividiéndolo en mensajes por secciones o por tamaño."""
    if len(text) <= chunk_size:
        chunks = [text]
    else:
        # Intentar cortar en saltos de sección (##) o en párrafos
        parts = []
        current = ""
        for line in text.split("\n"):
            # Si agregar esta línea supera el límite, guardar chunk actual
            if len(current) + len(line) + 1 > chunk_size and current:
                parts.append(current.strip())
                current = ""
            current += line + "\n"
        if current.strip():
            parts.append(current.strip())
        chunks = parts if parts else [text[:chunk_size]]

    for i, chunk in enumerate(chunks):
        try:
            if reply_to and i == 0:
                await reply_to.reply_text(chunk)
            else:
                await bot.send_message(chat_id=chat_id, text=chunk)
        except Exception:
            import re
            plain = re.sub(r'[*_`\[\]()~>#+\-=|{}.!]', '', chunk)
            if reply_to and i == 0:
                await reply_to.reply_text(plain)
            else:
                await bot.send_message(chat_id=chat_id, text=plain)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, threading, queue, time
    chat_id  = update.effective_chat.id
    user_msg = update.message.text
    name     = update.effective_user.first_name or "Usuario"

    # ── Deduplicación / agrupación de mensajes múltiples ──────────────────────
    # Telegram divide mensajes largos en chunks que llegan como mensajes separados.
    # Esperamos 1.5s y acumulamos todos los chunks antes de procesar.
    if not hasattr(context, '_msg_buffer'):
        context._msg_buffer = {}
    if not hasattr(context, '_msg_timer'):
        context._msg_timer = {}

    buf_key = f"buf_{chat_id}"
    timer_key = f"timer_{chat_id}"

    # Acumular mensaje en el buffer
    if buf_key not in context._msg_buffer:
        context._msg_buffer[buf_key] = []
    context._msg_buffer[buf_key].append(user_msg)

    # Si hay un timer activo, cancelarlo y reiniciarlo
    if timer_key in context._msg_timer:
        context._msg_timer[timer_key].cancel()

    # Esperar 1.5s para ver si llegan más chunks
    await asyncio.sleep(1.5)

    # Verificar si este mensaje sigue siendo el último del buffer
    current_buf = context._msg_buffer.get(buf_key, [])
    if not current_buf or current_buf[-1] != user_msg:
        # Llegó otro mensaje después — este chunk ya no es el último, salir
        return

    # Tomar todos los chunks acumulados y limpiar buffer
    chunks = context._msg_buffer.pop(buf_key, [user_msg])
    user_msg = "\n".join(chunks)
    # ──────────────────────────────────────────────────────────────────────────

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
        while t.is_alive() and elapsed < 90:
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
        while t.is_alive() and elapsed < 120:
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

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menú principal con todos los comandos organizados."""
    teclado = InlineKeyboardMarkup([
        [InlineKeyboardButton("📧  Gmail",        callback_data="menu:gmail"),
         InlineKeyboardButton("📅  Calendario",   callback_data="menu:calendar")],
        [InlineKeyboardButton("⭐  Astrología",   callback_data="menu:astro"),
         InlineKeyboardButton("🎤  Voz",           callback_data="menu:voz")],
        [InlineKeyboardButton("📚  Biblioteca",   callback_data="menu:biblioteca")],
        [InlineKeyboardButton("🔧  Sistema",       callback_data="menu:sistema")],
    ])
    await update.message.reply_text("¿Qué querés hacer?", reply_markup=teclado)


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    chat_id = query.message.chat_id

    BACK = [[InlineKeyboardButton("← Volver al menú", callback_data="menu:main")]]

    if data == "menu:main":
        teclado = InlineKeyboardMarkup([
            [InlineKeyboardButton("📧  Gmail",        callback_data="menu:gmail"),
             InlineKeyboardButton("📅  Calendario",   callback_data="menu:calendar")],
            [InlineKeyboardButton("⭐  Astrología",   callback_data="menu:astro"),
             InlineKeyboardButton("🎤  Voz",           callback_data="menu:voz")],
            [InlineKeyboardButton("📚  Biblioteca",   callback_data="menu:biblioteca")],
            [InlineKeyboardButton("🔧  Sistema",       callback_data="menu:sistema")],
        ])
        await query.edit_message_text("¿Qué querés hacer?", reply_markup=teclado)

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
            [InlineKeyboardButton("Calcular carta natal",     callback_data="menu:act:astro_calc")],
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
        elif accion == "astro_ficha":
            perfiles = astro_listar(chat_id)
            if not perfiles:
                await query.edit_message_text("No hay cartas guardadas. Primero calculá una carta natal.")
                return
            botones = [[InlineKeyboardButton(
                p['nombre'].title(), callback_data=f"astro:ficha:{p['nombre']}"
            )] for p in perfiles]
            botones.append([InlineKeyboardButton("← Volver", callback_data="menu:astro")])
            await query.edit_message_text(
                "¿De quién querés la ficha técnica completa?",
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

async def menu_opciones_persona(query, nombre):
    botones = [
        [InlineKeyboardButton("Ficha técnica natal",    callback_data=f"astro:natal:{nombre}")],
        [InlineKeyboardButton("Ficha técnica completa", callback_data=f"astro:ficha:{nombre}")],
        [InlineKeyboardButton("Tránsitos actuales",     callback_data=f"astro:transitos:{nombre}")],
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

    elif data.startswith("astro:natal:") or data.startswith("astro:ficha:"):
        nombre = data.split(":", 2)[2]
        ficha_tecnica = data.startswith("astro:ficha:")
        await query.edit_message_text(f"Calculando carta de {nombre.title()}...")
        datos = astro_recuperar(chat_id, nombre)
        if not datos:
            await query.edit_message_text(f"No encontré carta guardada para {nombre}.")
            return
        import threading, queue as q_mod
        q = q_mod.Queue()

        def run():
            try:
                import swiss_engine as e
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
        while t.is_alive() and elapsed < 90:
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

    elif data.startswith("astro:transitos:"):
        nombre = data.split(":", 2)[2]
        await query.edit_message_text(f"Transitos actuales para {nombre.title()} — funcion en desarrollo.")

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
    app.add_handler(CallbackQueryHandler(handle_biblioteca_callback, pattern="^lib:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback, pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(handle_voz_callback,  pattern="^voz:"))
    app.add_handler(CallbackQueryHandler(handle_callback,      pattern="^astro:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    log.info("✅ Bot en línea.")
    app.run_polling(drop_pending_updates=False)
