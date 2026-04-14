"""
Módulo SSH para control remoto del VPS desde Telegram.
"""
import asyncio
import os
import tempfile
import logging

logger = logging.getLogger(__name__)

# Configuración del VPS
VPS_HOST = os.getenv("VPS_HOST", "31.97.151.119")
VPS_USER = os.getenv("VPS_USER", "root")
VPS_PORT = int(os.getenv("VPS_PORT", "22"))
VPS_SSH_KEY = os.getenv("VPS_SSH_KEY", "")

async def run_ssh_command(command: str, timeout: int = 30) -> dict:
    """
    Ejecuta un comando SSH en el VPS.
    Retorna dict con stdout, stderr, exit_code.
    """
    if not VPS_SSH_KEY:
        return {
            "success": False,
            "error": "VPS_SSH_KEY no configurada",
            "stdout": "",
            "stderr": "",
            "exit_code": -1
        }
    
    # Crear archivo temporal con la key
    key_file = None
    try:
        key_file = tempfile.NamedTemporaryFile(mode='w', suffix='.key', delete=False)
        key_file.write(VPS_SSH_KEY)
        key_file.close()
        os.chmod(key_file.name, 0o600)
        
        # Construir comando SSH
        ssh_cmd = [
            "ssh",
            "-i", key_file.name,
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            "-p", str(VPS_PORT),
            f"{VPS_USER}@{VPS_HOST}",
            command
        ]
        
        # Ejecutar
        process = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
            
            return {
                "success": process.returncode == 0,
                "stdout": stdout.decode('utf-8', errors='replace').strip(),
                "stderr": stderr.decode('utf-8', errors='replace').strip(),
                "exit_code": process.returncode,
                "error": None
            }
        except asyncio.TimeoutError:
            process.kill()
            return {
                "success": False,
                "error": f"Timeout después de {timeout}s",
                "stdout": "",
                "stderr": "",
                "exit_code": -1
            }
            
    except Exception as e:
        logger.error(f"Error SSH: {e}")
        return {
            "success": False,
            "error": str(e),
            "stdout": "",
            "stderr": "",
            "exit_code": -1
        }
    finally:
        # Limpiar archivo temporal
        if key_file and os.path.exists(key_file.name):
            os.unlink(key_file.name)


# Comandos predefinidos
COMMANDS = {
    "uptime": "uptime -p",
    "disk": "df -h / | tail -1 | awk '{print \"Usado: \" $3 \" / \" $2 \" (\" $5 \")\"}'",
    "memory": "free -h | awk '/^Mem:/ {print \"RAM: \" $3 \" / \" $2}'",
    "cpu": "top -bn1 | grep 'Cpu(s)' | awk '{print \"CPU: \" $2 \"%\"}'",
    "status": "uptime -p && free -h | awk '/^Mem:/ {print \"RAM: \" $3 \" / \" $2}' && df -h / | tail -1 | awk '{print \"Disco: \" $3 \" / \" $2 \" (\" $5 \")\"}'",
    "docker": "docker ps --format 'table {{.Names}}\t{{.Status}}'",
    "docker-stats": "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'",
    "logs": "journalctl -n 20 --no-pager",
    "netstat": "ss -tuln | head -20",
    "top": "ps aux --sort=-%cpu | head -10",
    "reboot": "sudo reboot",
}


async def handle_vps_command(args: str) -> str:
    """
    Handler principal para comandos /vps.
    """
    args = args.strip().lower() if args else ""
    
    # Sin argumentos = mostrar ayuda
    if not args:
        return """🖥️ *Comandos VPS disponibles:*

`/vps status` - Estado general
`/vps uptime` - Tiempo encendido
`/vps disk` - Uso de disco
`/vps memory` - Uso de RAM
`/vps cpu` - Uso de CPU
`/vps docker` - Contenedores activos
`/vps docker-stats` - Stats de containers
`/vps logs` - Últimos logs del sistema
`/vps top` - Procesos más pesados
`/vps exec <comando>` - Ejecutar comando custom

🔴 `/vps reboot` - Reiniciar VPS"""
    
    # Comando custom
    if args.startswith("exec "):
        custom_cmd = args[5:].strip()
        if not custom_cmd:
            return "❌ Especificá el comando. Ej: `/vps exec ls -la`"
        result = await run_ssh_command(custom_cmd)
    elif args in COMMANDS:
        # Confirmación para reboot
        if args == "reboot":
            return "⚠️ ¿Seguro que querés reiniciar el VPS? Respondé `/vps reboot confirm`"
        result = await run_ssh_command(COMMANDS[args])
    elif args == "reboot confirm":
        result = await run_ssh_command(COMMANDS["reboot"])
        if result["success"]:
            return "🔄 VPS reiniciando..."
    else:
        return f"❌ Comando no reconocido: `{args}`\n\nUsá `/vps` para ver comandos disponibles."
    
    # Formatear respuesta
    if not result["success"]:
        error_msg = result.get("error") or result.get("stderr") or "Error desconocido"
        return f"❌ Error: {error_msg}"
    
    output = result["stdout"]
    if not output:
        output = "(sin output)"
    
    # Truncar si es muy largo
    if len(output) > 3500:
        output = output[:3500] + "\n... (truncado)"
    
    return f"```\n{output}\n```"


def setup(app):
    """Setup para integración con el bot."""
    logger.info("✅ Módulo SSH cargado")
    return True
