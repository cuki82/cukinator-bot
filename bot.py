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
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

def generar_pdf(carta: dict) -> str:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Fuente Unicode (DejaVu Mono soporta tildes, ñ, °, ℞, ☌, etc.)
    pdf.add_font("Mono",  "",  FONT_REGULAR)
    pdf.add_font("Mono",  "B", FONT_BOLD)

    NL = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}

    def fila(txt, bold=False):
        pdf.set_font("Mono", "B" if bold else "", 8)
        pdf.cell(0, 5, txt, **NL)

    # Título
    pdf.set_font("Mono", "B", 13)
    pdf.cell(0, 8, "CARTA NATAL — FICHA TÉCNICA ESTRUCTURAL", align="C", **NL)
    fila("─" * 95)

    d = carta["debug"]
    fila(f"Fecha : {d['fecha_original']}   {d['hora_ut']}")
    fila(f"Lugar : {d['lugar_geocodificado'][:80]}")
    fila(f"Coords: {d['lat']}N  {d['lon']}E   TZ: {d['timezone']}   JD: {d['jd_ut']}")
    fila("─" * 95)

    # Planetas
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
    fila("ASPECTOS MAYORES (orbe <= 5 grados)", bold=True)
    if carta["aspectos"]:
        fila(f"  {'Planeta 1':<14} {'Aspecto':<16} {'Planeta 2':<14} {'Orbe'}")
        fila(f"  {'─'*14} {'─'*16} {'─'*14} {'─'*6}")
        for a in carta["aspectos"]:
            fila(f"  {a['planeta1']:<14} {a['aspecto']:<16} {a['planeta2']:<14} {a['orb']} grados")
    else:
        fila("  (Sin aspectos mayores dentro del orbe)")

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
    }
]

SYSTEM_PROMPT = """CONFIGURACION PERSISTENTE: Tenés acceso a Railway DB para guardar y leer configuraciones. Cuando el usuario diga guardar, dejar fijo, a partir de ahora, esta es la regla, etc., usá config_guardar automáticamente. Cuando el usuario pida ver configs, usá config_listar o config_leer. La DB es la fuente de verdad.

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
- Antes de enviar: mostrás borrador y preguntás "¿lo mando?" en una línea.
- Si el usuario dice "el primero", "ese", "contestale", sabés de qué habla por contexto.

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

AUDIOS:
- Cuando el usuario manda un audio, ya está transcripto. Respondé directamente al contenido.
- Solo mostrás la transcripción si el usuario la pide explícitamente."""

# ── Claude ─────────────────────────────────────────────────────────────────────
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def get_system_prompt() -> str:
    import datetime
    hoy = datetime.datetime.now().strftime("%d de %B de %Y")
    return SYSTEM_PROMPT.replace("{{FECHA_HOY}}", hoy)

def ask_claude(chat_id: int, user_text: str) -> tuple:
    """Retorna (respuesta_texto, pdf_path_o_None, archivos_extra)
       archivos_extra = lista de (nombre, bytes, caption)
    """
    history = get_history_full(chat_id, limit=MAX_HISTORY, db_path=DB_PATH)
    history.append({"role": "user", "content": user_text})
    messages = history.copy()
    pdf_path    = None
    extra_files = []  # [(nombre, bytes, caption)]

    while True:
        response = claude.messages.create(
            model="claude-opus-4-5",
            max_tokens=2048,
            system=get_system_prompt(),
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":

                    if block.name == "search_web":
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
                                pdf_path = generar_pdf(carta)
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
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, threading, queue
    chat_id  = update.effective_chat.id
    user_msg = update.message.text
    name     = update.effective_user.first_name or "Usuario"
    log.info(f"[{chat_id}] {name}: {user_msg}")
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        q = queue.Queue()

        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, user_msg)))
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
        try:
            await update.message.reply_text(reply)
        except Exception:
            import re
            plain = re.sub(r'[*_`\[\]()~>#+\-=|{}.!]', '', reply)
            await update.message.reply_text(plain)

        if pdf_path:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f,
                    filename="carta_natal.pdf", caption="Ficha tecnica - Carta Natal")

        for nombre_f, contenido, caption in extra_files:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            await context.bot.send_document(chat_id=chat_id,
                document=io.BytesIO(contenido), filename=nombre_f, caption=caption)

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
                q.put(("ok", ask_claude(chat_id, texto)))
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
        try:
            await update.message.reply_text(reply)
        except Exception:
            import re
            plain = re.sub(r'[*_`\[\]()~>#+\-=|{}.!]', '', reply)
            await update.message.reply_text(plain)

        if pdf_path:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f,
                    filename="carta_natal.pdf", caption="Ficha tecnica - Carta Natal")

        for nombre_f, contenido, caption in extra_files:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            await context.bot.send_document(chat_id=chat_id,
                document=io.BytesIO(contenido), filename=nombre_f, caption=caption)

    except Exception as e:
        log.error(f"Error en voz: {e}")
        await update.message.reply_text("No pude procesar el audio, intentalo de nuevo.")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or ""
    await update.message.reply_text(
        f"Hola {name}! Soy Cukinator. Puedo conversar, buscar en internet y calcular cartas natales astrológicas. "
        f"Para la carta natal decime fecha, hora y lugar de nacimiento. "
        f"Usá /reset para borrar el historial."
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
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("cartas", cmd_cartas))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern="^astro:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    log.info("✅ Bot en línea.")
    app.run_polling(drop_pending_updates=False)
