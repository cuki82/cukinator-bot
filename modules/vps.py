"""
Módulo VPS - Ejecución remota de comandos via SSH
"""
import paramiko
import io
from typing import Optional, Tuple
from utils.db import get_db_connection

class VPSClient:
    def __init__(self):
        self.host = None
        self.user = None
        self.key = None
        self._load_credentials()
    
    def _load_credentials(self):
        """Carga credenciales desde la DB"""
        conn = get_db_connection()
        if not conn:
            return
        
        try:
            cur = conn.cursor()
            
            # Buscar host
            cur.execute("""
                SELECT value FROM secrets 
                WHERE key_name = 'VPS_HOST' 
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                self.host = row[0]
            
            # Buscar user
            cur.execute("""
                SELECT value FROM secrets 
                WHERE key_name = 'VPS_USER' 
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                self.user = row[0]
            
            # Buscar key
            cur.execute("""
                SELECT value FROM secrets 
                WHERE key_name = 'VPS_SSH_KEY' 
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                self.key = row[0]
                
        except Exception as e:
            print(f"Error loading VPS credentials: {e}")
        finally:
            conn.close()
    
    def is_configured(self) -> bool:
        """Verifica si el VPS está configurado"""
        return all([self.host, self.user, self.key])
    
    def execute(self, command: str, timeout: int = 30) -> Tuple[bool, str]:
        """
        Ejecuta un comando en el VPS via SSH
        Returns: (success, output)
        """
        if not self.is_configured():
            missing = []
            if not self.host:
                missing.append("VPS_HOST")
            if not self.user:
                missing.append("VPS_USER")
            if not self.key:
                missing.append("VPS_SSH_KEY")
            return False, f"VPS no configurado. Faltan: {', '.join(missing)}"
        
        try:
            # Crear cliente SSH
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Cargar la key privada
            key_file = io.StringIO(self.key)
            private_key = paramiko.Ed25519Key.from_private_key(key_file)
            
            # Conectar
            client.connect(
                hostname=self.host,
                username=self.user,
                pkey=private_key,
                timeout=10
            )
            
            # Ejecutar comando
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            
            # Leer output
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            
            client.close()
            
            if error and not output:
                return False, error
            
            return True, output if output else error
            
        except paramiko.AuthenticationException:
            return False, "Error de autenticación SSH. Verificar key y usuario."
        except paramiko.SSHException as e:
            return False, f"Error SSH: {str(e)}"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def test_connection(self) -> Tuple[bool, str]:
        """Prueba la conexión al VPS"""
        success, output = self.execute("echo 'Conexión OK' && hostname && uptime")
        if success:
            return True, f"✓ Conectado a {self.host}\n{output}"
        return False, output
    
    def get_docker_status(self) -> Tuple[bool, str]:
        """Lista contenedores Docker corriendo"""
        return self.execute('docker ps --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}"')
    
    def get_system_status(self) -> Tuple[bool, str]:
        """Estado general del sistema"""
        cmd = """
echo "=== SISTEMA ===" && hostname && uptime
echo ""
echo "=== MEMORIA ===" && free -h
echo ""
echo "=== DISCO ===" && df -h /
echo ""
echo "=== DOCKER ===" && docker ps --format "table {{.Names}}\t{{.Ports}}\t{{.Status}}" 2>/dev/null || echo "Docker no disponible"
"""
        return self.execute(cmd)
    
    def get_listening_ports(self) -> Tuple[bool, str]:
        """Lista puertos en escucha"""
        return self.execute("ss -tlnp | head -20")


# Singleton
_vps_client = None

def get_vps_client() -> VPSClient:
    global _vps_client
    if _vps_client is None:
        _vps_client = VPSClient()
    return _vps_client


def vps_execute(command: str, timeout: int = 30) -> dict:
    """
    Tool function para ejecutar comandos en VPS
    """
    client = get_vps_client()
    success, output = client.execute(command, timeout)
    return {
        "success": success,
        "output": output,
        "host": client.host
    }


def vps_status() -> dict:
    """
    Tool function para ver estado del VPS
    """
    client = get_vps_client()
    success, output = client.get_system_status()
    return {
        "success": success,
        "output": output,
        "host": client.host
    }


def vps_docker() -> dict:
    """
    Tool function para ver contenedores Docker
    """
    client = get_vps_client()
    success, output = client.get_docker_status()
    return {
        "success": success,
        "output": output
    }


def vps_test() -> dict:
    """
    Tool function para probar conexión
    """
    client = get_vps_client()
    
    if not client.is_configured():
        return {
            "success": False,
            "output": "VPS no configurado. Necesito VPS_HOST, VPS_USER y VPS_SSH_KEY",
            "configured": False
        }
    
    success, output = client.test_connection()
    return {
        "success": success,
        "output": output,
        "configured": True,
        "host": client.host,
        "user": client.user
    }
