"""
Handler de comandos VPS para Telegram
Usa ssh_executor con Paramiko
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from modules.ssh_executor import execute_ssh_command, get_vps_status

logger = logging.getLogger(__name__)

# User IDs autorizados para comandos VPS
AUTHORIZED_USERS = [8626420783]  # Cuki


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler para /vps [comando]
    Si no hay comando, muestra status del VPS
    """
    user_id = update.effective_user.id
    
    if user_id not in AUTHORIZED_USERS:
        await update.message.reply_text("No tenés permiso para ejecutar comandos en el VPS.")
        return
    
    # Obtener comando (todo lo que viene después de /vps)
    if context.args:
        command = " ".join(context.args)
    else:
        # Sin argumentos = mostrar status
        msg = await update.message.reply_text("Conectando al VPS...")
        result = get_vps_status()
        
        if result["success"]:
            await msg.edit_text(f"```\n{result['output']}\n```", parse_mode="Markdown")
        else:
            await msg.edit_text(f"Error conectando al VPS:\n{result['error']}")
        return
    
    # Ejecutar comando
    msg = await update.message.reply_text(f"Ejecutando: `{command[:50]}...`", parse_mode="Markdown")
    
    result = execute_ssh_command(command)
    
    if result["success"]:
        output = result["output"] or "(sin output)"
        # Truncar si es muy largo
        if len(output) > 4000:
            output = output[:4000] + "\n...(truncado)"
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")
    else:
        error_msg = result["error"] or f"Código de salida: {result['exit_code']}"
        output = result["output"]
        response = f"❌ Error (código {result['exit_code']}):\n{error_msg}"
        if output:
            response += f"\n\nOutput:\n```\n{output[:2000]}\n```"
        await msg.edit_text(response, parse_mode="Markdown")


async def vps_exec(command: str) -> str:
    """
    Función helper para ejecutar comandos VPS desde otros módulos
    Returns: string con output o error
    """
    result = execute_ssh_command(command)
    if result["success"]:
        return result["output"]
    else:
        return f"Error: {result['error']}"
