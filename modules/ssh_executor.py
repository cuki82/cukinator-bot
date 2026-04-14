"""
SSH Executor Module - Ejecuta comandos remotos en VPS vía SSH
"""

import paramiko
import io
import os
from typing import Optional, Tuple

class SSHExecutor:
    def __init__(self):
        self.host = os.getenv('SSH_HOST', '31.97.151.119')
        self.port = int(os.getenv('SSH_PORT', '22'))
        self.username = os.getenv('SSH_USER', 'root')
        self.private_key = os.getenv('SSH_PRIVATE_KEY', '')
    
    def execute(self, command: str, timeout: int = 30) -> Tuple[bool, str, str]:
        """
        Ejecuta un comando en el VPS remoto.
        
        Returns:
            Tuple[bool, str, str]: (success, stdout, stderr)
        """
        if not self.private_key:
            return False, "", "SSH_PRIVATE_KEY no configurada"
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Cargar clave privada desde string
            key_file = io.StringIO(self.private_key)
            private_key = paramiko.Ed25519Key.from_private_key(key_file)
            
            # Conectar
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                pkey=private_key,
                timeout=10
            )
            
            # Ejecutar comando
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            exit_code = stdout.channel.recv_exit_status()
            
            client.close()
            
            return exit_code == 0, out, err
            
        except paramiko.AuthenticationException:
            return False, "", "Error de autenticación SSH"
        except paramiko.SSHException as e:
            return False, "", f"Error SSH: {str(e)}"
        except Exception as e:
            return False, "", f"Error: {str(e)}"
        finally:
            client.close()
    
    def test_connection(self) -> Tuple[bool, str]:
        """Prueba la conexión SSH."""
        success, out, err = self.execute("echo 'SSH OK' && hostname && uptime")
        if success:
            return True, out
        return False, err


# Singleton
_executor: Optional[SSHExecutor] = None

def get_executor() -> SSHExecutor:
    global _executor
    if _executor is None:
        _executor = SSHExecutor()
    return _executor

def ssh_execute(command: str, timeout: int = 30) -> dict:
    """
    Función principal para ejecutar comandos SSH.
    Retorna dict con: success, output, error
    """
    executor = get_executor()
    success, stdout, stderr = executor.execute(command, timeout)
    
    return {
        "success": success,
        "output": stdout.strip() if stdout else "",
        "error": stderr.strip() if stderr else "",
        "command": command
    }

def ssh_test() -> dict:
    """Prueba la conexión SSH."""
    executor = get_executor()
    success, message = executor.test_connection()
    return {
        "success": success,
        "message": message
    }
