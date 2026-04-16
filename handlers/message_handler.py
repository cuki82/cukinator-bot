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
from bot_core import (
    ask_claude, save_message_full, send_long_message,
    texto_a_voz, es_respuesta_larga, DB_PATH, OWNER_CHAT_ID
)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import asyncio, io, threading, queue
    chat_id  = update.effective_chat.id
    user_msg = update.message.text
    name     = update.effective_user.first_name or "Usuario"

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
        _bot_dir = os.path.dirname(os.path.abspath(__file__ + "/.."))
        _transcribe_script = os.path.join(_bot_dir, "transcribe.py")

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

        q = queue.Queue()

        def run_claude():
            try:
                q.put(("ok", ask_claude(chat_id, texto, user_name=name, allow_voice=True)))
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
        if not tiene_voz and reply and not es_respuesta_larga(reply):
            ogg_path = texto_a_voz(reply)
            if ogg_path:
                with open(ogg_path, "rb") as f:
                    extra_files.append(("respuesta.ogg", f.read(), "voice"))
                os.unlink(ogg_path)
                tiene_voz = True

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
        from bot_core import ask_claude, save_message_full, send_long_message, DB_PATH

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
        from bot_core import claude, get_system_prompt, save_message_full, send_long_message, DB_PATH

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
