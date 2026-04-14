"""
Módulo SSH para control remoto del VPS desde Telegram.
Permite ejecutar comandos Docker y sistema de forma segura.
"""

import asyncio
import paramiko
import io
import os
from typing import Optional, Tuple

# Configuración del VPS
VPS_CONFIG = {
    "host": "31.97.151.119",
    "port": 22,
    "username": "cukibot",
}

# Comandos permitidos (whitelist de seguridad)
ALLOWED_COMMANDS = {
    # Docker
    "docker ps": "Lista contenedores activos",
    "docker ps -a": "Lista todos los contenedores",
    "docker images": "Lista imágenes Docker",
    "docker stats --no-stream": "Estadísticas de recursos",
    "docker network ls": "Lista redes Docker",
    
    # Logs (con parámetro dinámico)
    "docker logs": "Ver logs de un contenedor (agregar nombre)",
    
    # Control de contenedores (con parámetro dinámico)
    "docker restart": "Reiniciar contenedor (agregar nombre)",
    "docker stop": "Detener contenedor (agregar nombre)",
    "docker start": "Iniciar contenedor (agregar nombre)",
    
    # Sistema
    "df -h": "Espacio en disco",
    "free -h": "Memoria disponible",
    "uptime": "Tiempo activo del servidor",
    "top -bn1 | head -20": "Procesos principales",
    "whoami": "Usuario actual",
    
    # Docker Compose
    "docker compose ls": "Lista stacks de compose",
}

# Prefijos permitidos para comandos con argumentos
ALLOWED_PREFIXES = [
    "docker logs",
    "docker restart",
    "docker stop", 
    "docker start",
    "docker exec",
    "docker inspect",
]


def get_ssh_key() -> Optional[str]:
    """Obtiene la SSH key desde variables de entorno."""
    return os.environ.get("VPS_SSH_KEY")


def is_command_allowed(command: str) -> Tuple[bool, str]:
    """
    Verifica si un comando está permitido.
    Retorna (allowed, reason)
    """
    command = command.strip()
    
    # Comandos exactos permitidos
    if command in ALLOWED_COMMANDS:
        return True, "OK"
    
    # Comandos con prefijo permitido
    for prefix in ALLOWED_PREFIXES:
        if command.startswith(prefix + " "):
            return True, "OK"
    
    # Comandos peligrosos bloqueados explícitamente
    dangerous = ["rm ", "rmdir", "dd ", "mkfs", "> /", "sudo", "chmod 777", "curl | bash", "wget | bash"]
    for d in dangerous:
        if d in command:
            return False, f"Comando bloqueado por seguridad: contiene '{d}'"
    
    return False, f"Comando no autorizado. Usá /vps help para ver comandos permitidos."


async def execute_ssh_command(command: str) -> Tuple[bool, str]:
    """
    Ejecuta un comando SSH en el VPS.
    Retorna (success, output)
    """
    # Validar comando
    allowed, reason = is_command_allowed(command)
    if not allowed:
        return False, reason
    
    # Obtener SSH key
    ssh_key = get_ssh_key()
    if not ssh_key:
        return False, "Error: SSH key no configurada. Falta VPS_SSH_KEY en secrets."
    
    try:
        # Crear cliente SSH
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Cargar clave privada desde string
        key_file = io.StringIO(ssh_key)
        private_key = paramiko.Ed25519Key.from_private_key(key_file)
        
        # Conectar
        client.connect(
            hostname=VPS_CONFIG["host"],
            port=VPS_CONFIG["port"],
            username=VPS_CONFIG["username"],
            pkey=private_key,
            timeout=30,
            allow_agent=False,
            look_for_keys=False
        )
        
        # Ejecutar comando
        stdin, stdout, stderr = client.exec_command(command, timeout=60)
        
        # Leer output
        output = stdout.read().decode('utf-8', errors='replace')
        errors = stderr.read().decode('utf-8', errors='replace')
        
        # Cerrar conexión
        client.close()
        
        # Combinar output
        result = output
        if errors and not output:
            result = errors
        elif errors:
            result = f"{output}\n--- stderr ---\n{errors}"
        
        # Truncar si es muy largo
        if len(result) > 3500:
            result = result[:3500] + "\n... (truncado)"
        
        if not result.strip():
            result = "(comando ejecutado sin output)"
            
        return True, result
        
    except paramiko.AuthenticationException:
        return False, "Error de autenticación SSH. Verificar clave."
    except paramiko.SSHException as e:
        return False, f"Error SSH: {str(e)}"
    except TimeoutError:
        return False, "Timeout conectando al VPS."
    except Exception as e:
        return False, f"Error: {str(e)}"


def get_help_text() -> str:
    """Retorna el texto de ayuda con comandos disponibles."""
    lines = ["**Comandos VPS disponibles:**\n"]
    
    lines.append("**Docker:**")
    for cmd, desc in ALLOWED_COMMANDS.items():
        if cmd.startswith("docker"):
            lines.append(f"• `{cmd}` - {desc}")
    
    lines.append("\n**Sistema:**")
    for cmd, desc in ALLOWED_COMMANDS.items():
        if not cmd.startswith("docker"):
            lines.append(f"• `{cmd}` - {desc}")
    
    lines.append("\n**Uso:** `/vps <comando>`")
    lines.append("**Ejemplo:** `/vps docker ps`")
    
    return "\n".join(lines)


# Función principal para llamar desde el bot
async def handle_vps_command(args: str) -> str:
    """
    Handler principal para comandos /vps desde Telegram.
    """
    if not args or args.strip().lower() == "help":
        return get_help_text()
    
    command = args.strip()
    
    # Ejecutar
    success, result = await execute_ssh_command(command)
    
    if success:
        return f"```\n{result}\n```"
    else:
        return f"❌ {result}"
