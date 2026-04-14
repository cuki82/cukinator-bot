"""handlers/gmail_handler.py"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
log = logging.getLogger(__name__)

async def gmail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Decime qué necesitás de Gmail: ver inbox, buscar mails, enviar, etc.")
