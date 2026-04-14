"""
SSH Executor usando Paramiko - Lee clave privada desde variable de entorno
"""
import os
import paramiko
import io
import logging

logger = logging.getLogger(__name__)

# Configuración del VPS
VPS_HOST = os.getenv("VPS_HOST", "srv881834.hstgr.cloud")
VPS_USER = os.getenv("VPS_USER", "root")
VPS_PORT = int(os.getenv("VPS_PORT", "22"))


def get_private_key():
    """Obtiene la clave privada desde variable de entorno"""
    key_content = os.getenv("VPS_PRIVATE_KEY")
    if not key_content:
        raise ValueError("VPS_PRIVATE_KEY no está configurada")
    
    # Reemplazar literales \n por saltos de línea reales
    key_content = key_content.replace("\\n", "\n")
    
    # Intentar cargar como diferentes tipos de clave
    key_file = io.StringIO(key_content)
    
    # Probar RSA
    try:
        key_file.seek(0)
        return paramiko.RSAKey.from_private_key(key_file)
    except Exception:
        pass
    
    # Probar Ed25519
    try:
        key_file.seek(0)
        return paramiko.Ed25519Key.from_private_key(key_file)
    except Exception:
        pass
    
    # Probar ECDSA
    try:
        key_file.seek(0)
        return paramiko.ECDSAKey.from_private_key(key_file)
    except Exception:
        pass
    
    raise ValueError("No se pudo parsear la clave privada (intenté RSA, Ed25519, ECDSA)")


def execute_ssh_command(command: str, timeout: int = 30) -> dict:
    """
    Ejecuta un comando en el VPS via SSH usando Paramiko.
    
    Returns:
        dict con keys: success, stdout, stderr, error
    """
    client = None
    try:
        # Obtener clave privada
        private_key = get_private_key()
        
        # Crear cliente SSH
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Conectar
        logger.info(f"Conectando a {VPS_USER}@{VPS_HOST}:{VPS_PORT}")
        client.connect(
            hostname=VPS_HOST,
            port=VPS_PORT,
            username=VPS_USER,
            pkey=private_key,
            timeout=timeout,
            look_for_keys=False,
            allow_agent=False
        )
        
        # Ejecutar comando
        logger.info(f"Ejecutando: {command[:50]}...")
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        
        # Leer output
        stdout_text = stdout.read().decode('utf-8', errors='replace')
        stderr_text = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()
        
        return {
            "success": exit_code == 0,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "exit_code": exit_code,
            "error": None
        }
        
    except Exception as e:
        logger.error(f"Error SSH: {e}")
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "error": str(e)
        }
    finally:
        if client:
            client.close()


# Función de compatibilidad con el nombre anterior
def run_ssh_command(command: str) -> str:
    """Wrapper para compatibilidad - devuelve solo stdout o error"""
    result = execute_ssh_command(command)
    if result["success"]:
        return result["stdout"]
    elif result["error"]:
        return f"Error: {result['error']}"
    else:
        return f"Error (exit {result['exit_code']}): {result['stderr']}"
