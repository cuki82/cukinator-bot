"""
SSH Executor - Ejecuta comandos en VPS remoto via Paramiko
"""
import os
import io
import paramiko

# Configuración del VPS
VPS_HOST = os.getenv('VPS_HOST', '31.97.151.119')
VPS_USER = os.getenv('VPS_USER', 'root')
VPS_PORT = int(os.getenv('VPS_PORT', '22'))

def execute_ssh_command(command: str, timeout: int = 30) -> dict:
    """
    Ejecuta un comando en el VPS via SSH usando Paramiko.
    
    Args:
        command: Comando a ejecutar
        timeout: Timeout en segundos
        
    Returns:
        dict con stdout, stderr, exit_code
    """
    private_key_str = os.getenv('SSH_PRIVATE_KEY', '')
    
    if not private_key_str:
        return {
            'success': False,
            'error': 'SSH_PRIVATE_KEY no configurada',
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }
    
    # Convertir \n literales a newlines reales
    private_key_str = private_key_str.replace('\\n', '\n')
    
    # Asegurar que termine con newline
    if not private_key_str.endswith('\n'):
        private_key_str += '\n'
    
    try:
        # Cargar la clave privada desde string
        key_file = io.StringIO(private_key_str)
        
        # Intentar cargar como Ed25519 primero
        try:
            private_key = paramiko.Ed25519Key.from_private_key(key_file)
        except Exception:
            # Si falla, intentar como RSA
            key_file.seek(0)
            try:
                private_key = paramiko.RSAKey.from_private_key(key_file)
            except Exception as e:
                return {
                    'success': False,
                    'error': f'No se pudo cargar la clave SSH: {str(e)}',
                    'stdout': '',
                    'stderr': '',
                    'exit_code': -1
                }
        
        # Crear cliente SSH
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Conectar
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
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        
        exit_code = stdout.channel.recv_exit_status()
        stdout_text = stdout.read().decode('utf-8', errors='replace')
        stderr_text = stderr.read().decode('utf-8', errors='replace')
        
        client.close()
        
        return {
            'success': exit_code == 0,
            'stdout': stdout_text,
            'stderr': stderr_text,
            'exit_code': exit_code,
            'error': None
        }
        
    except paramiko.AuthenticationException as e:
        return {
            'success': False,
            'error': f'Error de autenticación SSH: {str(e)}',
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }
    except paramiko.SSHException as e:
        return {
            'success': False,
            'error': f'Error SSH: {str(e)}',
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }
    except Exception as e:
        return {
            'success': False,
            'error': f'Error: {str(e)}',
            'stdout': '',
            'stderr': '',
            'exit_code': -1
        }


def get_vps_status() -> str:
    """Obtiene estado básico del VPS."""
    commands = [
        "uptime",
        "free -h | grep Mem",
        "df -h / | tail -1"
    ]
    
    results = []
    for cmd in commands:
        result = execute_ssh_command(cmd, timeout=10)
        if result['success']:
            results.append(result['stdout'].strip())
        else:
            results.append(f"Error: {result.get('error', 'desconocido')}")
    
    return f"""📊 **Estado del VPS**
    
🕐 Uptime: {results[0]}
💾 Memoria: {results[1]}
💿 Disco: {results[2]}"""
