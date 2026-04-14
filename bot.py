"""
Cukinator Bot - Main Entry Point
"""
import os
import logging
import asyncio
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Importar handlers
from handlers.message_handler import handle_message, handle_voice
from handlers.callback_handler import handle_callback
from handlers.gmail_handler import gmail_command
from handlers.calendar_handler import calendar_command
from handlers.astro_handler import astro_command, cartas_command
from handlers.vps_handler import vps_command

# Configuración de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Reducir logs de httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /start"""
    await update.message.reply_text(
        "Qué hacés. Soy Cukinator. Preguntame lo que quieras."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /help"""
    help_text = """
*Comandos disponibles:*

/start - Iniciar el bot
/help - Ver esta ayuda
/gmail - Gestionar emails
/calendar - Ver calendario
/astro - Calcular carta natal
/cartas - Ver cartas guardadas
/vps - Comandos del servidor VPS

También podés hablarme directamente o mandarme audios.
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def set_bot_commands(application: Application):
    """Configura los comandos del menú del bot"""
    commands = [
        BotCommand("start", "Iniciar el bot"),
        BotCommand("help", "Ver ayuda"),
        BotCommand("gmail", "Gestionar emails"),
        BotCommand("calendar", "Ver calendario"),
        BotCommand("astro", "Calcular carta natal"),
        BotCommand("cartas", "Ver cartas guardadas"),
        BotCommand("vps", "Comandos del servidor VPS"),
    ]
    await application.bot.set_my_commands(commands)

async def post_init(application: Application):
    """Se ejecuta después de inicializar el bot"""
    await set_bot_commands(application)
    logger.info("Bot commands configured")

def main():
    """Función principal"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN no configurado")
    
    # Crear aplicación
    application = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )
    
    # Agregar handlers de comandos
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("gmail", gmail_command))
    application.add_handler(CommandHandler("calendar", calendar_command))
    application.add_handler(CommandHandler("astro", astro_command))
    application.add_handler(CommandHandler("cartas", cartas_command))
    application.add_handler(CommandHandler("vps", vps_command))
    
    # Handler de callbacks (botones inline)
    application.add_handler(CallbackQueryHandler(handle_callback))
    
    # Handler de voz
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    
    # Handler de mensajes (debe ir último)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Iniciar bot
    logger.info("Starting Cukinator bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
