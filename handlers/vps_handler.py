"""
Handler para comandos VPS via SSH
"""
import os
import logging
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Intentar importar el módulo SSH
try:
    from modules.ssh import SSHManager
    SSH_AVAILABLE = True
    logger.info("✅ Módulo SSH importado correctamente")
except ImportError as e:
    SSH_AVAILABLE = False
    logger.error(f"❌ Error importando módulo SSH: {e}")
except Exception as e:
    SSH_AVAILABLE = False
    logger.error(f"❌ Error general importando SSH: {e}")

# Owner ID
OWNER_ID = int(os.environ.get('OWNER_TELEGRAM_ID', '0'))

async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /vps <comando>"""
    user_id = update.effective_user.id
    logger.info(f"📟 /vps recibido de user_id={user_id}")
    
    # Solo owner
    if user_id != OWNER_ID:
        logger.warning(f"⛔ Acceso denegado a user_id={user_id}")
        await update.message.reply_text("⛔ Solo el owner puede usar este comando.")
        return
    
    # Verificar módulo SSH
    if not SSH_AVAILABLE:
        logger.error("❌ Módulo SSH no disponible")
        await update.message.reply_text("❌ Módulo SSH no disponible. Verificar logs.")
        return
    
    # Verificar argumentos
    if not context.args:
        await update.message.reply_text(
            "Uso: /vps <comando>\n\n"
            "Ejemplos:\n"
            "• /vps docker ps\n"
            "• /vps docker logs open-webui --tail 20\n"
            "• /vps uptime\n"
            "• /vps df -h"
        )
        return
    
    comando = ' '.join(context.args)
    logger.info(f"🔧 Ejecutando comando: {comando}")
    
    await update.message.reply_text(f"⏳ Ejecutando: `{comando}`", parse_mode='Markdown')
    
    try:
        ssh = SSHManager()
        resultado = ssh.execute(comando)
        
        if resultado['success']:
            output = resultado['output'] or "(sin output)"
            # Truncar si es muy largo
            if len(output) > 4000:
                output = output[:4000] + "\n... (truncado)"
            
            await update.message.reply_text(
                f"```\n{output}\n```",
                parse_mode='Markdown'
            )
            logger.info(f"✅ Comando ejecutado exitosamente")
        else:
            error = resultado.get('error', 'Error desconocido')
            await update.message.reply_text(f"❌ Error:\n```\n{error}\n```", parse_mode='Markdown')
            logger.error(f"❌ Error en comando: {error}")
            
    except Exception as e:
        logger.exception(f"💥 Excepción ejecutando comando VPS: {e}")
        await update.message.reply_text(f"💥 Error: {str(e)}")
