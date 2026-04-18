"""
handlers/message_handler.py
Maneja mensajes de texto y audio del usuario.
Importa las funciones de negocio desde bot_core.py (el monolítico renombrado).
"""
import os
import io
import sys
import logging
import asyncio
import threading
import queue
import subprocess
import tempfile

from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Importar core desde el módulo principal
from core.bot_core import (
    ask_claude, save_message_full, send_long_message,
    texto_a_voz, es_respuesta_larga, DB_PATH, OWNER_CHAT_ID
)
try:
    from agents.intent_router import classify as _classify_intent
    from agents.worker_client import send_coding_task, format_worker_result
    _WORKER_ENABLED = True
except ImportError:
    _WORKER_ENABLED = False
    def _classify_intent(t): return "conversational"
    def format_worker_result(r): return r.get("summary", "")
    async def send_coding_task(t, c): return {"status": "error", "summary": "Worker no disponible"}


# ── Detección automática de API keys pegadas en el chat ───────────────────────
# Intenta match ordenado por especificidad. El primero que matchee gana para ese
# trozo de texto. El bot escribe directo al vault (corre en el mismo VPS) — la
# key nunca se loguea, nunca llega al LLM, nunca queda en el history del chat.

import re as _re_cred

_CREDENTIAL_PATTERNS = [
    # (regex, vault_key_name, service_display_name)
    # URLs de DB con creds (Postgres/Supabase) — matchean ANTES que otros
    # patterns de JWT, porque el URL puede contener `eyJ...` dentro del query string.
    (r"postgresql://[\w._\-]+:[^@\s]+@[\w.\-]+:\d+/\w+(?:\?[^\s]*)?",  "SUPABASE_DB_URL",   "Supabase/Postgres DB URL"),
    (r"postgres://[\w._\-]+:[^@\s]+@[\w.\-]+:\d+/\w+(?:\?[^\s]*)?",    "SUPABASE_DB_URL",   "Postgres DB URL"),
    (r"https://[a-z0-9]+\.supabase\.co",                                "SUPABASE_URL",      "Supabase REST URL"),
    (r"sbp_[A-Za-z0-9]{40,}",                                           "SUPABASE_SERVICE_KEY","Supabase service key"),
    # API keys por servicio
    (r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{60,}",       "ANTHROPIC_KEY",     "Anthropic API"),
    (r"sk-proj-[A-Za-z0-9_\-]{80,}",                "OPENAI_API_KEY",    "OpenAI (project)"),
    (r"sk-[A-Za-z0-9]{40,}",                        "OPENAI_API_KEY",    "OpenAI (legacy)"),
    (r"github_pat_[A-Za-z0-9_]{50,}",               "GITHUB_TOKEN",      "GitHub PAT fine-grained"),
    (r"ghp_[A-Za-z0-9]{30,}",                       "GITHUB_TOKEN",      "GitHub PAT classic"),
    (r"xoxb-\d+-\d+-[A-Za-z0-9]+",                  "SLACK_BOT_TOKEN",   "Slack bot"),
    (r"AKIA[A-Z0-9]{16}",                           "AWS_ACCESS_KEY_ID", "AWS access key"),
    (r"xai-[A-Za-z0-9]{40,}",                       "XAI_API_KEY",       "xAI"),
    (r"gsk_[A-Za-z0-9]{40,}",                       "GROQ_API_KEY",      "Groq"),
    (r"AIza[A-Za-z0-9_\-]{35}",                     "GOOGLE_API_KEY",    "Google"),
    (r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}", "JWT_TOKEN", "JWT"),
]


def _mask_cred(value: str) -> str:
    """Muestra primeros 10 y últimos 4 chars, el resto oculto."""
    if len(value) <= 18:
        return value[:4] + "…" + value[-2:]
    return value[:10] + "…" + value[-4:]


# Placeholders típicos que aparecen en templates de Supabase/Postgres y no
# deben guardarse como credencial real. Si el valor contiene cualquiera de
# estos, lo rechazamos y avisamos al user.
_PLACEHOLDER_MARKERS = [
    "[YOUR-PASSWORD]", "[YOUR-DB-PASSWORD]", "[PASSWORD]", "[your-password]",
    "<password>", "<YOUR_PASSWORD>", "YOUR_PASSWORD", "your_password",
    "xxxxxxxx", "CHANGEME", "changeme",
]


def _looks_like_placeholder(value: str) -> bool:
    """True si el valor parece un template sin password real rellenada."""
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)


def _detect_credentials(text: str) -> list:
    """Busca API keys conocidas en el texto. Retorna lista de (vault_key, value, service).
    Descarta valores que contengan placeholders obvios (ej. [YOUR-PASSWORD])."""
    if not text:
        return []
    found = []
    seen_values = set()
    for pattern, vault_key, service in _CREDENTIAL_PATTERNS:
        for m in _re_cred.finditer(pattern, text):
            v = m.group(0)
            if v in seen_values:
                continue
            if _looks_like_placeholder(v):
                # El user pegó un template con [YOUR-PASSWORD] sin rellenar.
                # No lo guardamos: sobreescribiría una credencial real si ya estuviera.
                log.warning(f"skip creds con placeholder: {service}")
                continue
            seen_values.add(v)
            found.append((vault_key, v, service))
    return found


# ── Confirmación explícita antes de escribir al vault ────────────────────
# El bot NUNCA escribe una credencial al vault sin que el user apriete un
# botón de confirmación. Esto evita sobrescribir accidentalmente una key
# real con un template con placeholder (bug que nos pasó con Supabase) y
# evita que el bot actúe sobre un mensaje de prompt injection que incluya
# una API key disfrazada.
#
# Flujo:
#   1. user pega credencial → detectamos → guardamos pending en memoria
#   2. bot responde con botones: ✅ Guardar · ❌ Cancelar
#   3. user toca Guardar → ahí escribimos al vault (con backup de la anterior)
#   4. timeout 5 min sin confirmar → se borra el pending

import time as _time
import uuid as _uuid
import threading as _threading

_PENDING_CREDS: dict = {}
_PENDING_LOCK = _threading.Lock()
_PENDING_TTL_SECS = 300  # 5 minutos


def _pending_cleanup():
    """Borra pendings expirados. Llamado oportunísticamente."""
    now = _time.time()
    with _PENDING_LOCK:
        expired = [k for k, v in _PENDING_CREDS.items() if now - v["ts"] > _PENDING_TTL_SECS]
        for k in expired:
            del _PENDING_CREDS[k]


def _pending_create(creds: list, chat_id: int) -> str:
    """Guarda un set de credenciales como 'pendientes de confirmación'.
    Retorna el ID del pending (corto, para usar en callback_data)."""
    _pending_cleanup()
    pid = _uuid.uuid4().hex[:10]
    with _PENDING_LOCK:
        _PENDING_CREDS[pid] = {"creds": creds, "chat_id": chat_id, "ts": _time.time()}
    return pid


def _pending_consume(pid: str, chat_id: int):
    """Saca un pending por ID. Verifica que sea del mismo chat_id (seguridad)."""
    with _PENDING_LOCK:
        entry = _PENDING_CREDS.pop(pid, None)
    if not entry:
        return None
    if entry["chat_id"] != chat_id:
        return None
    if _time.time() - entry["ts"] > _PENDING_TTL_SECS:
        return None
    return entry["creds"]


async def handle_vault_callback(update, context):
    """Callback handler para los botones ✅/❌ del vault. Pattern: vault:<action>:<pid>"""
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":", 2)
    if len(parts) < 3:
        return
    _, action, pid = parts
    chat_id = q.message.chat.id if q.message else None

    if action == "cancel":
        with _PENDING_LOCK:
            _PENDING_CREDS.pop(pid, None)
        await q.edit_message_text("❌ Cancelado. No guardé nada en el vault.")
        return

    if action == "apply":
        creds = _pending_consume(pid, chat_id)
        if not creds:
            await q.edit_message_text(
                "⏱️ La confirmación expiró (5 min) o no coincide el chat. "
                "Reenviá la credencial si querés volver a intentarlo."
            )
            return
        try:
            from services.vault import set as vault_set, get as vault_get
        except Exception as e:
            await q.edit_message_text(f"❌ No pude abrir el vault: {e}")
            return

        result_lines = []
        for vault_key, value, service in creds:
            existing = vault_get(vault_key)
            try:
                replaced = False
                if existing and existing != value:
                    # Backup de la credencial anterior antes de sobrescribir
                    ts = int(_time.time())
                    vault_set(f"{vault_key}_backup_{ts}", existing)
                    replaced = True
                vault_set(vault_key, value)
                tag = "🔄 reemplazó la anterior (backup guardado)" if replaced else "✨ nueva"
                result_lines.append(f"• *{service}* → `{vault_key}` {tag}")
                log.info(f"[{chat_id}] vault confirmed: {vault_key} (len={len(value)}, replaced={replaced})")
            except Exception as e:
                result_lines.append(f"• *{service}* → ❌ {e}")
                log.error(f"[{chat_id}] vault set {vault_key} falló: {e}")

        await q.edit_message_text(
            "✅ *Guardado en el vault del VPS*\n\n" + "\n".join(result_lines) +
            "\n\n_El próximo restart del bot/worker levanta los nuevos valores._",
            parse_mode="Markdown",
        )


async def cmd_setvault(update, context):
    """Comando /setvault <KEY> <value> — guarda al vault con confirmación inline.
    Reconocido como command antes que cualquier LLM toque el mensaje. Solo
    el owner puede ejecutarlo. Uso típico: passwords sueltas sin prefijo
    conocido (Supabase DB password, LiteLLM master key, etc)."""
    from core.bot_core import OWNER_CHAT_ID
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("🚫 Solo el owner puede escribir al vault.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "*Uso:* `/setvault <KEY_NAME> <value>`\n\n"
            "Ejemplo:\n"
            "`/setvault SUPABASE_DB_PASSWORD mi-password-nueva`\n\n"
            "El bot pide confirmación antes de guardar al vault. "
            "Después borrá tu mensaje de Telegram (tap largo → Delete).",
            parse_mode="Markdown",
        )
        return

    key_name = args[0].strip()
    value = " ".join(args[1:]).strip()

    # Validar key_name: alfanumérico + underscore + guiones, máx 64 chars
    if not key_name or not all(c.isalnum() or c in "_-" for c in key_name) or len(key_name) > 64:
        await update.message.reply_text(
            "❌ *KEY_NAME inválido.* Usá solo A-Z, 0-9, `_` y `-` (máx 64 chars).",
            parse_mode="Markdown",
        )
        return

    if len(value) < 4 or _looks_like_placeholder(value):
        await update.message.reply_text(
            "❌ Valor inválido o es un placeholder sin rellenar. No guardé nada.",
        )
        return

    # Chequear si sobrescribe una credencial existente (para el mensaje)
    try:
        from services.vault import get as vault_get
        existing = vault_get(key_name)
    except Exception:
        existing = None

    pid = _pending_create([(key_name, value, "Vault")], chat_id)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Guardar", callback_data=f"vault:apply:{pid}"),
        InlineKeyboardButton("❌ Cancelar", callback_data=f"vault:cancel:{pid}"),
    ]])

    warning = ""
    if existing and existing != value:
        warning = "\n⚠️ *sobrescribe* la actual (backup automático)"
    elif existing:
        warning = "\n ℹ️ idéntica a la actual"

    await update.message.reply_text(
        f"🔐 *Guardar al vault*\n\n"
        f"• key: `{key_name}`\n"
        f"• valor: `{_mask_cred(value)}`"
        f"{warning}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🛡️ No escribo sin que toques *Guardar*.\n"
        f"⏱️ Expira en 5 min.\n\n"
        f"💡 *Después de confirmar, borrá tu mensaje `/setvault ...` "
        f"de Telegram* (tap largo → Delete for bot too).",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _handle_credential_paste(update, _context, user_msg: str) -> bool:
    """Si el mensaje contiene API keys, muestra confirmación y queda pendiente
    hasta que el user apriete ✅ Guardar. Retorna True si consumió el mensaje."""
    creds = _detect_credentials(user_msg)
    if not creds:
        # Chequear si el user pegó un template con placeholder sin rellenar
        if any(marker in user_msg for marker in _PLACEHOLDER_MARKERS):
            await update.message.reply_text(
                "⚠️ Parece que pegaste un template de connection string con "
                "`[YOUR-PASSWORD]` (u otro placeholder) sin reemplazar por la "
                "password real.\n\nReemplazá el placeholder por la password y "
                "reenviámelo. No guardé nada.",
                parse_mode="Markdown",
            )
            return True
        return False

    chat_id = update.effective_chat.id
    log.info(f"[{chat_id}] Detectadas {len(creds)} credencial(es), esperando confirmación")

    # No guardamos nada todavía — dejamos pending y mostramos botones
    try:
        from services.vault import get as vault_get
    except Exception:
        vault_get = lambda k: None

    preview_lines = []
    for vault_key, value, service in creds:
        existing = vault_get(vault_key) if vault_get else None
        warning = ""
        if existing and existing != value:
            warning = f" · ⚠️ *sobrescribe* la actual (backup automático)"
        elif existing and existing == value:
            warning = " · ℹ️ idéntica a la actual"
        preview_lines.append(
            f"• *{service}* → `{vault_key}`\n  valor: `{_mask_cred(value)}`{warning}"
        )

    pid = _pending_create(creds, chat_id)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Guardar", callback_data=f"vault:apply:{pid}"),
        InlineKeyboardButton("❌ Cancelar", callback_data=f"vault:cancel:{pid}"),
    ]])

    await update.message.reply_text(
        "🔐 *Credencial detectada — confirmá antes de guardar*\n\n"
        + "\n".join(preview_lines) +
        "\n\n━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ No escribo al vault sin que toques *Guardar*.\n"
        "⏱️ La confirmación expira en 5 minutos.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, threading, queue
    chat_id  = update.effective_chat.id
    user_msg = update.message.text
    name     = update.effective_user.first_name or "Usuario"

    # Detección de credenciales — DEBE ir antes del log para evitar filtrar la
    # key en journalctl. Si se detecta key, el handler escribe al vault, responde
    # al user, y retorna sin pasar por el LLM ni guardar en el history.
    if await _handle_credential_paste(update, context, user_msg):
        return

    # (sin buffer)

    log.info(f"[{chat_id}] {name}: {user_msg[:80]}{'...' if len(user_msg)>80 else ''}")

    msg_lower = user_msg.strip().lower()
    if msg_lower in ("menu", "menú", "abri el menu", "abrí el menú", "ver menu", "ver menú"):
        from handlers.callback_handler import cmd_menu
        await cmd_menu(update, context)
        return
    if msg_lower in ("biblioteca", "librería", "libreria", "knowledge base", "kb", "kb reaseguros"):
        from handlers.callback_handler import cmd_biblioteca
        await cmd_biblioteca(update, context)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Context-aware routing: si el bot en su último mensaje estaba pidiendo datos
    # de nacimiento (fecha/hora/lugar), el follow-up con esos datos se clasifica
    # como astrology aunque el intent router keyword-based lo diera conversational.
    classified = _classify_intent(user_msg)
    try:
        import re as _re_ctx
        import sqlite3 as _sl
        _con_ctx = _sl.connect(DB_PATH)
        _last_bot_msg = _con_ctx.execute(
            "SELECT content FROM messages WHERE chat_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        _con_ctx.close()
        if _last_bot_msg and _last_bot_msg[0]:
            _prev = _last_bot_msg[0].lower()
            # Detectar señal de follow-up astrológico
            _astro_trigger = any(k in _prev for k in [
                "fecha de nacimiento", "datos de nacimiento", "fecha, hora", "fecha hora", "cuándo nació",
                "cuándo naciste", "dónde naciste", "dónde nació", "pasame los datos",
                "ficha natal", "perfil astro",
            ])
            # Detectar si el user respondió con formato de datos (fecha + eventualmente hora/lugar)
            _has_date = bool(_re_ctx.search(r"\b\d{1,2}[/\-\s]\d{1,2}[/\-\s]\d{2,4}\b", user_msg)) \
                or bool(_re_ctx.search(r"\b\d{1,2}\s+de\s+\w+\s+(?:de\s+)?\d{4}\b", user_msg, _re_ctx.IGNORECASE))
            if _astro_trigger and _has_date and classified == "conversational":
                classified = "astrology"
                log.info(f"[{chat_id}] ctx routing: {classified} (bot pidió datos, user respondió con fecha)")
    except Exception as _ctx_e:
        log.debug(f"ctx routing skip: {_ctx_e}")

    # Routing: coding intent -> agent_worker en el VPS
    if _WORKER_ENABLED and classified == "coding":
        await update.message.reply_text("Entendido, lo proceso con el Agent Worker en el VPS...")
        try:
            result = await send_coding_task(user_msg, chat_id)
            reply_text = format_worker_result(result)
            if os.environ.get("BOT_TRACE", "").lower() in ("true", "1"):
                elapsed = result.get("duration_s") or result.get("elapsed_seconds") or result.get("duration") or "?"
                status  = result.get("status", "?")
                modified = result.get("modified_files") or []
                errors_n = len(result.get("errors") or [])
                status_emoji = {"ok": "✅", "partial": "⚠️", "error": "❌", "busy": "🚦", "timeout": "⏰"}.get(status, "❓")
                files_part = f"\n📝 Archivos tocados: {len(modified)}" if modified else ""
                errors_part = f"\n⚠️ {errors_n} error(es)" if errors_n else ""
                reply_text += (
                    f"\n\n━━━━━━━━━━━━━━━━━━━\n"
                    f"📥 *De:* {name} (chat {chat_id})\n"
                    f"🎯 *Intent router* → `coding` (keyword-based, sin LLM)\n"
                    f"📤 *Bot handler* → *Agent Worker* (VPS :3335)\n"
                    f"   🧠 Plan     → *Codex* `gpt-5-codex` via /v1/responses\n"
                    f"   ⚡ Ejecuta  → *Claude Code CLI* (Opus via Anthropic API)\n"
                    f"   📝 Summary  → *Codex* `gpt-5-codex`\n"
                    f"↪️ *Agent Worker* → Bot handler → *{name}*\n"
                    f"⏱️ Latencia total: {elapsed}s · {status_emoji} status: {status}"
                    f"{files_part}{errors_part}"
                )
            save_message_full(chat_id, "user", user_msg, db_path=DB_PATH)
            save_message_full(chat_id, "assistant", reply_text, db_path=DB_PATH)
            await send_long_message(context.bot, chat_id, reply_text, reply_to=update.message)
        except Exception as e:
            log.error(f"worker_client error: {e}")
            await update.message.reply_text(f"Error con el Agent Worker: {e}")
        return

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
        vps_hint_sent = False
        while t.is_alive() and elapsed < 180:
            await asyncio.sleep(4)
            elapsed += 4
            if t.is_alive():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                    # Hint visual si tarda más de 12s (probablemente está ejecutando SSH)
                    if elapsed == 12 and not vps_hint_sent:
                        vps_kw = any(w in user_msg.lower() for w in
                            ["vps","docker","container","ssh","litellm","ollama","webui","servidor","open-web"])
                        if vps_kw:
                            await update.message.reply_text("Conectando al VPS...")
                            vps_hint_sent = True
                except Exception:
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

        pidio_voz_explicito = any(w in user_msg.lower() for w in
            ["voz", "audio", "escuchar", "hablame", "háblame",
             "respondé con voz", "responde con voz", "mandame un audio"])
        if not pidio_voz_explicito:
            extra_files = [(n, c, cap) for n, c, cap in extra_files if cap != "voice"]

        await send_long_message(context.bot, chat_id, reply, reply_to=update.message)

        if pdf_path:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f,
                    filename="carta_natal.pdf", caption="Ficha tecnica - Carta Natal")

        for nombre_f, contenido, caption in extra_files:
            try:
                if caption == "voice":
                    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
                    await context.bot.send_voice(chat_id=chat_id, voice=io.BytesIO(contenido))
                elif caption == "video_link":
                    lines = contenido.decode().split("\n")
                    msg = "\n".join(lines[:3])
                    await context.bot.send_message(chat_id=chat_id, text=msg)
                elif caption.startswith("video|"):
                    titulo_vid = caption.split("|", 1)[1]
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
                    await context.bot.send_video(chat_id=chat_id,
                        video=io.BytesIO(contenido), filename=nombre_f,
                        caption=titulo_vid, supports_streaming=True)
                else:
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await context.bot.send_document(chat_id=chat_id,
                        document=io.BytesIO(contenido), filename=nombre_f, caption=caption)
            except Exception as ve:
                log.error(f"[{chat_id}] Error enviando {caption}: {ve}")

        log.info(f"[{chat_id}] Bot: {reply[:80]}...")
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()[-500:]
        log.error(f"Error en handle_message: {e}\n{err_detail}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, tempfile, subprocess, threading, queue, sys
    chat_id = update.effective_chat.id
    name    = update.effective_user.first_name or "Usuario"

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        log.info(f"[{chat_id}] Transcribiendo audio de {name}...")
        loop = asyncio.get_event_loop()
        # transcribe.py vive en core/ desde la restructura Silicon Valley.
        # Resolvemos el path desde la ubicación de este handler (handlers/ -> ../core/).
        from pathlib import Path as _Path
        _transcribe_script = str(_Path(__file__).resolve().parent.parent / "core" / "transcribe.py")

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, _transcribe_script, tmp_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
                texto = stdout.decode().strip()
            except asyncio.TimeoutError:
                proc.kill()
                texto = ""
        except Exception as e:
            log.error(f"Error transcripcion: {e}")
            texto = ""
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        if not texto or texto.startswith("ERROR:"):
            await update.message.reply_text("No pude entender el audio, manda de nuevo.")
            return

        log.info(f"[{chat_id}] Transcripcion: {texto}")
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Detectar si el usuario pidió explicitamente respuesta en audio.
        # Default: audio in -> texto out. Solo si hay una frase explícita
        # tipo "respondeme con audio", "hablame", etc., activamos la voz.
        import re as _re
        _VOICE_REQUEST_PATTERNS = [
            r"\brespond[eé]me (?:con|en) (?:un )?(?:audio|voz)\b",
            r"\bcontest[aá]me (?:con|en) (?:un )?(?:audio|voz)\b",
            r"\bmand[aá]me (?:un )?(?:audio|voz)\b",
            r"\b(?:respond[eé]|contest[aá]) (?:con|en) (?:audio|voz)\b",
            r"\bhabl[aá]me\b", r"\bescuch[aá]me\b",
            r"\ben audio\b", r"\bcon voz\b",
        ]
        _pidio_audio = any(_re.search(p, (texto or "").lower()) for p in _VOICE_REQUEST_PATTERNS)
        log.info(f"[{chat_id}] pidio_audio={_pidio_audio}")

        q = queue.Queue()

        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, texto, user_name=name, allow_voice=_pidio_audio)))
            except Exception as e:
                q.put(("err", str(e)))

        t = threading.Thread(target=run_claude, daemon=True)
        t.start()

        elapsed = 0
        while t.is_alive() and elapsed < 120:
            await asyncio.sleep(4)
            elapsed += 4
            if t.is_alive():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass

        t.join(timeout=1)
        if t.is_alive():
            await update.message.reply_text("Tardo demasiado, intentalo de nuevo.")
            return

        status, payload = q.get(timeout=2)
        if status == "err":
            raise Exception(payload)

        reply, pdf_path, extra_files = payload
        save_message_full(chat_id, "user",      texto, db_path=DB_PATH)
        save_message_full(chat_id, "assistant", reply, db_path=DB_PATH)

        tiene_voz = any(cap == "voice" for _, _, cap in extra_files)
        if _pidio_audio and not tiene_voz and reply and not es_respuesta_larga(reply):
            ogg_path = texto_a_voz(reply)
            if ogg_path:
                with open(ogg_path, "rb") as f:
                    extra_files.append(("respuesta.ogg", f.read(), "voice"))
                os.unlink(ogg_path)
                tiene_voz = True

        # Siempre mandar texto también (excepto si ya se generó audio por
        # pedido explícito — en ese caso solo audio, como tenés programado).
        if not tiene_voz:
            await send_long_message(context.bot, chat_id, reply, reply_to=update.message)

        if pdf_path:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
            with open(pdf_path, "rb") as f:
                await context.bot.send_document(chat_id=chat_id, document=f,
                    filename="carta_natal.pdf", caption="Ficha tecnica - Carta Natal")

        for nombre_f, contenido, caption in extra_files:
            try:
                if caption == "voice":
                    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
                    await context.bot.send_voice(chat_id=chat_id, voice=io.BytesIO(contenido))
                    log.info(f"[{chat_id}] Voz enviada OK: {len(contenido)} bytes")
                elif caption == "video_link":
                    lines = contenido.decode().split("\n")
                    await context.bot.send_message(chat_id=chat_id, text="\n".join(lines[:3]))
                else:
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_document")
                    await context.bot.send_document(chat_id=chat_id,
                        document=io.BytesIO(contenido), filename=nombre_f, caption=caption)
            except Exception as ve:
                log.error(f"[{chat_id}] Error enviando {caption}: {ve}")

    except Exception as e:
        log.error(f"Error en voz: {e}")
        await update.message.reply_text("No pude procesar el audio, intentalo de nuevo.")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja documentos enviados al bot (PDF, TXT, etc.)"""
    import tempfile, os
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or "Usuario"
    doc = update.message.document

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    # Solo PDF y texto por ahora
    if doc.mime_type not in ("application/pdf", "text/plain"):
        await update.message.reply_text(f"Por ahora solo proceso PDF y TXT. Recibí: {doc.mime_type}")
        return

    try:
        # Descargar archivo
        tg_file = await context.bot.get_file(doc.file_id)
        suffix = ".pdf" if doc.mime_type == "application/pdf" else ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        log.info(f"[{chat_id}] Documento recibido: {doc.file_name} ({doc.file_size} bytes)")

        # Extraer texto
        texto = ""
        if doc.mime_type == "application/pdf":
            try:
                import pypdf
                with open(tmp_path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        texto += page.extract_text() or ""
            except ImportError:
                try:
                    import pdfminer.high_level as pdfminer
                    texto = pdfminer.extract_text(tmp_path)
                except ImportError:
                    await update.message.reply_text("Necesito instalar pypdf para leer PDFs. Avisale al admin.")
                    return
        else:
            with open(tmp_path, "r", errors="replace") as f:
                texto = f.read()

        os.unlink(tmp_path)

        if not texto.strip():
            await update.message.reply_text("No pude extraer texto del documento. ¿Es un PDF escaneado (imagen)?")
            return

        # Truncar si es muy largo
        texto_truncado = texto[:12000]
        truncado = len(texto) > 12000

        # Pasar a Claude con contexto
        caption = update.message.caption or ""
        prompt = f"El usuario envió el documento '{doc.file_name}'"
        if caption:
            prompt += f" con el mensaje: '{caption}'"
        prompt += f".\n\nContenido del documento ({len(texto)} caracteres"
        if truncado:
            prompt += ", truncado a 12000"
        prompt += f"):\n\n{texto_truncado}"

        await update.message.reply_text(f"Documento recibido: {doc.file_name} ({len(texto)} caracteres). Procesando...")
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        import queue, threading
        from core.bot_core import ask_claude, save_message_full, send_long_message, DB_PATH

        q = queue.Queue()
        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, prompt, user_name=name)))
            except Exception as e:
                q.put(("err", str(e)))

        t = threading.Thread(target=run_claude, daemon=True)
        t.start()

        elapsed = 0
        while t.is_alive() and elapsed < 180:
            await asyncio.sleep(4)
            elapsed += 4
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

        t.join(timeout=1)
        if t.is_alive():
            await update.message.reply_text("Tardó demasiado procesando el documento.")
            return

        status, payload = q.get(timeout=2)
        if status == "err":
            raise Exception(payload)

        reply, _, extra_files = payload
        save_message_full(chat_id, "user", prompt[:500], db_path=DB_PATH)
        save_message_full(chat_id, "assistant", reply, db_path=DB_PATH)
        await send_long_message(context.bot, chat_id, reply, reply_to=update.message)

    except Exception as e:
        log.error(f"Error procesando documento: {e}")
        await update.message.reply_text(f"Error procesando el documento: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja fotos enviadas al bot — las pasa a Claude con visión."""
    import tempfile, os, base64
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or "Usuario"

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Tomar la foto de mayor resolución
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        with open(tmp_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        os.unlink(tmp_path)

        caption = update.message.caption or "Analizá esta imagen y describí qué ves, especialmente colores, tipografías, logos y elementos de diseño."

        log.info(f"[{chat_id}] Foto recibida de {name}")

        # Llamar a Claude con visión directamente
        import queue, threading
        from core.bot_core import claude, get_system_prompt, save_message_full, send_long_message, DB_PATH

        q = queue.Queue()

        def run_claude():
            try:
                response = claude.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=2048,
                    system=get_system_prompt(user_name=name, chat_id=chat_id),
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img_b64
                                }
                            },
                            {
                                "type": "text",
                                "text": caption
                            }
                        ]
                    }]
                )
                text = response.content[0].text if response.content else "No pude analizar la imagen."
                q.put(("ok", text))
            except Exception as e:
                q.put(("err", str(e)))

        t = threading.Thread(target=run_claude, daemon=True)
        t.start()

        elapsed = 0
        while t.is_alive() and elapsed < 60:
            await asyncio.sleep(3)
            elapsed += 3
            try:
                await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                pass

        t.join(timeout=1)
        status, payload = q.get(timeout=2)

        if status == "err":
            raise Exception(payload)

        save_message_full(chat_id, "user", f"[foto] {caption}", db_path=DB_PATH)
        save_message_full(chat_id, "assistant", payload, db_path=DB_PATH)
        await send_long_message(context.bot, chat_id, payload, reply_to=update.message)

    except Exception as e:
        log.error(f"Error procesando foto: {e}")
        await update.message.reply_text(f"Error procesando la imagen: {e}")
