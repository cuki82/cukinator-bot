"""
VPS Handler - Conexión SSH al VPS de Hostinger
Permite ejecutar comandos remotos desde Telegram
"""

import paramiko
import os
import io
from typing import Optional, Tuple

class VPSHandler:
    def __init__(self):
        self.host = os.environ.get("VPS_HOST")
        self.username = os.environ.get("VPS_USER", "root")
        self.port = int(os.environ.get("VPS_PORT", 22))
        self._ssh_key = os.environ.get("VPS_SSH_KEY") or os.environ.get("SSH_PRIVATE_KEY")
        self._client = None
    
    def _get_client(self) -> paramiko.SSHClient:
        """Crea o reutiliza conexión SSH"""
        if self._client is not None:
            try:
                self._client.exec_command("echo ok", timeout=5)
                return self._client
            except:
                self._client = None
        
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        if self._ssh_key:
            # Conectar con key privada
            key_file = io.StringIO(self._ssh_key)
            try:
                pkey = paramiko.RSAKey.from_private_key(key_file)
            except:
                key_file.seek(0)
                pkey = paramiko.Ed25519Key.from_private_key(key_file)
            
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                pkey=pkey,
                timeout=30
            )
        else:
            # Conectar con password (fallback)
            password = os.environ.get("VPS_PASSWORD")
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=password,
                timeout=30
            )
        
        self._client = client
        return client
    
    def execute(self, command: str, timeout: int = 60) -> Tuple[str, str, int]:
        """
        Ejecuta un comando en el VPS
        Returns: (stdout, stderr, exit_code)
        """
        client = self._get_client()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        
        return out, err, exit_code
    
    def status(self) -> dict:
        """Obtiene estado básico del VPS"""
        results = {}
        
        # Uptime
        out, _, _ = self.execute("uptime -p")
        results["uptime"] = out.strip()
        
        # Memoria
        out, _, _ = self.execute("free -h | grep Mem | awk '{print $3\"/\"$2}'")
        results["memory"] = out.strip()
        
        # Disco
        out, _, _ = self.execute("df -h / | tail -1 | awk '{print $3\"/\"$2\" (\"$5\")\"}'")
        results["disk"] = out.strip()
        
        # CPU load
        out, _, _ = self.execute("cat /proc/loadavg | awk '{print $1, $2, $3}'")
        results["load"] = out.strip()
        
        # Containers Docker (si hay)
        out, _, code = self.execute("docker ps --format '{{.Names}}: {{.Status}}' 2>/dev/null | head -5")
        if code == 0 and out.strip():
            results["docker"] = out.strip()
        
        return results
    
    def close(self):
        """Cierra la conexión"""
        if self._client:
            self._client.close()
            self._client = None


# Singleton global
_vps: Optional[VPSHandler] = None

def get_vps() -> VPSHandler:
    global _vps
    if _vps is None:
        _vps = VPSHandler()
    return _vps

def vps_execute(command: str, timeout: int = 60) -> str:
    """Helper para ejecutar comando y devolver resultado formateado"""
    vps = get_vps()
    out, err, code = vps.execute(command, timeout)
    
    result = []
    if out:
        result.append(out)
    if err:
        result.append(f"[stderr] {err}")
    if code != 0:
        result.append(f"[exit code: {code}]")
    
    return "\n".join(result) if result else "(sin output)"

def vps_status() -> str:
    """Helper para obtener status formateado"""
    vps = get_vps()
    status = vps.status()
    
    lines = [
        f"🖥 VPS Status",
        f"Uptime: {status.get('uptime', 'N/A')}",
        f"Memory: {status.get('memory', 'N/A')}",
        f"Disk: {status.get('disk', 'N/A')}",
        f"Load: {status.get('load', 'N/A')}"
    ]
    
    if "docker" in status:
        lines.append(f"Docker:\n{status['docker']}")
    
    return "\n".join(lines)
