"""
Módulo SSH para ejecutar comandos remotos en el VPS
"""
import subprocess
import tempfile
import os

def execute_ssh_command(command: str, timeout: int = 30) -> dict:
    """
    Ejecuta un comando SSH en el VPS configurado.
    
    Args:
        command: Comando a ejecutar
        timeout: Timeout en segundos (default 30)
    
    Returns:
        dict con 'success', 'output' o 'error'
    """
    # Configuración desde variables de entorno
    host = os.environ.get('SSH_HOST', '31.97.151.119')
    user = os.environ.get('SSH_USER', 'root')
    port = os.environ.get('SSH_PORT', '22')
    private_key = os.environ.get('SSH_PRIVATE_KEY', '')
    
    if not private_key:
        return {
            'success': False,
            'error': 'SSH_PRIVATE_KEY no configurada en Railway'
        }
    
    # Convertir \n literales a saltos de línea reales
    private_key = private_key.replace('\\n', '\n')
    
    # Crear archivo temporal con la clave
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.pem', delete=False) as f:
            f.write(private_key)
            key_file = f.name
        
        # Permisos correctos para la clave
        os.chmod(key_file, 0o600)
        
        # Construir comando SSH
        ssh_cmd = [
            'ssh',
            '-i', key_file,
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'ConnectTimeout=10',
            '-o', 'BatchMode=yes',
            '-p', port,
            f'{user}@{host}',
            command
        ]
        
        # Ejecutar
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        # Limpiar archivo temporal
        os.unlink(key_file)
        
        if result.returncode == 0:
            return {
                'success': True,
                'output': result.stdout.strip() or '(sin output)'
            }
        else:
            return {
                'success': False,
                'error': f"Error (código {result.returncode}):\n{result.stderr.strip() or result.stdout.strip()}"
            }
            
    except subprocess.TimeoutExpired:
        if 'key_file' in locals():
            os.unlink(key_file)
        return {
            'success': False,
            'error': f'Timeout después de {timeout} segundos'
        }
    except Exception as e:
        if 'key_file' in locals():
            os.unlink(key_file)
        return {
            'success': False,
            'error': f'Error: {str(e)}'
        }


def get_vps_status() -> dict:
    """Obtiene estado básico del VPS"""
    commands = {
        'uptime': 'uptime',
        'disk': 'df -h / | tail -1',
        'memory': 'free -h | grep Mem',
        'load': 'cat /proc/loadavg'
    }
    
    results = {}
    for name, cmd in commands.items():
        result = execute_ssh_command(cmd, timeout=15)
        if result['success']:
            results[name] = result['output']
        else:
            results[name] = f"Error: {result.get('error', 'unknown')}"
    
    return results
