"""
SSH Executor usando Paramiko - para ejecutar comandos en VPS desde Railway
Sin dependencia de archivos de clave en disco.
"""
import paramiko
import os
import io
import logging

logger = logging.getLogger(__name__)

# Configuración del VPS
VPS_HOST = os.getenv("VPS_HOST", "31.97.151.119")
VPS_USER = os.getenv("VPS_USER", "root")
VPS_KEY = os.getenv("VPS_PRIVATE_KEY", "")  # Clave privada completa en env var

def execute_ssh_command(command: str, timeout: int = 30) -> dict:
    """
    Ejecuta un comando SSH en el VPS usando Paramiko.
    
    Args:
        command: Comando a ejecutar
        timeout: Timeout en segundos
        
    Returns:
        dict con keys: success, output, error, exit_code
    """
    if not VPS_KEY:
        return {
            "success": False,
            "output": "",
            "error": "VPS_PRIVATE_KEY no está configurada en las variables de entorno",
            "exit_code": -1
        }
    
    try:
        # Crear cliente SSH
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Cargar la clave privada desde string
        key_str = VPS_KEY.replace("\\n", "\n")  # Por si viene escapado
        key_file = io.StringIO(key_str)
        
        # Intentar cargar como RSA, si falla probar Ed25519
        try:
            private_key = paramiko.RSAKey.from_private_key(key_file)
        except paramiko.ssh_exception.SSHException:
            key_file.seek(0)
            try:
                private_key = paramiko.Ed25519Key.from_private_key(key_file)
            except paramiko.ssh_exception.SSHException:
                key_file.seek(0)
                private_key = paramiko.ECDSAKey.from_private_key(key_file)
        
        # Conectar
        logger.info(f"Conectando a {VPS_USER}@{VPS_HOST}...")
        client.connect(
            hostname=VPS_HOST,
            username=VPS_USER,
            pkey=private_key,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False
        )
        
        # Ejecutar comando
        logger.info(f"Ejecutando: {command[:50]}...")
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        
        exit_code = stdout.channel.recv_exit_status()
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        
        client.close()
        
        return {
            "success": exit_code == 0,
            "output": output,
            "error": error,
            "exit_code": exit_code
        }
        
    except paramiko.AuthenticationException as e:
        logger.error(f"Error de autenticación SSH: {e}")
        return {
            "success": False,
            "output": "",
            "error": f"Error de autenticación: {e}",
            "exit_code": -1
        }
    except paramiko.SSHException as e:
        logger.error(f"Error SSH: {e}")
        return {
            "success": False,
            "output": "",
            "error": f"Error SSH: {e}",
            "exit_code": -1
        }
    except Exception as e:
        logger.error(f"Error ejecutando SSH: {e}")
        return {
            "success": False,
            "output": "",
            "error": str(e),
            "exit_code": -1
        }


def test_connection() -> dict:
    """Prueba la conexión al VPS"""
    return execute_ssh_command("echo 'Conexión OK' && hostname && uptime")


def get_vps_status() -> dict:
    """Obtiene estado básico del VPS"""
    commands = [
        "hostname",
        "uptime -p",
        "free -h | grep Mem | awk '{print $3\"/\"$2}'",
        "df -h / | tail -1 | awk '{print $3\"/\"$2\" (\"$5\" usado)\"}'"
    ]
    return execute_ssh_command(" && ".join([
        f'echo "Host: $(hostname)"',
        f'echo "Uptime: $(uptime -p)"', 
        f'echo "RAM: $(free -h | grep Mem | awk \'{{print $3\"/\"$2}}\')"',
        f'echo "Disco: $(df -h / | tail -1 | awk \'{{print $3\"/\"$2\" (\"$5\" usado)\"}}\')\"'
    ]))
