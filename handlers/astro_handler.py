"""handlers/astro_handler.py — delega al core monolítico"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
log = logging.getLogger(__name__)

async def astro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Para calcular una carta natal decime fecha (DD/MM/AAAA), hora (HH:MM) y lugar.")

async def cartas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Importar del core en runtime para evitar circular imports
    from bot_core import menu_lista_cartas
    await menu_lista_cartas(update, context, update.effective_chat.id)
