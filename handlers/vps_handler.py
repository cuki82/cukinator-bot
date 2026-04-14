"""
Handler de Telegram para comandos VPS
Usa el módulo ssh_executor con Paramiko
"""
import os
from telegram import Update
from telegram.ext import ContextTypes

# Importar el ejecutor SSH con Paramiko
from modules.ssh_executor import run_ssh_command

# Solo el owner puede ejecutar comandos
OWNER_ID = int(os.getenv("OWNER_TELEGRAM_ID", "8626420783"))

async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /vps <comando> - Ejecuta comando en el VPS via SSH
    Solo disponible para el owner
    """
    user_id = update.effective_user.id
    
    if user_id != OWNER_ID:
        await update.message.reply_text("🚫 No tenés permiso para ejecutar comandos en el VPS.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "Uso: `/vps <comando>`\n"
            "Ejemplo: `/vps uptime`",
            parse_mode="Markdown"
        )
        return
    
    comando = " ".join(context.args)
    
    # Comandos peligrosos bloqueados
    blocked = ["rm -rf /", "mkfs", "dd if=", "> /dev/sd", ":(){ :|:& };:"]
    if any(b in comando for b in blocked):
        await update.message.reply_text("🚫 Comando bloqueado por seguridad.")
        return
    
    await update.message.reply_text(f"⏳ Ejecutando: `{comando}`...", parse_mode="Markdown")
    
    try:
        resultado = run_ssh_command(comando)
        
        # Truncar si es muy largo
        if len(resultado) > 4000:
            resultado = resultado[:4000] + "\n... (truncado)"
        
        await update.message.reply_text(
            f"```\n{resultado}\n```",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
