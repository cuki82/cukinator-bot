"""
modules/vps.py — Operaciones VPS via SSH.
"""
import os
from modules.ssh import execute, read_file, write_file, VPS_HOST


def status() -> dict:
    cmd = (
        "echo '=== SISTEMA ===' && hostname && uptime && "
        "echo '' && echo '=== RAM ===' && free -h && "
        "echo '' && echo '=== DISCO ===' && df -h / && "
        "echo '' && echo '=== DOCKER ===' && "
        "docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}' 2>/dev/null || echo 'Docker n/a'"
    )
    r = execute(cmd, timeout=20)
    return {"success": r["success"], "output": r["stdout"] or r["stderr"], "host": VPS_HOST}


def docker_ps() -> dict:
    r = execute("docker ps --format 'table {{.Names}}\t{{.Ports}}\t{{.Status}}'")
    return {"success": r["success"], "output": r["stdout"]}


def run_command(command: str, timeout: int = 30) -> dict:
    r = execute(command, timeout)
    return {"success": r["success"], "output": r["stdout"] or r["stderr"], "host": VPS_HOST}


def read_vps_file(path: str) -> dict:
    return read_file(path)


def write_vps_file(path: str, content: str) -> dict:
    return write_file(path, content)
