"""
modules/ssh.py — SSH/SFTP unificado para el VPS.
Consolida ssh_client.py, ssh_executor.py, ssh_module.py, ssh_vps.py.
"""
import os
import io
import logging
import paramiko

logger = logging.getLogger(__name__)

VPS_HOST = os.getenv("VPS_HOST", "31.97.151.119")
VPS_USER = os.getenv("VPS_USER", "cukibot")
VPS_PORT = int(os.getenv("VPS_PORT", "22"))

VPS_COMMANDS = {
    "uptime":       "uptime -p",
    "disk":         "df -h / | tail -1 | awk '{print \"Disco: \" $3 \" / \" $2 \" (\" $5 \")\"}' ",
    "memory":       "free -h | awk '/^Mem:/ {print \"RAM: \" $3 \" / \" $2}'",
    "status":       "uptime -p && free -h | awk '/^Mem:/ {print \"RAM: \" $3 \" / \" $2}' && df -h / | tail -1",
    "docker":       "docker ps --format 'table {{.Names}}\t{{.Status}}'",
    "docker-stats": "docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'",
    "logs":         "journalctl -n 20 --no-pager",
    "top":          "ps aux --sort=-%cpu | head -10",
}


def _load_private_key() -> paramiko.PKey:
    key_content = os.getenv("VPS_PRIVATE_KEY", "").replace("\n", "\n")
    if not key_content:
        raise ValueError("VPS_PRIVATE_KEY no configurada")
    buf = io.StringIO(key_content)
    for cls in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            buf.seek(0)
            return cls.from_private_key(buf)
        except Exception:
            pass
    raise ValueError("No se pudo parsear VPS_PRIVATE_KEY (RSA/Ed25519/ECDSA)")


def _make_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=VPS_HOST, port=VPS_PORT, username=VPS_USER,
        pkey=_load_private_key(), timeout=10,
        look_for_keys=False, allow_agent=False,
        banner_timeout=10, auth_timeout=10,
    )
    return client


def execute(command: str, timeout: int = 30) -> dict:
    client = None
    try:
        client = _make_client()
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return {"success": code == 0, "stdout": out, "stderr": err, "exit_code": code, "error": None}
    except paramiko.AuthenticationException:
        return {"success": False, "stdout": "", "stderr": "", "exit_code": -1, "error": "Auth SSH fallida"}
    except Exception as e:
        logger.error(f"SSH error: {e}")
        return {"success": False, "stdout": "", "stderr": "", "exit_code": -1, "error": str(e)}
    finally:
        if client:
            client.close()


def read_file(path: str) -> dict:
    client = None
    try:
        client = _make_client()
        sftp = client.open_sftp()
        with sftp.file(path, "r") as f:
            content = f.read().decode("utf-8", errors="replace")
        sftp.close()
        return {"success": True, "content": content, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


def write_file(path: str, content: str) -> dict:
    client = None
    try:
        client = _make_client()
        import posixpath, time
        dir_path = posixpath.dirname(path)
        client.exec_command(f"mkdir -p {dir_path}")
        time.sleep(0.3)
        sftp = client.open_sftp()
        with sftp.file(path, "w") as f:
            f.write(content.encode("utf-8"))
        sftp.close()
        return {"success": True, "path": path, "bytes": len(content.encode())}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        if client:
            client.close()


def run(command: str) -> str:
    r = execute(command)
    return r["stdout"] if r["success"] else f"Error: {r.get('error') or r['stderr']}"
