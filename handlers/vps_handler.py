"""
Handler para comandos VPS/SSH
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Intentar importar el módulo SSH
try:
    from modules.ssh_module import handle_vps_command, run_ssh_command
    SSH_AVAILABLE = True
    log.info("✅ Módulo SSH cargado correctamente")
except ImportError as e:
    SSH_AVAILABLE = False
    handle_vps_command = None
    log.error(f"❌ No se pudo cargar módulo SSH: {e}")
except Exception as e:
    SSH_AVAILABLE = False
    handle_vps_command = None
    log.error(f"❌ Error inicializando SSH: {e}")


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /vps - ejecuta comandos en el servidor remoto"""
    log.info(f"📟 /vps recibido de user_id={update.effective_user.id}")
    
    if not SSH_AVAILABLE:
        log.error("❌ Módulo SSH no disponible")
        await update.message.reply_text("❌ Módulo SSH no disponible. Verificar logs.")
        return
    
    # Obtener argumentos
    args = " ".join(context.args) if context.args else ""
    
    try:
        # Usar el handler del módulo SSH
        result = await handle_vps_command(args)
        await update.message.reply_text(result, parse_mode="Markdown")
            
    except Exception as e:
        log.error(f"Error ejecutando comando SSH: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
