"""
VPS Handler - Comandos SSH al VPS de Hostinger
"""
import os
from telegram import Update
from telegram.ext import ContextTypes

# Unificado con OWNER_CHAT_ID de bot_core.py
OWNER_ID = 8626420783

async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /vps <comando>"""
    
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("No tenés permiso para usar este comando.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Uso: /vps <comando>\n"
            "Ejemplo: /vps docker ps\n"
            "Ejemplo: /vps systemctl status ollama"
        )
        return
    
    comando = " ".join(context.args)
    
    # Importar el executor
    try:
        from modules.ssh_executor import execute_ssh_command
    except ImportError:
        await update.message.reply_text("Error: módulo SSH no disponible")
        return
    
    await update.message.reply_text(f"Ejecutando: `{comando}`...", parse_mode="Markdown")
    
    try:
        result = await execute_ssh_command(comando)
        
        if len(result) > 4000:
            # Truncar si es muy largo
            result = result[:4000] + "\n... (truncado)"
        
        await update.message.reply_text(f"```\n{result}\n```", parse_mode="Markdown")
        
    except Exception as e:
        await update.message.reply_text(f"Error ejecutando comando: {str(e)}")
