"""
handlers/vps_handler.py — Comandos VPS via SSH
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from modules.ssh_executor import execute_ssh_command

log = logging.getLogger(__name__)

# Comandos permitidos (whitelist por seguridad)
ALLOWED_COMMANDS = {
    "status": "uptime && df -h / && free -h | head -2",
    "uptime": "uptime",
    "disk": "df -h",
    "memory": "free -h",
    "processes": "ps aux --sort=-%cpu | head -15",
    "docker": "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'",
    "logs": "journalctl -n 50 --no-pager",
    "network": "ss -tuln | head -20",
    "load": "cat /proc/loadavg && nproc",
}


async def vps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /vps <comando> - Ejecuta comandos en el VPS
    
    Comandos predefinidos: status, uptime, disk, memory, processes, docker, logs, network, load
    Comando custom: /vps run <comando>
    """
    user_id = update.effective_user.id
    
    # Solo owner puede usar VPS
    OWNER_ID = 8626420783
    if user_id != OWNER_ID:
        await update.message.reply_text("No tenés permiso para usar comandos VPS.")
        return
    
    if not context.args:
        help_text = """*Comandos VPS disponibles:*

`/vps status` — Estado general
`/vps uptime` — Uptime del servidor
`/vps disk` — Uso de disco
`/vps memory` — Uso de memoria
`/vps processes` — Top procesos por CPU
`/vps docker` — Containers Docker
`/vps logs` — Últimos logs del sistema
`/vps network` — Puertos abiertos
`/vps load` — Load average

`/vps run <comando>` — Comando custom"""
        await update.message.reply_text(help_text, parse_mode="Markdown")
        return
    
    cmd_name = context.args[0].lower()
    
    # Comando custom
    if cmd_name == "run" and len(context.args) > 1:
        custom_cmd = " ".join(context.args[1:])
        log.info(f"VPS custom command from {user_id}: {custom_cmd}")
        await execute_and_reply(update, custom_cmd)
        return
    
    # Comando predefinido
    if cmd_name in ALLOWED_COMMANDS:
        command = ALLOWED_COMMANDS[cmd_name]
        log.info(f"VPS command from {user_id}: {cmd_name}")
        await execute_and_reply(update, command)
        return
    
    await update.message.reply_text(f"Comando no reconocido: `{cmd_name}`\nUsá `/vps` para ver opciones.", parse_mode="Markdown")


async def execute_and_reply(update: Update, command: str):
    """Ejecuta comando SSH y envía resultado"""
    msg = await update.message.reply_text("Ejecutando...")
    
    try:
        result = execute_ssh_command(command, timeout=30)
        
        if result["success"]:
            output = result["stdout"].strip() or "(sin output)"
        else:
            if result["error"]:
                output = f"Error: {result['error']}"
            else:
                output = f"Exit code {result['exit_code']}:\n{result['stderr']}"
        
        # Truncar si es muy largo
        if len(output) > 4000:
            output = output[:4000] + "\n... (truncado)"
        
        await msg.edit_text(f"```\n{output}\n```", parse_mode="Markdown")
        
    except Exception as e:
        log.error(f"VPS error: {e}")
        await msg.edit_text(f"Error ejecutando comando: {e}")
