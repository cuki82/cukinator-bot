"""
Handler para comandos VPS/SSH
"""
import logging
import os
import asyncio

log = logging.getLogger(__name__)

# Configuración SSH desde variables de entorno
SSH_HOST = os.getenv("SSH_HOST")
SSH_USER = os.getenv("SSH_USER", "root")
SSH_KEY = os.getenv("SSH_PRIVATE_KEY")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))

# Verificar si tenemos credenciales
SSH_AVAILABLE = bool(SSH_HOST and SSH_KEY)

if SSH_AVAILABLE:
    log.info(f"✅ SSH configurado para {SSH_USER}@{SSH_HOST}:{SSH_PORT}")
else:
    log.warning("⚠️ SSH no configurado. Faltan SSH_HOST o SSH_PRIVATE_KEY")


async def run_ssh_command(command: str) -> dict:
    """Ejecuta un comando SSH usando asyncio subprocess"""
    if not SSH_AVAILABLE:
        return {"success": False, "error": "SSH no configurado"}
    
    try:
        # Crear archivo temporal con la key
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='_key', delete=False) as f:
            f.write(SSH_KEY)
            key_file = f.name
        
        # Asegurar permisos correctos
        os.chmod(key_file, 0o600)
        
        # Construir comando SSH
        ssh_cmd = [
            "ssh",
            "-i", key_file,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-p", str(SSH_PORT),
            f"{SSH_USER}@{SSH_HOST}",
            command
        ]
        
        # Ejecutar
        process = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=30
        )
        
        # Limpiar key temporal
        os.unlink(key_file)
        
        if process.returncode == 0:
            return {
                "success": True,
                "output": stdout.decode('utf-8', errors='replace')
            }
        else:
            return {
                "success": False,
                "error": stderr.decode('utf-8', errors='replace') or f"Exit code: {process.returncode}"
            }
            
    except asyncio.TimeoutError:
        return {"success": False, "error": "Timeout (30s)"}
    except Exception as e:
        log.error(f"Error SSH: {e}")
        return {"success": False, "error": str(e)}


async def vps_command(update, context):
    """Comando /vps - ejecuta comandos en el servidor remoto"""
    from telegram import Update
    from telegram.ext import ContextTypes
    
    log.info(f"📟 /vps recibido de user_id={update.effective_user.id}")
    
    if not SSH_AVAILABLE:
        log.error("❌ SSH no configurado")
        await update.message.reply_text(
            "SSH no configurado.\n"
            "Necesito las variables: SSH_HOST, SSH_PRIVATE_KEY\n"
            "Opcional: SSH_USER (default: root), SSH_PORT (default: 22)"
        )
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
        result = await run_ssh_command(command)
        
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
