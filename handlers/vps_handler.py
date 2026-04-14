"""
Handler para comandos VPS/SSH
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Intentar importar el módulo SSH
try:
    from modules.ssh_module import SSHModule
    ssh = SSHModule()
    SSH_AVAILABLE = True
    log.info("✅ Módulo SSH cargado correctamente")
except ImportError as e:
    SSH_AVAILABLE = False
    ssh = None
    log.error(f"❌ No se pudo cargar módulo SSH: {e}")
except Exception as e:
    SSH_AVAILABLE = False
    ssh = None
    log.error(f"❌ Error inicializando SSH: {e}")


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /vps - ejecuta comandos en el servidor remoto"""
    log.info(f"📟 /vps recibido de user_id={update.effective_user.id}")
    
    if not SSH_AVAILABLE:
        log.error("❌ Módulo SSH no disponible")
        await update.message.reply_text("Módulo SSH no disponible. Verificar logs")
        return
    
    # Obtener el comando a ejecutar
    if context.args:
        command = " ".join(context.args)
    else:
        await update.message.reply_text(
            "Uso: /vps <comando>\n"
            "Ejemplo: /vps uptime\n"
            "Ejemplo: /vps df -h\n"
            "Ejemplo: /vps free -m"
        )
        return
    
    try:
        await update.message.reply_text(f"⏳ Ejecutando: {command}")
        result = await ssh.execute(command)
        
        # Formatear respuesta
        if result.get("success"):
            output = result.get("output", "").strip()
            if output:
                # Truncar si es muy largo
                if len(output) > 4000:
                    output = output[:4000] + "\n... (truncado)"
                await update.message.reply_text(f"```\n{output}\n```", parse_mode="Markdown")
            else:
                await update.message.reply_text("✅ Comando ejecutado (sin output)")
        else:
            error = result.get("error", "Error desconocido")
            await update.message.reply_text(f"❌ Error: {error}")
            
    except Exception as e:
        log.error(f"Error ejecutando comando SSH: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
