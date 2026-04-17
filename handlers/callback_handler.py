"""handlers/callback_handler.py — delega al core monolítico"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
log = logging.getLogger(__name__)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.bot_core import handle_callback as _hc
    await _hc(update, context)

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.bot_core import cmd_menu as _cm
    await _cm(update, context)

async def cmd_biblioteca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from core.bot_core import cmd_biblioteca as _cb
    await _cb(update, context)
