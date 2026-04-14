"""
Cukinator Bot — Entry Point
La lógica de negocio vive en bot_core.py
Los handlers de Telegram están en handlers/
"""
import os
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

# Importar handlers
from handlers.message_handler  import handle_message, handle_voice
from handlers.callback_handler import handle_callback, cmd_menu, cmd_biblioteca
from handlers.gmail_handler    import gmail_command
from handlers.calendar_handler import calendar_command
from handlers.astro_handler    import astro_command, cartas_command
from handlers.vps_handler      import vps_command

# Importar comandos del core
from bot_core import (
    cmd_start, cmd_reset, cmd_voz, cmd_testvoice, cmd_cartas,
    handle_voz_callback, handle_menu_callback, handle_biblioteca_callback,
    init_db, TELEGRAM_TOKEN, log
)

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

if __name__ == "__main__":
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
    app.add_handler(CallbackQueryHandler(handle_biblioteca_callback, pattern="^lib:"))
    app.add_handler(CallbackQueryHandler(handle_menu_callback,       pattern="^menu:"))
    app.add_handler(CallbackQueryHandler(handle_voz_callback,        pattern="^voz:"))
    app.add_handler(CallbackQueryHandler(handle_callback,            pattern="^astro:"))

    # Mensajes
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("✅ Bot en línea.")
    app.run_polling(drop_pending_updates=True)
