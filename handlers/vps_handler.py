"""handlers/vps_handler.py — VPS stub"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
log = logging.getLogger(__name__)

async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Funcion VPS en desarrollo.")
