"""
VPS Handler - Ejecuta comandos en el VPS via SSH usando Paramiko
Usa la variable de entorno VPS_PRIVATE_KEY para autenticación
"""

import os
import io
import paramiko
from telegram import Update
from telegram.ext import ContextTypes

# Configuración del VPS
VPS_HOST = os.getenv("VPS_HOST", "31.97.151.119")
VPS_USER = os.getenv("VPS_USER", "root")
VPS_PORT = int(os.getenv("VPS_PORT", "22"))

# Lista de usuarios autorizados (owner)
OWNER_IDS = [8626420783]


def ejecutar_ssh(comando: str, timeout: int = 30) -> tuple[bool, str]:
    """
    Ejecuta un comando en el VPS usando Paramiko.
    Retorna (success, output)
    """
    private_key_str = os.getenv("VPS_PRIVATE_KEY")
    
    if not private_key_str:
        return False, "❌ VPS_PRIVATE_KEY no está configurada en las variables de entorno"
    
    try:
        # Cargar la clave privada desde string
        # Asegurarse de que tenga el formato correcto
        key_str = private_key_str.replace("\\n", "\n")
        if not key_str.endswith("\n"):
            key_str += "\n"
            
        key_file = io.StringIO(key_str)
        
        # Intentar cargar como diferentes tipos de clave
        private_key = None
        errors = []
        
        # Intentar RSA
        try:
            key_file.seek(0)
            private_key = paramiko.RSAKey.from_private_key(key_file)
        except Exception as e:
            errors.append(f"RSA: {e}")
        
        # Intentar Ed25519
        if not private_key:
            try:
                key_file.seek(0)
                private_key = paramiko.Ed25519Key.from_private_key(key_file)
            except Exception as e:
                errors.append(f"Ed25519: {e}")
        
        # Intentar ECDSA
        if not private_key:
            try:
                key_file.seek(0)
                private_key = paramiko.ECDSAKey.from_private_key(key_file)
            except Exception as e:
                errors.append(f"ECDSA: {e}")
        
        if not private_key:
            return False, f"❌ No se pudo cargar la clave privada.\nErrores: {'; '.join(errors)}"
        
        # Conectar al VPS
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
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
        stdin, stdout, stderr = client.exec_command(comando, timeout=timeout)
        
        output = stdout.read().decode('utf-8', errors='replace')
        error = stderr.read().decode('utf-8', errors='replace')
        exit_code = stdout.channel.recv_exit_status()
        
        client.close()
        
        # Combinar output
        result = ""
        if output:
            result += output
        if error:
            if result:
                result += "\n"
            result += error
        
        if not result:
            result = "(sin output)"
            
        # Truncar si es muy largo
        if len(result) > 3500:
            result = result[:3500] + "\n... (truncado)"
        
        if exit_code == 0:
            return True, f"✅ Ejecutado:\n```\n{result}\n```"
        else:
            return False, f"⚠️ Exit code {exit_code}:\n```\n{result}\n```"
            
    except paramiko.AuthenticationException:
        return False, "❌ Error de autenticación SSH. Verificá la clave privada."
    except paramiko.SSHException as e:
        return False, f"❌ Error SSH: {e}"
    except TimeoutError:
        return False, f"❌ Timeout después de {timeout}s"
    except Exception as e:
        return False, f"❌ Error: {type(e).__name__}: {e}"


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para /vps - ejecuta comandos en el VPS"""
    
    user_id = update.effective_user.id
    
    # Verificar autorización
    if user_id not in OWNER_IDS:
        await update.message.reply_text("❌ No autorizado")
        return
    
    # Obtener comando
    if not context.args:
        await update.message.reply_text(
            "🖥️ *VPS Control*\n\n"
            "Uso: `/vps <comando>`\n\n"
            "Ejemplos:\n"
            "• `/vps uptime`\n"
            "• `/vps df -h`\n"
            "• `/vps docker ps`\n"
            "• `/vps free -m`",
            parse_mode="Markdown"
        )
        return
    
    comando = " ".join(context.args)
    
    # Mensaje de espera
    msg = await update.message.reply_text(f"⏳ Ejecutando: `{comando}`...", parse_mode="Markdown")
    
    # Ejecutar
    success, output = ejecutar_ssh(comando)
    
    # Responder
    await msg.edit_text(output, parse_mode="Markdown")


# Función para usar desde otros módulos
def ssh_exec(comando: str) -> str:
    """Wrapper simple para ejecutar comandos SSH"""
    success, output = ejecutar_ssh(comando)
    return output
