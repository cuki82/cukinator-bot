"""
VPS Handler - Conexión SSH a servidores remotos
"""
import paramiko
import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Configuración por defecto (puede ser overrideada por config en DB)
DEFAULT_CONFIG = {
    "host": None,
    "port": 22,
    "username": None,
    "password": None,
    "key_path": None,
    "timeout": 30
}

_cached_client: Optional[paramiko.SSHClient] = None

def get_ssh_client(
    host: str,
    username: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    port: int = 22,
    timeout: int = 30
) -> paramiko.SSHClient:
    """Crea y retorna un cliente SSH conectado."""
    global _cached_client
    
    # Si ya hay conexión activa al mismo host, reutilizar
    if _cached_client is not None:
        try:
            _cached_client.exec_command("echo ping", timeout=5)
            return _cached_client
        except:
            _cached_client = None
    
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    connect_kwargs = {
        "hostname": host,
        "port": port,
        "username": username,
        "timeout": timeout,
        "allow_agent": False,
        "look_for_keys": False
    }
    
    if key_path:
        connect_kwargs["key_filename"] = key_path
    elif password:
        connect_kwargs["password"] = password
    else:
        raise ValueError("Se requiere password o key_path para conectar")
    
    client.connect(**connect_kwargs)
    _cached_client = client
    log.info(f"SSH conectado a {username}@{host}:{port}")
    
    return client


def ssh_exec(
    command: str,
    host: str,
    username: str,
    password: Optional[str] = None,
    key_path: Optional[str] = None,
    port: int = 22,
    timeout: int = 30
) -> Tuple[str, str, int]:
    """
    Ejecuta un comando SSH y retorna (stdout, stderr, exit_code).
    """
    try:
        client = get_ssh_client(host, username, password, key_path, port, timeout)
        
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        
        exit_code = stdout.channel.recv_exit_status()
        stdout_str = stdout.read().decode('utf-8', errors='replace')
        stderr_str = stderr.read().decode('utf-8', errors='replace')
        
        log.info(f"SSH exec '{command[:50]}...' -> exit {exit_code}")
        
        return stdout_str, stderr_str, exit_code
        
    except paramiko.AuthenticationException:
        return "", "Error de autenticación SSH", 1
    except paramiko.SSHException as e:
        return "", f"Error SSH: {e}", 1
    except Exception as e:
        return "", f"Error de conexión: {e}", 1


def ssh_close():
    """Cierra la conexión SSH cacheada."""
    global _cached_client
    if _cached_client:
        try:
            _cached_client.close()
        except:
            pass
        _cached_client = None
        log.info("SSH conexión cerrada")


def vps_status(host: str, username: str, password: Optional[str] = None, 
               key_path: Optional[str] = None, port: int = 22) -> str:
    """Obtiene estado básico del VPS: uptime, memoria, disco."""
    commands = [
        ("Uptime", "uptime"),
        ("Memoria", "free -h | grep Mem"),
        ("Disco", "df -h / | tail -1"),
        ("Load", "cat /proc/loadavg"),
    ]
    
    results = []
    for label, cmd in commands:
        stdout, stderr, code = ssh_exec(cmd, host, username, password, key_path, port)
        if code == 0 and stdout.strip():
            results.append(f"{label}: {stdout.strip()}")
        elif stderr:
            results.append(f"{label}: Error - {stderr.strip()}")
    
    return "\n".join(results) if results else "No se pudo obtener estado del VPS"


def vps_docker_ps(host: str, username: str, password: Optional[str] = None,
                  key_path: Optional[str] = None, port: int = 22) -> str:
    """Lista contenedores Docker corriendo."""
    stdout, stderr, code = ssh_exec(
        "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'",
        host, username, password, key_path, port
    )
    
    if code != 0:
        return f"Error listando containers: {stderr or 'comando falló'}"
    
    return stdout.strip() if stdout.strip() else "No hay containers corriendo"


def vps_docker_logs(container: str, lines: int, host: str, username: str,
                    password: Optional[str] = None, key_path: Optional[str] = None,
                    port: int = 22) -> str:
    """Obtiene logs de un container Docker."""
    stdout, stderr, code = ssh_exec(
        f"docker logs --tail {lines} {container} 2>&1",
        host, username, password, key_path, port
    )
    
    if code != 0 and not stdout:
        return f"Error obteniendo logs: {stderr or 'container no encontrado'}"
    
    return stdout.strip() if stdout.strip() else "Sin logs"


def vps_service_status(service: str, host: str, username: str,
                       password: Optional[str] = None, key_path: Optional[str] = None,
                       port: int = 22) -> str:
    """Verifica estado de un servicio systemd."""
    stdout, stderr, code = ssh_exec(
        f"systemctl status {service} --no-pager -l",
        host, username, password, key_path, port
    )
    
    # systemctl devuelve código != 0 si el servicio está parado, pero igual hay output
    if stdout.strip():
        return stdout.strip()
    elif stderr.strip():
        return f"Error: {stderr.strip()}"
    else:
        return f"Servicio '{service}' no encontrado"
