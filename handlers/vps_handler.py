"""
Handler para comandos VPS/SSH - Autocontenido
"""
import logging
import os
import asyncio
import tempfile
from telegram import Update
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Configuración SSH desde variables de entorno
SSH_HOST = os.getenv("SSH_HOST")
SSH_USER = os.getenv("SSH_USER", "root")
SSH_PORT = int(os.getenv("SSH_PORT", "22"))
SSH_PRIVATE_KEY = os.getenv("SSH_PRIVATE_KEY")

def is_ssh_configured():
    """Verifica si SSH está configurado"""
    return bool(SSH_HOST and SSH_PRIVATE_KEY)

async def run_ssh_command(command: str, timeout: int = 30) -> str:
    """Ejecuta un comando SSH en el VPS"""
    if not is_ssh_configured():
        return "❌ SSH no configurado. Faltan SSH_HOST o SSH_PRIVATE_KEY en variables de entorno."
    
    # Crear archivo temporal con la key
    key_file = None
    try:
        key_file = tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False)
        key_content = SSH_PRIVATE_KEY.replace('\\n', '\n')
        key_file.write(key_content)
        key_file.close()
        os.chmod(key_file.name, 0o600)
        
        # Construir comando SSH
        ssh_cmd = [
            "ssh",
            "-i", key_file.name,
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
            timeout=timeout
        )
        
        output = stdout.decode('utf-8', errors='replace').strip()
        errors = stderr.decode('utf-8', errors='replace').strip()
        
        if process.returncode != 0:
            return f"❌ Error (código {process.returncode}):\n{errors or output}"
        
        return output if output else "(sin output)"
        
    except asyncio.TimeoutError:
        return f"❌ Timeout después de {timeout}s"
    except Exception as e:
        return f"❌ Error SSH: {str(e)}"
    finally:
        if key_file and os.path.exists(key_file.name):
            os.unlink(key_file.name)


# Comandos predefinidos
VPS_COMMANDS = {
    "status": "echo '=== SISTEMA ===' && uptime && echo && echo '=== MEMORIA ===' && free -h && echo && echo '=== DISCO ===' && df -h / && echo && echo '=== DOCKER ===' && docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo 'Docker no disponible'",
    "uptime": "uptime",
    "memory": "free -h",
    "disk": "df -h",
    "docker": "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'",
    "docker-all": "docker ps -a --format 'table {{.Names}}\t{{.Status}}'",
    "logs": "docker logs --tail 50 $(docker ps -q | head -1) 2>/dev/null || journalctl -n 50 --no-pager",
    "top": "ps aux --sort=-%mem | head -10",
    "ip": "curl -s ifconfig.me && echo",
    "reboot": "sudo reboot",
}


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /vps - ejecuta comandos en el servidor remoto"""
    user_id = update.effective_user.id
    log.info(f"📟 /vps recibido de user_id={user_id}")
    
    # Verificar configuración
    if not is_ssh_configured():
        await update.message.reply_text(
            "⚠️ SSH no configurado.\n\n"
            "Necesito estas variables en Railway:\n"
            "• `SSH_HOST` - IP o dominio del VPS\n"
            "• `SSH_PRIVATE_KEY` - Key privada completa\n"
            "• `SSH_USER` - Usuario (default: root)\n"
            "• `SSH_PORT` - Puerto (default: 22)",
            parse_mode="Markdown"
        )
        return
    
    # Obtener argumentos
    args = " ".join(context.args) if context.args else ""
    
    # Sin argumentos = mostrar ayuda
    if not args:
        commands_list = "\n".join([f"• `{cmd}`" for cmd in VPS_COMMANDS.keys()])
        await update.message.reply_text(
            f"🖥️ *VPS Control*\n\n"
            f"*Comandos rápidos:*\n{commands_list}\n\n"
            f"*Uso:*\n"
            f"`/vps status` - Estado general\n"
            f"`/vps <comando>` - Ejecutar comando custom\n\n"
            f"*Conectado a:* `{SSH_USER}@{SSH_HOST}`",
            parse_mode="Markdown"
        )
        return
    
    # Determinar comando a ejecutar
    if args.lower() in VPS_COMMANDS:
        command = VPS_COMMANDS[args.lower()]
        label = args.lower()
    else:
        command = args
        label = "custom"
    
    # Feedback inmediato
    msg = await update.message.reply_text(f"⏳ Ejecutando `{label}`...", parse_mode="Markdown")
    
    try:
        result = await run_ssh_command(command)
        
        # Formatear respuesta
        response = f"```\n{result[:3500]}\n```"
        if len(result) > 3500:
            response += "\n_(output truncado)_"
        
        await msg.edit_text(response, parse_mode="Markdown")
        
    except Exception as e:
        log.error(f"Error en /vps: {e}")
        await msg.edit_text(f"❌ Error: {str(e)}")
