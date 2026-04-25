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


async def cmd_usage(update, context):
    """Uso mensual de tokens + costo estimado. /usage [slug].
    Sin args: muestra el tenant del chat_id actual."""
    from core.bot_core import OWNER_CHAT_ID
    chat_id = update.effective_chat.id
    try:
        from services.tenants import resolve_tenant, list_tenants
        from services.usage import get_period
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    args = context.args or []
    if args and chat_id == OWNER_CHAT_ID:
        slug = args[0]
    else:
        slug = resolve_tenant(chat_id)

    u = get_period(slug)
    if not u:
        await update.message.reply_text(f"Sin datos de consumo para `{slug}`.", parse_mode="Markdown")
        return
    tin = u.get("tokens_in", 0)
    tout = u.get("tokens_out", 0)
    cost = u.get("cost_usd", 0.0)
    msgs = u.get("msg_count", 0)
    msg = (
        f"📊 *Consumo del mes — `{slug}`*\n\n"
        f"• Mensajes: {msgs}\n"
        f"• Tokens in:  {tin:,}\n"
        f"• Tokens out: {tout:,}\n"
        f"• Total:      {tin + tout:,}\n"
        f"• Costo estimado: *${cost:.4f} USD*\n"
    )
    if chat_id == OWNER_CHAT_ID and not args:
        # Mostrar resumen de TODOS los tenants también
        tenants = list_tenants()
        if len(tenants) > 1:
            msg += "\n━━━━━━━━━━━━━━━━━━━\n*Todos los tenants:*\n"
            total_cost = 0.0
            for t in tenants:
                tu = get_period(t["slug"])
                c = tu.get("cost_usd", 0.0)
                total_cost += c
                msg += f"• `{t['slug']}`: ${c:.4f} USD · {tu.get('msg_count',0)} msgs\n"
            msg += f"\n*Total workspace: ${total_cost:.4f} USD*"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_tenant(update, context):
    """Administración de tenants (solo owner).

    /tenant list
    /tenant info <slug>
    /tenant set_prompt <slug> <texto del system prompt>
    /tenant set_tools <slug> <tool1,tool2,tool3>
    /tenant set_lang <slug> <es-AR|en-US|...>
    /tenant link <slug> <chat_id>
    """
    from core.bot_core import OWNER_CHAT_ID
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("🚫 Solo el owner administra tenants.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "*Uso:*\n"
            "`/tenant list`\n"
            "`/tenant info <slug>`\n"
            "`/tenant set_prompt <slug> <texto>`\n"
            "`/tenant set_tools <slug> <tool1,tool2,...>`\n"
            "`/tenant set_lang <slug> <es-AR>`\n"
            "`/tenant link <slug> <chat_id>`",
            parse_mode="Markdown",
        )
        return

    try:
        from services.tenants import list_tenants, get_tenant_config, set_tenant_config, link_chat_to_tenant
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return

    sub = args[0]
    try:
        if sub == "list":
            tenants = list_tenants()
            if not tenants:
                await update.message.reply_text("Sin tenants.")
                return
            lines = ["*Tenants:*"]
            for t in tenants:
                lines.append(f"• `{t['slug']}` — {t['name']} · {t.get('email') or '—'}")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

        elif sub == "info" and len(args) >= 2:
            slug = args[1]
            cfg = get_tenant_config(slug)
            sp = cfg.get("system_prompt") or "(sin override)"
            st = cfg.get("settings") or {}
            await update.message.reply_text(
                f"*Tenant `{slug}`*\n"
                f"idioma: `{cfg.get('display_language')}`\n"
                f"tools_enabled: `{st.get('tools_enabled') or 'todas'}`\n"
                f"system_prompt override:\n```\n{sp[:500]}\n```",
                parse_mode="Markdown",
            )

        elif sub == "set_prompt" and len(args) >= 3:
            slug = args[1]; prompt = " ".join(args[2:])
            set_tenant_config(slug, system_prompt=prompt)
            await update.message.reply_text(f"✅ System prompt actualizado para `{slug}` ({len(prompt)} chars).", parse_mode="Markdown")

        elif sub == "set_tools" and len(args) >= 3:
            slug = args[1]; tools = [t.strip() for t in args[2].split(",") if t.strip()]
            cfg = get_tenant_config(slug)
            new_settings = dict(cfg.get("settings") or {})
            new_settings["tools_enabled"] = tools
            set_tenant_config(slug, settings=new_settings)
            await update.message.reply_text(f"✅ Tools whitelist para `{slug}`: {tools}", parse_mode="Markdown")

        elif sub == "set_lang" and len(args) >= 3:
            slug = args[1]; lang = args[2]
            set_tenant_config(slug, display_language=lang)
            await update.message.reply_text(f"✅ Idioma de `{slug}` → `{lang}`", parse_mode="Markdown")

        elif sub == "link" and len(args) >= 3:
            slug = args[1]; new_chat = int(args[2])
            link_chat_to_tenant(new_chat, slug)
            await update.message.reply_text(f"✅ chat_id `{new_chat}` ↔ tenant `{slug}`", parse_mode="Markdown")

        else:
            await update.message.reply_text("Subcomando no reconocido. Mandá `/tenant` sin args para ver la ayuda.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


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
    chat_id    = update.effective_chat.id
    chat_type  = update.effective_chat.type
    chat_title = update.effective_chat.title
    user_msg = update.message.text
    name     = update.effective_user.first_name or "Usuario"

    # ── Group ACL ─────────────────────────────────────────────────────
    # Whitelist de grupos + filtro mention/reply/nick. Sin esto, el bot
    # contesta TODOS los mensajes del grupo cuando es admin (lee todo).
    # ADEMÁS: SIEMPRE acumulamos el msg al buffer de contexto del grupo
    # (incluso si vamos a ignorar), para que cuando el bot SÍ responda
    # tenga la conversación previa entre humanos como contexto y resuelva
    # referencias ("ella", "ese", nombres, etc.).
    from services.group_acl import is_group_chat_type, is_allowed_group, is_directed_to_bot
    from services.group_context import append_message as _gctx_append
    is_group_chat = is_group_chat_type(chat_type)
    if is_group_chat:
        if not is_allowed_group(chat_id):
            log.info(f"[{chat_id}] grupo NO whitelisted ({chat_title!r}) — ignorando msg de {name}")
            return
        # Acumulamos al buffer SIEMPRE (sea para nosotros o entre humanos)
        _gctx_append(chat_id, name, user_msg or "")
        if not is_directed_to_bot(update):
            log.info(f"[{chat_id}] grupo whitelisted pero msg NO dirigido al bot ({chat_title!r}) — ignorando msg de {name}: {user_msg[:50]!r}")
            return

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

    # ── Intent Router v2 · Layer 0: pending action confirmation ──
    # Si el turno anterior dejó una acción pendiente Y este mensaje es una
    # confirmación corta ("sí", "dale", "mandásela"), heredamos ese intent
    # y reescribimos el user_msg con la acción guardada — para que el worker
    # reciba contexto real, NO el "sí, mandásela" suelto.
    try:
        from services.intent_state import resolve_with_pending, clear_pending, log_classification
        _pending = resolve_with_pending(chat_id, user_msg)
    except Exception as _ipe:
        log.debug(f"intent_state import fail: {_ipe}")
        _pending = None
    if _pending:
        log.info(f"[{chat_id}] PENDING confirmation matched: intent={_pending.intent} action={_pending.action[:60]!r}")
        try:
            log_classification(chat_id, user_msg, _pending.intent, layer="pending",
                               confidence=1.0, metadata={"action": _pending.action})
        except Exception:
            pass
        # Reescribir el user_msg para que el handler downstream reciba la acción real
        user_msg = _pending.action
        classified = _pending.intent
        clear_pending(chat_id)
    else:
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

    # Hard guard: si el mensaje tiene señales fuertes de VPS/DevOps/código y
    # NO fue clasificado como coding, forzar routing al worker. Esto evita que
    # Haiku/Sonnet directo se pongan a leer/ejecutar en el VPS con sus tools
    # (aunque las tools peligrosas ya están en NEVER_LLM_TOOLS, reforzamos).
    import re as _re_guard
    _hard_vps_signals = [
        r"\b(systemctl|journalctl|docker|nginx|uvicorn)\b",
        r"\bvps\b", r"\bservidor\b.*(entrá|accede|conect)",
        r"\bssh\b", r"\bchmod\b", r"\bchown\b",
        r"/home/cukibot", r"\.service\b", r"\bcommit\b",
        r"\b(cat|grep|sed|awk|tail|head|find|ls|wc)\s+(-\w+\s+)?[/\w]",
    ]
    if (classified not in ("coding",)
            and any(_re_guard.search(p, user_msg, _re_guard.IGNORECASE) for p in _hard_vps_signals)
            and _WORKER_ENABLED):
        log.info(f"[{chat_id}] hard guard: forcing coding intent (vps signal detected)")
        try:
            from services.audit import log_event
            log_event(action="guard_forced_coding", resource="intent_router",
                      chat_id=chat_id, actor="system",
                      details={"original_intent": classified, "msg_preview": user_msg[:80]})
        except Exception:
            pass
        classified = "coding"

    # Routing: coding intent -> agent_worker en el VPS
    if _WORKER_ENABLED and classified == "coding":
        await update.message.reply_text("Entendido, lo proceso con el Agent Worker en el VPS...")
        try:
            result = await send_coding_task(user_msg, chat_id)
            reply_text = format_worker_result(result)
            if os.environ.get("BOT_TRACE", "").lower() in ("true", "1") and chat_id > 0:
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

    # ── En grupos, prefixear el user_msg con [Nombre] e inyectar contexto ──
    # del buffer del grupo. Esto resuelve dos bugs:
    #   1. Bot le decía "H" a Cuki / mezclaba nombres porque no sabía quién
    #      había escrito qué (el history pone role:user sin sender_name).
    #   2. Bot perdía contexto de la charla previa entre humanos.
    user_msg_for_claude = user_msg
    if is_group_chat:
        try:
            from services.group_context import get_context as _gctx
            _ctx = _gctx(chat_id, exclude_last=True)
        except Exception:
            _ctx = ""
        # Prefijo del sender + contexto del grupo previo. El system prompt
        # de grupo (group_acl.group_system_suffix) tiene la regla para
        # llamar al user por el nombre del prefijo.
        if _ctx:
            user_msg_for_claude = f"{_ctx}\n[{name}] {user_msg}"
        else:
            user_msg_for_claude = f"[{name}] {user_msg}"

    try:
        q = queue.Queue()

        def run_claude():
            try:
                pidio_voz = any(w in user_msg.lower() for w in
                    ["voz", "audio", "escuchar", "hablame", "háblame",
                     "respondé con voz", "responde con voz", "mandame un audio", "en audio"])
                q.put(("ok", ask_claude(chat_id, user_msg_for_claude, user_name=name, allow_voice=pidio_voz)))
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

        # ── Intent Router v2 · Layer 0: parsear tag [PENDING:intent:action] ──
        # Si el LLM emitió el tag, lo extraemos del reply (no se le muestra al
        # user) y guardamos la acción pendiente. Si el siguiente mensaje del
        # user es una confirmación corta, resolve_with_pending la hereda.
        try:
            from services.intent_state import extract_pending_tag, remember_pending
            reply, _tag = extract_pending_tag(reply)
            if _tag:
                _intent, _action = _tag
                remember_pending(chat_id, _intent, _action)
                log.info(f"[{chat_id}] PENDING tag emitido por LLM: intent={_intent} action={_action[:60]!r}")
        except Exception as _ie:
            log.debug(f"PENDING tag parse skip: {_ie}")

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
                elif nombre_f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
                    await context.bot.send_photo(chat_id=chat_id,
                        photo=io.BytesIO(contenido), caption=caption[:1024])
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
    chat_id    = update.effective_chat.id
    chat_type  = update.effective_chat.type
    chat_title = update.effective_chat.title
    name       = update.effective_user.first_name or "Usuario"

    # Group ACL — mismo filtro que handle_message para no consumir tokens en
    # grupos no whitelisted (Whisper transcribe + Claude respuesta = caro).
    # Para audios: en grupos solo procesar si es REPLY a un mensaje del bot
    # (no se puede "mencionar" en un audio).
    from services.group_acl import is_group_chat_type, is_allowed_group, is_directed_to_bot
    if is_group_chat_type(chat_type):
        if not is_allowed_group(chat_id):
            log.info(f"[{chat_id}] AUDIO en grupo NO whitelisted ({chat_title!r}) — ignorando")
            return
        if not is_directed_to_bot(update):
            log.info(f"[{chat_id}] AUDIO en grupo no dirigido al bot ({chat_title!r}) — ignorando (hace reply al bot para hablarle por audio)")
            return

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

        # Comportamiento default: si el user mandó audio, respondemos con
        # audio (con la voz clonada COCOBASILE). Solo si pide explícito que
        # le respondan en texto, lo respetamos.
        import re as _re
        _TEXT_REQUEST_PATTERNS = [
            r"\brespond[eé]me (?:con|en) (?:un )?(?:texto|escrito)\b",
            r"\bcontest[aá]me (?:con|en) (?:un )?(?:texto|escrito)\b",
            r"\b(?:respond[eé]|contest[aá]) (?:con|en) (?:texto|escrito)\b",
            r"\bsin (?:audio|voz)\b", r"\bs[oó]lo texto\b", r"\bsolo texto\b",
            r"\bescrib[ií]me\b",
        ]
        _pidio_texto = any(_re.search(p, (texto or "").lower()) for p in _TEXT_REQUEST_PATTERNS)
        _pidio_audio = not _pidio_texto   # audio in → audio out (default)
        log.info(f"[{chat_id}] pidio_audio={_pidio_audio} (pidio_texto_explicito={_pidio_texto})")

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
                elif nombre_f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
                    await context.bot.send_photo(chat_id=chat_id,
                        photo=io.BytesIO(contenido), caption=caption[:1024])
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
    """Maneja documentos enviados al bot (PDF, TXT, etc.).

    Nuevo flow (multi-tenant):
      1. Extrae texto del documento.
      2. NO ingesta automáticamente — muestra botones preguntando a qué
         tenant/namespace subir (Reamerica / Goodsten / Personal / Analizar
         sin ingestar / Cancelar).
      3. Al click, ingesta al schema correcto via modules.rag_kb.
    """
    import tempfile, os, secrets
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    chat_id = update.effective_chat.id
    doc = update.message.document

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    if doc.mime_type not in ("application/pdf", "text/plain"):
        await update.message.reply_text(f"Por ahora solo proceso PDF y TXT. Recibí: {doc.mime_type}")
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        suffix = ".pdf" if doc.mime_type == "application/pdf" else ".txt"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)
        log.info(f"[{chat_id}] Documento recibido: {doc.file_name} ({doc.file_size} bytes)")

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
                    await update.message.reply_text("Necesito pypdf instalado para leer PDFs.")
                    return
        else:
            with open(tmp_path, "r", errors="replace") as f:
                texto = f.read()

        os.unlink(tmp_path)

        if not texto.strip():
            await update.message.reply_text("No pude extraer texto. ¿Es un PDF escaneado (imagen)?")
            return

        # Guardar en cache de usuario con token corto (callback_data tiene 64b límite)
        token = secrets.token_urlsafe(6)
        caption = update.message.caption or ""
        if not hasattr(context, "user_data") or context.user_data is None:
            context.user_data = {}
        context.user_data.setdefault("pending_ingest", {})[token] = {
            "filename": doc.file_name,
            "text":     texto,
            "caption":  caption,
            "chat_id":  chat_id,
        }

        # Sugerencia de tenant basada en filename/caption
        suggestion = _suggest_tenant_from_doc(doc.file_name, caption)

        def _b(emoji, name, tenant_ns, suggest_slug):
            star = " ⭐" if suggest_slug and tenant_ns.split(":")[0] == suggest_slug else ""
            return InlineKeyboardButton(f"{emoji} {name}{star}", callback_data=f"ing:{token}:{tenant_ns}")

        kb = [
            [_b("🏢", "Reamerica · brand",   "reamerica:brand",   suggestion),
             _b("🍦", "Goodsten · brand",    "goodsten:brand",    suggestion)],
            [_b("🍦", "Goodsten · marketing","goodsten:marketing", suggestion),
             _b("🍦", "Goodsten · producto", "goodsten:producto",  suggestion)],
            [_b("🏢", "Reamerica · reinsurance", "reamerica:reinsurance", suggestion)],
            [_b("🔒", "Personal · general",  "personal:general",   suggestion)],
            [InlineKeyboardButton("📝 Analizar sin ingestar",  callback_data=f"ing:{token}:_analyze_"),
             InlineKeyboardButton("❌ Cancelar",                callback_data=f"ing:{token}:_cancel_")],
        ]

        msg = (
            f"📄 *{doc.file_name}*\n"
            f"_{len(texto):,} caracteres · {doc.file_size/1024:.0f} KB_\n\n"
            "¿A qué tenant/namespace lo ingesto?"
        )
        if suggestion:
            msg += f"\n\n💡 Sugerido: *{suggestion}* (por nombre/caption)"
        msg += "\n_La ⭐ marca el sugerido. Si elegís 'Analizar', te devuelvo un resumen sin guardar en la KB._"

        await update.message.reply_text(msg, parse_mode="Markdown",
                                         reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        log.error(f"Error procesando documento: {e}")
        await update.message.reply_text(f"Error procesando el documento: {e}")


def _suggest_tenant_from_doc(filename: str, caption: str) -> str:
    """Sugiere tenant basado en nombre de archivo y caption. '' si no detecta."""
    blob = f"{filename or ''} {caption or ''}".lower()
    if any(k in blob for k in ["goodsten", "helado", "helados", "sabor", "gelato"]):
        return "goodsten"
    if any(k in blob for k in ["reamerica", "reaseguro", "reinsurance", "broker",
                                "endoso", "ibf", "cedente", "quota share", "treaty"]):
        return "reamerica"
    if any(k in blob for k in ["astrolog", "carta natal", "transito", "retorno solar"]):
        return "personal"
    return ""


async def handle_ingest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback para los botones de ingesta post-documento."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    parts = query.data.split(":", 2)  # "ing:<token>:<tenant_ns>"
    if len(parts) < 3:
        await query.edit_message_text("Callback inválido.")
        return
    token = parts[1]
    dest = parts[2]  # "reamerica:brand" | "_analyze_" | "_cancel_"

    cache = (context.user_data or {}).get("pending_ingest", {})
    data = cache.pop(token, None)
    if not data:
        await query.edit_message_text("⏰ Expiró el pedido (o ya fue procesado).")
        return

    filename = data["filename"]
    texto    = data["text"]
    caption  = data.get("caption", "")

    if dest == "_cancel_":
        await query.edit_message_text(f"❌ Cancelado. `{filename}` no se ingestó.",
                                       parse_mode="Markdown")
        return

    if dest == "_analyze_":
        # Comportamiento anterior: pasar a Claude para análisis
        await query.edit_message_text(f"📝 Analizando `{filename}` con Claude...",
                                       parse_mode="Markdown")
        from core.bot_core import ask_claude, save_message_full, DB_PATH
        import asyncio, queue, threading
        prompt = (f"El usuario envió el documento '{filename}'"
                  + (f" con mensaje: '{caption}'" if caption else "")
                  + f".\n\nContenido ({len(texto)} chars, truncado si >12k):\n\n{texto[:12000]}")
        q = queue.Queue()
        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, prompt, user_name="User")))
            except Exception as e:
                q.put(("err", str(e)))
        t = threading.Thread(target=run_claude, daemon=True)
        t.start()
        elapsed = 0
        while t.is_alive() and elapsed < 120:
            await asyncio.sleep(4)
            elapsed += 4
        t.join(timeout=1)
        try:
            status, payload = q.get(timeout=2)
            if status == "ok":
                reply, _, _ = payload
                save_message_full(chat_id, "user",      prompt[:500], db_path=DB_PATH)
                save_message_full(chat_id, "assistant", reply,         db_path=DB_PATH)
                await context.bot.send_message(chat_id=chat_id, text=reply[:4000])
            else:
                await context.bot.send_message(chat_id=chat_id, text=f"Error analizando: {payload}")
        except Exception:
            pass
        return

    # Ingesta real: dest tiene formato "tenant:namespace"
    try:
        tenant_slug, namespace = dest.split(":", 1)
    except ValueError:
        await query.edit_message_text(f"Destino inválido: {dest}")
        return

    try:
        from modules.rag_kb import ingest as rag_ingest
        await query.edit_message_text(
            f"⏳ Ingestando `{filename}` en *{tenant_slug}* (ns=`{namespace}`)...",
            parse_mode="Markdown"
        )
        metadata = {
            "source_file": filename,
            "uploaded_by_chat_id": chat_id,
            "caption":    caption[:500] if caption else "",
            "ingested_via": "telegram_document_callback",
        }
        # Schema mapping: personal usa schema 'personal' (cross-tenant owner data)
        # los demás van al schema del tenant slug
        schema = "personal" if tenant_slug == "personal" else None
        n_chunks = rag_ingest(
            source=filename,
            text=texto,
            metadata=metadata,
            namespace=namespace,
            tenant=tenant_slug if tenant_slug != "personal" else None,
            schema=schema,
            semantic=True,
        )
        emoji = {"reamerica": "🏢", "goodsten": "🍦", "personal": "🔒"}.get(tenant_slug, "📁")
        await query.edit_message_text(
            f"✅ Ingestado en {emoji} *{tenant_slug}* · `ns={namespace}`\n\n"
            f"📄 `{filename}`\n"
            f"📊 *{n_chunks}* chunks indexados\n"
            f"📏 {len(texto):,} caracteres procesados",
            parse_mode="Markdown"
        )
    except Exception as e:
        log.error(f"[{chat_id}] ingest callback error: {e}")
        await query.edit_message_text(
            f"❌ Error ingestando en {tenant_slug}:\n`{str(e)[:300]}`",
            parse_mode="Markdown"
        )


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


async def cmd_sf(update, context):
    """Salesforce CRM — solo lectura. Owner-only. /sf sin args muestra menú."""
    from core.bot_core import OWNER_CHAT_ID
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("Comando solo para el owner.")
        return
    args = context.args or []
    if not args:
        # Recomendar /rma como hub principal — /sf queda como atajo CRM puro.
        await update.message.reply_text(
            "ℹ️ Para una experiencia completa usá `/rma` (hub Reamerica).\n\n"
            "`/sf` directo:\n"
            "• `/sf <pregunta natural>` — el LLM arma la query\n"
            "• `/sf SELECT ... FROM ...` — SOQL crudo\n"
            "• `/sf describe Account` — campos del sObject\n"
            "• `/sf list` — todos los sObjects",
            parse_mode="Markdown"
        )
        return

    # Modo CLI / natural language
    try:
        from services.salesforce import sf_query, sf_describe, sf_list_objects, is_select_only
        from services.tenants import resolve_tenant
        tslug = resolve_tenant(chat_id) or "reamerica"
        full = " ".join(args).strip()
        first = args[0].lower()

        # Subcomandos exactos
        if first == "describe" and len(args) >= 2:
            obj = args[1]
            d = sf_describe(obj, tenant=tslug, env="uat")
            fields = d.get("fields", [])
            lines = [f"*{obj}* — {len(fields)} campos:"]
            for f in fields[:60]:
                tag = " 🔧" if f.get("custom") else ""
                lines.append(f"`{f['name']}` ({f.get('type')}){tag} — {f.get('label','')[:50]}")
            await update.message.reply_text("\n".join(lines)[:4000], parse_mode="Markdown")
            return
        if first == "list":
            objs = sf_list_objects(tenant=tslug, env="uat")
            lines = [f"*{len(objs)} sObjects queryables:*"]
            for o in objs[:80]:
                tag = " 🔧" if o["custom"] else ""
                lines.append(f"`{o['name']}`{tag} — {o['label']}")
            await update.message.reply_text("\n".join(lines)[:4000], parse_mode="Markdown")
            return

        # SOQL crudo (empieza con SELECT). Hard guard read-only.
        if first == "select":
            if not is_select_only(full):
                await update.message.reply_text(
                    "❌ *Bloqueado.* Salesforce está en modo SOLO LECTURA. "
                    "Solo `SELECT` puro (sin INSERT/UPDATE/DELETE/MERGE/UPSERT, sin `;`).",
                    parse_mode="Markdown"
                )
                return
            rows = sf_query(full, tenant=tslug, env="uat", max_records=50)
            if not rows:
                await update.message.reply_text(f"Sin resultados para:\n`{full}`", parse_mode="Markdown")
                return
            clean = [{k: v for k, v in r.items() if k != "attributes"} for r in rows[:25]]
            import json as _j
            body = _j.dumps(clean, indent=1, ensure_ascii=False, default=str)[:3700]
            await update.message.reply_text(
                f"*{len(rows)} registros* (primeros 25):\n```json\n{body}\n```",
                parse_mode="Markdown"
            )
            return

        # Caso default: pregunta en lenguaje natural sobre Salesforce.
        # Delegar al LLM con prefijo "Salesforce:" para forzar intent=reinsurance,
        # que tiene sf_consultar disponible + el mapa de schema en el system prompt.
        from core.bot_core import ask_claude, save_message_full, DB_PATH, send_long_message
        import asyncio, io, queue, threading
        natural = "Salesforce: " + full
        name = update.effective_user.first_name or "Usuario"
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        q = queue.Queue()
        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, natural, user_name=name, allow_voice=False)))
            except Exception as e:
                q.put(("err", str(e)))
        t = threading.Thread(target=run_claude, daemon=True)
        t.start()

        elapsed = 0
        while t.is_alive() and elapsed < 120:
            await asyncio.sleep(3)
            elapsed += 3
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
        reply, _pdf, _extras = payload
        save_message_full(chat_id, "user",      natural, db_path=DB_PATH)
        save_message_full(chat_id, "assistant", reply,   db_path=DB_PATH)
        await send_long_message(context.bot, chat_id, reply, reply_to=update.message)

    except Exception as e:
        log.error(f"[{chat_id}] /sf error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:300]}")


async def handle_sf_callback(update, context):
    """Callbacks del menú /sf. Cada botón ejecuta una query de solo lectura.
    Cada vista incluye [← Volver al hub] para navegar al menú /rma."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    data = query.data  # ej: "sf:summary", "sf:accounts", "sf:obj:Contratos__c"
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    extra  = parts[2] if len(parts) > 2 else ""

    # Botón estándar de retorno al hub /rma
    _back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("← Volver al hub", callback_data="rma:back")]])

    try:
        from services.salesforce import sf_query, sf_describe, sf_list_objects
        from services.tenants import resolve_tenant
        tslug = resolve_tenant(chat_id) or "reamerica"
        env = "uat"

        if action == "help":
            txt = (
                "*Comandos SF directos:*\n"
                "• `/sf list` — todos los sObjects\n"
                "• `/sf describe Account` — campos\n"
                "• `/sf SELECT Id,Name FROM Account LIMIT 10` — SOQL crudo\n\n"
                "*Conversacional:* tipeá natural, ej.\n"
                "• \"primeros 10 accounts de Argentina\"\n"
                "• \"oportunidades en stage Orden en firme\"\n"
                "• \"contratos creados este mes\"\n\n"
                "_Solo lectura. INSERT/UPDATE/DELETE bloqueados._"
            )
            await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "summary":
            ta = sf_query("SELECT COUNT(Id) c FROM Account")[0]["c"]
            tc = sf_query("SELECT COUNT(Id) c FROM Contact")[0]["c"]
            to = sf_query("SELECT COUNT(Id) c FROM Opportunity")[0]["c"]
            tco = sf_query("SELECT COUNT(Id) c FROM Contratos__c")[0]["c"]
            te = sf_query("SELECT COUNT(Id) c FROM Endosos__c")[0]["c"]
            tt = sf_query("SELECT COUNT(Id) c FROM Tercero__c")[0]["c"]
            txt = (
                "*Resumen Salesforce — Reamerica UAT*\n\n"
                f"👥 Accounts (clientes): *{ta:,}*\n"
                f"📞 Contacts (personas): *{tc:,}*\n"
                f"💼 Opportunities: *{to:,}*\n"
                f"📜 Contratos\\_\\_c: *{tco:,}*\n"
                f"📝 Endosos\\_\\_c: *{te:,}*\n"
                f"👤 Tercero\\_\\_c: *{tt:,}*\n"
            )
            await query.edit_message_text(txt, parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "accounts":
            rows = sf_query(
                "SELECT Name, Industry, Type, BillingCountry FROM Account "
                "ORDER BY LastModifiedDate DESC LIMIT 20"
            )
            lines = [f"*Últimas 20 cuentas modificadas:*"]
            for r in rows:
                nm = (r.get("Name") or "")[:35]
                ind = (r.get("Industry") or "—")[:20]
                tp = (r.get("Type") or "—")[:12]
                pais = (r.get("BillingCountry") or "—")[:12]
                lines.append(f"• *{nm}* — {ind} · {tp} · {pais}")
            await query.edit_message_text("\n".join(lines)[:4000], parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "contacts":
            rows = sf_query(
                "SELECT Name, Title, Email, Account.Name FROM Contact "
                "WHERE Email != null ORDER BY LastModifiedDate DESC LIMIT 15"
            )
            lines = [f"*Últimos 15 contactos con email:*"]
            for r in rows:
                nm = (r.get("Name") or "")[:25]
                title = (r.get("Title") or "—")[:20]
                email = (r.get("Email") or "—")[:30]
                acct = ((r.get("Account") or {}).get("Name") or "—")[:20]
                lines.append(f"• *{nm}* — {title}\n  📧 `{email}`\n  🏢 {acct}")
            await query.edit_message_text("\n".join(lines)[:4000], parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "opps":
            rows = sf_query(
                "SELECT Name, StageName, Amount, CloseDate, Account.Name FROM Opportunity "
                "ORDER BY LastModifiedDate DESC LIMIT 15"
            )
            lines = [f"*Últimas 15 oportunidades modificadas:*"]
            for r in rows:
                nm = (r.get("Name") or "")[:30]
                stage = (r.get("StageName") or "—")[:20]
                amt = r.get("Amount") or 0
                close = r.get("CloseDate") or "—"
                acct = ((r.get("Account") or {}).get("Name") or "—")[:20]
                lines.append(f"• *{nm}*\n  {stage} · ${amt} · cierre {close}\n  🏢 {acct}")
            await query.edit_message_text("\n".join(lines)[:4000], parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "pipeline":
            rows = sf_query(
                "SELECT StageName, COUNT(Id) c, SUM(Amount) amt FROM Opportunity "
                "GROUP BY StageName ORDER BY COUNT(Id) DESC"
            )
            lines = ["*Pipeline por stage:*"]
            tot = 0
            for r in rows:
                stage = r.get("StageName") or "(sin stage)"
                c = r.get("c") or 0
                amt = r.get("amt") or 0
                tot += c
                lines.append(f"• *{stage}* — {c} opps · ${amt:,}")
            lines.append(f"\n*Total:* {tot} opportunities")
            await query.edit_message_text("\n".join(lines)[:4000], parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "industries":
            rows = sf_query(
                "SELECT Industry, COUNT(Id) c FROM Account "
                "GROUP BY Industry ORDER BY COUNT(Id) DESC LIMIT 15"
            )
            lines = ["*Top 15 industrias (Accounts):*"]
            for r in rows:
                ind = (r.get("Industry") or "(sin industria)")[:50]
                c = r.get("c") or 0
                lines.append(f"• {ind} — *{c}*")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "countries":
            rows = sf_query(
                "SELECT BillingCountry, COUNT(Id) c FROM Account "
                "WHERE BillingCountry != null "
                "GROUP BY BillingCountry ORDER BY COUNT(Id) DESC LIMIT 20"
            )
            lines = ["*Top 20 países (Accounts):*"]
            for r in rows:
                pais = (r.get("BillingCountry") or "?")[:30]
                c = r.get("c") or 0
                lines.append(f"• {pais} — *{c}*")
            if len(lines) == 1:
                lines.append("_(no hay BillingCountry cargado en UAT)_")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "list":
            objs = sf_list_objects(tenant=tslug, env=env)
            customs = [o for o in objs if o["custom"]]
            stds = [o for o in objs if not o["custom"]]
            lines = [
                f"*sObjects en SF UAT*",
                f"\n*Custom ({len(customs)}):*"
            ]
            for o in customs[:40]:
                lines.append(f"`{o['name']}` — {o['label']}")
            lines.append(f"\n*Standard ({len(stds)} totales, top 30):*")
            for o in stds[:30]:
                lines.append(f"`{o['name']}` — {o['label']}")
            await query.edit_message_text("\n".join(lines)[:4000], parse_mode="Markdown", reply_markup=_back_kb)
            return

        if action == "obj" and extra:
            d = sf_describe(extra, tenant=tslug, env=env)
            fields = d.get("fields", [])
            cnt_q = sf_query(f"SELECT COUNT(Id) c FROM {extra}")
            total = cnt_q[0]["c"] if cnt_q else 0
            lines = [
                f"*{extra}*",
                f"_{d.get('label','')} · {total:,} registros_\n",
                f"*Campos ({len(fields)}, primeros 40):*"
            ]
            for f in fields[:40]:
                tag = " 🔧" if f.get("custom") else ""
                lines.append(f"`{f['name']}` ({f.get('type')}){tag} — {f.get('label','')[:40]}")
            await query.edit_message_text("\n".join(lines)[:4000], parse_mode="Markdown", reply_markup=_back_kb)
            return

        await query.edit_message_text(f"Acción `{action}` no reconocida.", parse_mode="Markdown", reply_markup=_back_kb)
    except Exception as e:
        log.error(f"[{chat_id}] sf callback error: {e}")
        try:
            await query.edit_message_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass


async def cmd_rma(update, context):
    """Hub principal de Reamerica Risk Advisors. Owner-only."""
    from core.bot_core import OWNER_CHAT_ID
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("Comando solo para el owner.")
        return
    kb = [
        [InlineKeyboardButton("📊 CRM — Resumen general", callback_data="rma:summary")],
        [InlineKeyboardButton("👥 Cuentas",        callback_data="sf:accounts"),
         InlineKeyboardButton("📞 Contactos",      callback_data="sf:contacts")],
        [InlineKeyboardButton("💼 Oportunidades",  callback_data="sf:opps"),
         InlineKeyboardButton("📈 Pipeline",       callback_data="sf:pipeline")],
        [InlineKeyboardButton("📜 Contratos",      callback_data="sf:obj:Contratos__c"),
         InlineKeyboardButton("📝 Endosos",        callback_data="sf:obj:Endosos__c")],
        [InlineKeyboardButton("🌎 Por industria",  callback_data="sf:industries"),
         InlineKeyboardButton("🌍 Por país",        callback_data="sf:countries")],
        [InlineKeyboardButton("👤 Brokers / Performance", callback_data="rma:brokers")],
        [InlineKeyboardButton("📂 Listar todos los objetos", callback_data="sf:list")],
        [InlineKeyboardButton("⚡ Comandos directos", callback_data="rma:tools")],
        [InlineKeyboardButton("❓ Ayuda",          callback_data="rma:help")],
    ]
    await update.message.reply_text(
        "🏢 *REAMERICA RISK ADVISORS*\n"
        "_Hub principal — elegí qué querés ver._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def handle_rma_callback(update, context):
    """Callbacks del menú /rma. Maneja navegación + brokers list + ayuda."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    parts = query.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    extra  = parts[2] if len(parts) > 2 else ""

    try:
        from services.salesforce import sf_query
        from services.tenants import resolve_tenant
        tslug = resolve_tenant(chat_id) or "reamerica"

        if action == "back":
            # Volver al menu principal
            kb = [
                [InlineKeyboardButton("📊 CRM — Resumen general", callback_data="rma:summary")],
                [InlineKeyboardButton("👥 Cuentas",        callback_data="sf:accounts"),
                 InlineKeyboardButton("📞 Contactos",      callback_data="sf:contacts")],
                [InlineKeyboardButton("💼 Oportunidades",  callback_data="sf:opps"),
                 InlineKeyboardButton("📈 Pipeline",       callback_data="sf:pipeline")],
                [InlineKeyboardButton("📜 Contratos",      callback_data="sf:obj:Contratos__c"),
                 InlineKeyboardButton("📝 Endosos",        callback_data="sf:obj:Endosos__c")],
                [InlineKeyboardButton("🌎 Por industria",  callback_data="sf:industries"),
                 InlineKeyboardButton("🌍 Por país",        callback_data="sf:countries")],
                [InlineKeyboardButton("👤 Brokers / Performance", callback_data="rma:brokers")],
                [InlineKeyboardButton("📂 Listar todos los objetos", callback_data="sf:list")],
                [InlineKeyboardButton("⚡ Comandos directos", callback_data="rma:tools")],
                [InlineKeyboardButton("❓ Ayuda",          callback_data="rma:help")],
            ]
            await query.edit_message_text(
                "🏢 *REAMERICA RISK ADVISORS*\n_Hub principal — elegí qué querés ver._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        if action == "summary":
            ta = sf_query("SELECT COUNT(Id) c FROM Account")[0]["c"]
            tc = sf_query("SELECT COUNT(Id) c FROM Contact")[0]["c"]
            to = sf_query("SELECT COUNT(Id) c FROM Opportunity")[0]["c"]
            tco = sf_query("SELECT COUNT(Id) c FROM Contratos__c")[0]["c"]
            te = sf_query("SELECT COUNT(Id) c FROM Endosos__c")[0]["c"]
            ti = sf_query("SELECT COUNT(Id) c FROM IBF__c")[0]["c"]
            tot = sf_query("SELECT SUM(Prima_periodo_100__c) p FROM IBF__c")[0]["p"] or 0
            txt = (
                "📊 *CRM Reamerica — Resumen general*\n\n"
                f"👥 Cuentas: *{ta:,}*  ·  📞 Contactos: *{tc:,}*\n"
                f"💼 Opps: *{to:,}*  ·  📜 Contratos: *{tco:,}*  ·  📝 Endosos: *{te:,}*\n"
                f"📋 IBFs: *{ti:,}*\n\n"
                f"💰 Prima 100% acumulada (todos los IBFs): *${tot:,.0f}*"
            )
            kb = [[InlineKeyboardButton("← Volver al hub", callback_data="rma:back")]]
            await query.edit_message_text(txt, parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(kb))
            return

        if action == "brokers":
            # Top brokers por # opportunities (Owner)
            rows = sf_query(
                "SELECT Owner.Name name, OwnerId oid, COUNT(Id) c "
                "FROM Opportunity WHERE OwnerId != null "
                "GROUP BY Owner.Name, OwnerId ORDER BY COUNT(Id) DESC LIMIT 12"
            )
            kb = []
            for r in rows:
                nm = r.get("name") or "?"
                oid = r.get("oid") or ""
                c = r.get("c") or 0
                # callback_data tiene 64 chars max — uso solo el OwnerId (15 chars)
                kb.append([InlineKeyboardButton(f"{nm} ({c} opps)", callback_data=f"rma:broker:{oid}")])
            kb.append([InlineKeyboardButton("← Volver al hub", callback_data="rma:back")])
            await query.edit_message_text(
                "👥 *Brokers — Top 12 por volumen de opps*\n_Tocá uno para ver su dashboard._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

        if action == "broker" and extra:
            # Dashboard de un broker — invocar sf_broker_perf y devolver
            from services.sf_broker_perf import resolve_broker, compute, format_dashboard
            await query.answer("Calculando...", show_alert=False)
            b = resolve_broker(extra)
            if not b:
                await query.edit_message_text(f"No encontré broker para Id `{extra}`",
                                              parse_mode="Markdown")
                return
            metrics = compute(b["Id"])
            text = format_dashboard(b, metrics)
            kb = [
                [InlineKeyboardButton("📅 Solo 2025", callback_data=f"rma:brokerY:{extra}:2025"),
                 InlineKeyboardButton("📅 Solo 2024", callback_data=f"rma:brokerY:{extra}:2024")],
                [InlineKeyboardButton("← Volver a brokers", callback_data="rma:brokers"),
                 InlineKeyboardButton("🏠 Hub", callback_data="rma:back")],
            ]
            await query.edit_message_text(text[:3900], parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(kb))
            return

        if action == "brokerY" and ":" in extra:
            from services.sf_broker_perf import resolve_broker, compute, format_dashboard
            uid, yr = extra.split(":", 1)
            await query.answer("Calculando...", show_alert=False)
            b = resolve_broker(uid)
            if not b:
                await query.edit_message_text("Broker no encontrado.")
                return
            metrics = compute(b["Id"], year=int(yr))
            text = format_dashboard(b, metrics)
            kb = [
                [InlineKeyboardButton("📅 Histórico", callback_data=f"rma:broker:{uid}")],
                [InlineKeyboardButton("← Brokers", callback_data="rma:brokers"),
                 InlineKeyboardButton("🏠 Hub", callback_data="rma:back")],
            ]
            await query.edit_message_text(text[:3900], parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(kb))
            return

        if action == "tools":
            txt = (
                "⚡ *Comandos directos Reamerica*\n\n"
                "*CRM:*\n"
                "• `/sf list` — todos los sObjects\n"
                "• `/sf describe Account` — campos del sObject\n"
                "• `/sf SELECT Id,Name FROM Account LIMIT 10` — SOQL crudo\n"
                "• `/sf <pregunta natural>` — el LLM arma la query\n\n"
                "*Brokers:*\n"
                "• `/broker Ignacio Romanelli` — dashboard histórico\n"
                "• `/broker Tomas Barrabino 2025` — solo 2025\n\n"
                "*Conversacional:* hablale natural y el bot rutea solo.\n"
                "_Solo lectura. INSERT/UPDATE/DELETE bloqueados._"
            )
            kb = [[InlineKeyboardButton("← Volver al hub", callback_data="rma:back")]]
            await query.edit_message_text(txt, parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(kb))
            return

        if action == "help":
            txt = (
                "❓ *Ayuda — Reamerica*\n\n"
                "Este es el hub para todo lo relacionado a Reamerica Risk Advisors:\n"
                "• Datos del CRM Salesforce (cuentas, contactos, oportunidades)\n"
                "• Performance de brokers (dashboards individuales)\n"
                "• Reportes (próximamente: Quickbooks, dashboards consolidados)\n\n"
                "*Cómo navegar:*\n"
                "1. Tocá un botón para ir a esa sección\n"
                "2. Cada sección tiene un botón ← para volver\n"
                "3. Para queries específicas, usá `/rma:tools` o tipeá natural\n\n"
                "*Restricción:* Salesforce solo lectura. Para escribir, usá la UI de SF y "
                "después podés pedirme que lea los cambios.\n\n"
                "Ambiente actual: *UAT* (sandbox de pruebas). Cuando pasen a PROD, "
                "se cambia automáticamente."
            )
            kb = [[InlineKeyboardButton("← Volver al hub", callback_data="rma:back")]]
            await query.edit_message_text(txt, parse_mode="Markdown",
                                          reply_markup=InlineKeyboardMarkup(kb))
            return

        await query.edit_message_text(f"Acción `rma:{action}` no reconocida.",
                                      parse_mode="Markdown")
    except Exception as e:
        log.error(f"[{chat_id}] rma callback error: {e}")
        try:
            await query.edit_message_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass


async def cmd_broker(update, context):
    """Dashboard de performance de un broker. Owner-only.
    /broker <nombre|email|userId> [year]
    Ej: /broker Ignacio Romanelli
        /broker Ignacio Romanelli 2025
        /broker ignacio.romanelli@reamerica
    """
    from core.bot_core import OWNER_CHAT_ID
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("Comando solo para el owner.")
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "*Uso:* `/broker <nombre|email|userId> [year]`\n\n"
            "Ejemplos:\n"
            "• `/broker Ignacio Romanelli` — histórico\n"
            "• `/broker Ignacio Romanelli 2025` — solo 2025\n"
            "• `/broker tomas.barrabino` — busca por email\n",
            parse_mode="Markdown"
        )
        return
    # Parse: si último arg es un año (4 dígitos 2020-2099), separar
    year = None
    if args[-1].isdigit() and len(args[-1]) == 4 and 2020 <= int(args[-1]) <= 2099:
        year = int(args[-1])
        needle = " ".join(args[:-1])
    else:
        needle = " ".join(args)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        from services.sf_broker_perf import resolve_broker, compute, format_dashboard
        broker = resolve_broker(needle)
        if not broker:
            await update.message.reply_text(
                f"No encontré ningún User para `{needle}`.\n"
                f"Probá con email completo o User Id (empieza con `005`).",
                parse_mode="Markdown"
            )
            return
        log.info(f"[{chat_id}] /broker {broker.get('Name')} year={year}")
        metrics = compute(broker["Id"], year=year)
        text = format_dashboard(broker, metrics)
        # Si es muy largo, partir en 2
        if len(text) > 4000:
            chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
            for ch in chunks:
                await update.message.reply_text(ch, parse_mode="Markdown")
        else:
            await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"[{chat_id}] /broker error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:300]}")


async def cmd_top(update, context):
    """Top 5 usuarios con más mensajes este mes. Owner-only.

    Uso:
      /top           -> top 5 este mes (default)
      /top 10        -> top 10 este mes
      /top all       -> top 5 histórico
      /top 10 all    -> top 10 histórico
    """
    from core.bot_core import OWNER_CHAT_ID, DB_PATH
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("Solo owner.")
        return

    args = context.args or []
    limit = 5
    all_time = False
    for a in args:
        if a.lower() == "all":
            all_time = True
        elif a.isdigit() and 1 <= int(a) <= 50:
            limit = int(a)

    import sqlite3
    period = "histórico" if all_time else "este mes"
    where = "role = 'user'" if all_time else "role = 'user' AND timestamp >= date('now', 'start of month')"

    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(f"""
            SELECT chat_id, COUNT(*) as c, MAX(timestamp) as last_ts
            FROM messages
            WHERE {where}
            GROUP BY chat_id
            ORDER BY c DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        # Total msgs para % share
        cur.execute(f"SELECT COUNT(*) FROM messages WHERE {where}")
        total = cur.fetchone()[0] or 1
        con.close()
    except Exception as e:
        log.error(f"[{chat_id}] /top error: {e}")
        await update.message.reply_text(f"Error: {str(e)[:200]}")
        return

    if not rows:
        await update.message.reply_text(f"Sin mensajes ({period}).")
        return

    # Resolver nombres desde person_memory o shared.tenants si hay
    def _name_for(cid):
        try:
            con2 = sqlite3.connect(DB_PATH)
            r = con2.execute("SELECT name FROM person_memory WHERE chat_id=? LIMIT 1", (cid,)).fetchone()
            con2.close()
            if r:
                return r[0]
        except Exception:
            pass
        return None

    lines = [f"*Top {len(rows)} usuarios — {period}*", f"_Total mensajes user: {total:,}_\n"]
    for i, (cid, c, last_ts) in enumerate(rows, 1):
        pct = (c / total) * 100
        name = _name_for(cid)
        who = f"*{name}*" if name else f"`{cid}`"
        last = (last_ts or "")[:16]
        lines.append(f"{i}. {who} — *{c:,}* msgs ({pct:.1f}%) · último: {last}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_version(update, context):
    """Versión del código corriendo en el bot.
    Muestra `git describe --always` (tag/sha), rama actual, y último commit.

    Uso: /version
    """
    import subprocess
    import os
    chat_id = update.effective_chat.id

    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _git(args, default="—"):
        try:
            r = subprocess.run(["git", "-C", repo_dir] + args,
                               capture_output=True, text=True, timeout=5)
            if r.returncode != 0:
                return default
            return (r.stdout or "").strip() or default
        except FileNotFoundError:
            return "(git no instalado)"
        except subprocess.TimeoutExpired:
            return "(timeout)"
        except Exception:
            return default

    describe = _git(["describe", "--always", "--dirty", "--tags"])
    branch   = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    last_log = _git(["log", "-1", "--format=%h %s (%ar)"])
    is_repo  = _git(["rev-parse", "--is-inside-work-tree"], default="false")

    if is_repo != "true":
        await update.message.reply_text(
            "⚠️ No es un repo git (o git no disponible).",
            parse_mode="Markdown"
        )
        return

    msg = (
        "🏷 *Versión del código*\n\n"
        f"• Describe: `{describe}`\n"
        f"• Rama: `{branch}`\n"
        f"• Último commit: `{last_log}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_stats(update, context):
    """Stats del service cukinator.service (o el que se pase como arg).
    Devuelve PID, memoria (MB), CPU acumulado (s), uptime y estado. Owner-only.

    Uso:
      /stats                     → cukinator.service
      /stats cukinator-mcp       → otro service user-level
      /stats all                 → resumen de los 5 services del bot
    """
    from core.bot_core import OWNER_CHAT_ID
    chat_id = update.effective_chat.id
    if chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("Solo owner.")
        return

    args = context.args or []
    if args and args[0].lower() == "all":
        services = [
            "cukinator.service",
            "cukinator-worker.service",
            "cukinator-mcp.service",
            "cukinator-designer.service",
            "cukinator-remote.service",
        ]
    else:
        svc = args[0] if args else "cukinator.service"
        if not svc.endswith(".service"):
            svc += ".service"
        services = [svc]

    lines = []
    for s in services:
        lines.append(_format_service_stats(s))
    await update.message.reply_text("\n\n".join(lines)[:4000], parse_mode="Markdown")


def _format_service_stats(service: str) -> str:
    """Ejecuta systemctl --user show y formatea como markdown Telegram.
    Safe: captura errores, usa timeout corto."""
    import subprocess
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", service,
             "--property=MainPID,MemoryCurrent,CPUUsageNSec,"
             "ActiveEnterTimestamp,ActiveState,SubState,LoadState",
             "--no-pager"],
            capture_output=True, text=True, timeout=6,
        )
    except FileNotFoundError:
        return f"❓ *{service}*: systemctl no disponible (¿Windows?)"
    except subprocess.TimeoutExpired:
        return f"⏰ *{service}*: timeout consultando systemd"
    except Exception as e:
        return f"❌ *{service}*: {str(e)[:80]}"

    if result.returncode != 0:
        return f"❌ *{service}*: {(result.stderr or '')[:120]}"

    data = {}
    for line in (result.stdout or "").strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v

    if data.get("LoadState") == "not-found":
        return f"❓ *{service}*: service no existe"

    pid         = data.get("MainPID", "0")
    mem_bytes   = int(data.get("MemoryCurrent", "0") or 0)
    cpu_ns      = int(data.get("CPUUsageNSec", "0") or 0)
    active_ts   = data.get("ActiveEnterTimestamp", "")
    active      = data.get("ActiveState", "?")
    sub         = data.get("SubState", "?")

    mem_mb  = mem_bytes / (1024 * 1024) if mem_bytes else 0
    cpu_sec = cpu_ns / 1e9 if cpu_ns else 0
    uptime  = _parse_systemd_uptime(active_ts)
    icon    = {"active": "✅", "failed": "❌", "inactive": "⏸", "activating": "🔄"}.get(active, "❓")

    return (
        f"{icon} *{service}* — `{active}/{sub}`\n"
        f"🆔 PID `{pid}` · 💾 {mem_mb:,.1f} MB · ⏲️ CPU {cpu_sec:,.1f}s · ⏰ up {uptime}"
    )


def _parse_systemd_uptime(active_ts: str) -> str:
    """Parsea 'Sun 2026-04-20 14:15:00 UTC' y devuelve '3h 12m' o similar."""
    if not active_ts or active_ts in ("0", "n/a"):
        return "—"
    try:
        from datetime import datetime, timezone
        parts = active_ts.strip().split()
        if len(parts) < 3:
            return "—"
        dt_str = f"{parts[1]} {parts[2]}"
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        total_s = int((datetime.now(timezone.utc) - dt).total_seconds())
        if total_s < 0:
            return "—"
        days,  rem  = divmod(total_s, 86400)
        hours, rem  = divmod(rem, 3600)
        mins,  secs = divmod(rem, 60)
        if days:
            return f"{days}d {hours}h {mins}m"
        if hours:
            return f"{hours}h {mins}m"
        if mins:
            return f"{mins}m {secs}s"
        return f"{secs}s"
    except Exception:
        return "—"


async def cmd_qr(update, context):
    """Genera QR code del texto/URL pasado. /qr <texto o URL>
    Ejemplos:
      /qr https://reamerica.com.ar
      /qr WIFI:T:WPA;S:MiRed;P:secret;;
      /qr Hola, este es un QR
    """
    import io
    chat_id = update.effective_chat.id
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: `/qr <texto o URL>`\n"
            "Ejemplos:\n"
            "• `/qr https://reamerica.com.ar`\n"
            "• `/qr WIFI:T:WPA;S:MiRed;P:secret;;`",
            parse_mode="Markdown"
        )
        return
    data = " ".join(args)[:2000]
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
        qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M,
                           box_size=10, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        cap = data if len(data) <= 100 else data[:97] + "..."
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        await context.bot.send_photo(chat_id=chat_id, photo=buf, caption=cap)
        log.info(f"[{chat_id}] QR generado ({len(data)} chars)")
    except Exception as e:
        log.error(f"[{chat_id}] /qr error: {e}")
        await update.message.reply_text(f"Error generando QR: {e}")
