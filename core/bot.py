"""
Cukinator Bot — Entry Point
La lógica de negocio vive en bot_core.py
Los handlers de Telegram están en handlers/
"""
import os
import sys

# Cargar vault antes de cualquier import que use env vars
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from services.vault import load_all_to_env, init
    init()
    load_all_to_env()
except Exception as e:
    print(f"Vault warning: {e} — usando env vars directas")
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

# Importar handlers
from handlers.message_handler  import handle_message, handle_voice, handle_document, handle_photo
from handlers.callback_handler import handle_callback, cmd_menu, cmd_biblioteca
from handlers.gmail_handler    import gmail_command
from handlers.calendar_handler import calendar_command
from handlers.astro_handler    import astro_command, cartas_command
from handlers.vps_handler      import vps_command

# Importar comandos del core
from core.bot_core import (
    cmd_start, cmd_reset, cmd_voz, cmd_testvoice, cmd_cartas,
    handle_voz_callback, handle_menu_callback, handle_biblioteca_callback,
    handle_confirm_callback,
    init_db, TELEGRAM_TOKEN, log
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

if __name__ == "__main__":
    if os.environ.get("DISABLE_BOT", "").lower() in ("true", "1"):
        import http.server, threading
        port = int(os.environ.get("PORT", "8080"))
        def _health(req): req.send_response(200); req.end_headers(); req.wfile.write(b'{"status":"standby"}')
        httpd = http.server.HTTPServer(("", port), type("H", (http.server.BaseHTTPRequestHandler,), {"do_GET": _health, "log_message": lambda *a: None}))
        log.info(f"DISABLE_BOT=true — Railway standby en :{port}")
        httpd.serve_forever()
    init_db()
    log.info("🤖 CukinatorBot iniciando (arquitectura modular)...")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Comandos
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("reset",     cmd_reset))
    app.add_handler(CommandHandler("cartas",    cmd_cartas))
    app.add_handler(CommandHandler("testvoice", cmd_testvoice))
    app.add_handler(CommandHandler("voz",       cmd_voz))
    app.add_handler(CommandHandler("menu",      cmd_menu))
    app.add_handler(CommandHandler("biblioteca",cmd_biblioteca))
    app.add_handler(CommandHandler("gmail",     gmail_command))
    app.add_handler(CommandHandler("calendar",  calendar_command))
    app.add_handler(CommandHandler("astro",     astro_command))
    app.add_handler(CommandHandler("vps",       vps_command))

    # Callbacks inline
    app.add_handler(CallbackQueryHandler(handle_confirm_callback,      pattern="^confirm:"))
    app.add_handler(CallbackQueryHandler(handle_biblioteca_callback,   pattern="^lib:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback,         pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(handle_voz_callback,          pattern="^voz:"))
    app.add_handler(CallbackQueryHandler(handle_callback,              pattern="^astro:"))

    # Mensajes
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("✅ Bot en línea.")
    app.run_polling(drop_pending_updates=True)
