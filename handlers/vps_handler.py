"""
Handler de Telegram para comandos VPS.
"""

from telegram import Update
from telegram.ext import ContextTypes
import sys
sys.path.append('..')
from modules.ssh_vps import handle_vps_command


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /vps <comando>"""
    
    # Solo owner puede usar esto
    OWNER_ID = 102871160  # Cuki
    
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Este comando es solo para el owner.")
        return
    
    # Obtener argumentos
    args = " ".join(context.args) if context.args else ""
    
    # Mostrar "escribiendo..."
    await update.message.chat.send_action("typing")
    
    # Ejecutar
    result = await handle_vps_command(args)
    
    # Enviar resultado
    await update.message.reply_text(
        result,
        parse_mode="Markdown"
    )
