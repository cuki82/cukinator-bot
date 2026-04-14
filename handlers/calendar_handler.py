"""handlers/calendar_handler.py"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
log = logging.getLogger(__name__)

async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Decime qué necesitás del calendario: ver eventos, crear, etc.")
