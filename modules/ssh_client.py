"""
Módulo SSH Client para Cukinator
Permite ejecutar comandos remotos en servidores vía SSH
"""

import paramiko
import os
from io import StringIO
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)

class SSHClient:
    def __init__(self):
        self.connections = {}
    
    def connect(
        self,
        host: str,
        username: str,
        password: Optional[str] = None,
        key_path: Optional[str] = None,
        key_string: Optional[str] = None,
        port: int = 22
    ) -> Tuple[bool, str]:
        """
        Conecta a un servidor SSH
        """
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            if key_string:
                # Key privada como string
                pkey = paramiko.RSAKey.from_private_key(StringIO(key_string))
                client.connect(host, port=port, username=username, pkey=pkey)
            elif key_path and os.path.exists(key_path):
                # Key privada desde archivo
                client.connect(host, port=port, username=username, key_filename=key_path)
            elif password:
                # Password auth
                client.connect(host, port=port, username=username, password=password)
            else:
                return False, "No se proporcionó password ni key SSH"
            
            # Guardar conexión
            conn_id = f"{username}@{host}:{port}"
            self.connections[conn_id] = client
            
            logger.info(f"SSH conectado: {conn_id}")
            return True, f"Conectado a {conn_id}"
            
        except paramiko.AuthenticationException:
            return False, "Error de autenticación - verificá usuario/password"
        except paramiko.SSHException as e:
            return False, f"Error SSH: {str(e)}"
        except Exception as e:
            return False, f"Error de conexión: {str(e)}"
    
    def execute(
        self,
        command: str,
        host: str,
        username: str = "root",
        port: int = 22,
        timeout: int = 30
    ) -> Tuple[bool, str, str]:
        """
        Ejecuta un comando en el servidor
        Retorna: (success, stdout, stderr)
        """
        conn_id = f"{username}@{host}:{port}"
        
        if conn_id not in self.connections:
            return False, "", f"No hay conexión activa a {conn_id}. Conectate primero."
        
        client = self.connections[conn_id]
        
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            exit_code = stdout.channel.recv_exit_status()
            
            success = exit_code == 0
            
            logger.info(f"SSH exec [{conn_id}]: {command[:50]}... -> exit {exit_code}")
            
            return success, out, err
            
        except Exception as e:
            return False, "", f"Error ejecutando comando: {str(e)}"
    
    def disconnect(self, host: str, username: str = "root", port: int = 22) -> str:
        """
        Cierra la conexión SSH
        """
        conn_id = f"{username}@{host}:{port}"
        
        if conn_id in self.connections:
            try:
                self.connections[conn_id].close()
                del self.connections[conn_id]
                return f"Desconectado de {conn_id}"
            except:
                pass
        
        return f"No había conexión activa a {conn_id}"
    
    def disconnect_all(self):
        """
        Cierra todas las conexiones
        """
        for conn_id, client in self.connections.items():
            try:
                client.close()
            except:
                pass
        self.connections = {}
        return "Todas las conexiones cerradas"
    
    def list_connections(self) -> list:
        """
        Lista las conexiones activas
        """
        return list(self.connections.keys())


# Singleton para usar desde el bot
_ssh_client = None

def get_ssh_client() -> SSHClient:
    global _ssh_client
    if _ssh_client is None:
        _ssh_client = SSHClient()
    return _ssh_client


# Funciones helper para las tools
def ssh_connect(host: str, username: str, password: str = None, port: int = 22) -> dict:
    """
    Conecta al servidor SSH
    """
    client = get_ssh_client()
    success, message = client.connect(
        host=host,
        username=username,
        password=password,
        port=port
    )
    return {
        "success": success,
        "message": message,
        "connection": f"{username}@{host}:{port}" if success else None
    }


def ssh_exec(command: str, host: str, username: str = "root", port: int = 22, timeout: int = 30) -> dict:
    """
    Ejecuta un comando SSH
    """
    client = get_ssh_client()
    success, stdout, stderr = client.execute(
        command=command,
        host=host,
        username=username,
        port=port,
        timeout=timeout
    )
    
    return {
        "success": success,
        "stdout": stdout,
        "stderr": stderr,
        "command": command
    }


def ssh_disconnect(host: str, username: str = "root", port: int = 22) -> dict:
    """
    Desconecta del servidor
    """
    client = get_ssh_client()
    message = client.disconnect(host, username, port)
    return {
        "success": True,
        "message": message
    }
