"""
services/vault.py — API Vault con encriptacion Fernet (AES-128).

Uso:
    vault.set("ANTHROPIC_KEY", "sk-ant-...")
    vault.get("ANTHROPIC_KEY")
    vault.list()

Requiere MASTER_KEY en env vars. Unica variable que no cambia entre instalaciones.
"""
import os
import sqlite3
import logging
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/memory.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault (
    key_name    TEXT PRIMARY KEY,
    ciphertext  BLOB NOT NULL,
    service     TEXT,
    description TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _fernet() -> Fernet:
    master = os.environ.get("MASTER_KEY", "")
    if not master:
        new_key = Fernet.generate_key().decode()
        raise RuntimeError(
            f"MASTER_KEY no configurada. Ejecuta una vez y guarda como env var:\n"
            f"MASTER_KEY={new_key}"
        )
    return Fernet(master.encode())


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def init():
    _conn().close()


def set(key_name: str, value: str, service: str = None, description: str = None) -> str:
    ciphertext = _fernet().encrypt(value.encode())
    masked = value[:4] + "***" + value[-4:] if len(value) > 8 else "***"
    con = _conn()
    con.execute(
        """INSERT INTO vault (key_name, ciphertext, service, description, updated_at)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(key_name) DO UPDATE SET
               ciphertext=excluded.ciphertext, service=excluded.service,
               description=excluded.description, updated_at=CURRENT_TIMESTAMP""",
        (key_name.upper(), ciphertext, service, description)
    )
    con.commit()
    con.close()
    os.environ[key_name.upper()] = value
    logger.info(f"Vault: guardado {key_name.upper()}")
    return masked


def get(key_name: str, fallback_env: bool = True) -> str | None:
    key = key_name.upper()
    con = _conn()
    row = con.execute("SELECT ciphertext FROM vault WHERE key_name = ?", (key,)).fetchone()
    con.close()
    if row:
        try:
            return _fernet().decrypt(row[0]).decode()
        except InvalidToken:
            logger.error(f"Vault: MASTER_KEY incorrecta para {key}")
            return None
    return os.environ.get(key) if fallback_env else None


def load_all_to_env():
    """Carga todos los secrets del vault a os.environ al arrancar el bot."""
    try:
        f = _fernet()
    except RuntimeError as e:
        logger.warning(str(e))
        return 0
    con = _conn()
    rows = con.execute("SELECT key_name, ciphertext FROM vault").fetchall()
    con.close()
    loaded = 0
    for key, ciphertext in rows:
        try:
            os.environ[key] = f.decrypt(ciphertext).decode()
            loaded += 1
        except InvalidToken:
            logger.error(f"Vault: no se pudo desencriptar {key}")
    logger.info(f"Vault: {loaded}/{len(rows)} secrets cargados")
    return loaded


def delete(key_name: str) -> bool:
    con = _conn()
    cur = con.execute("DELETE FROM vault WHERE key_name = ?", (key_name.upper(),))
    con.commit()
    con.close()
    return cur.rowcount > 0


def list_keys() -> list[dict]:
    con = _conn()
    rows = con.execute(
        "SELECT key_name, service, description, created_at, updated_at FROM vault ORDER BY key_name"
    ).fetchall()
    con.close()
    return [{"key": r[0], "service": r[1], "description": r[2], "created": r[3][:16], "updated": r[4][:16]}
            for r in rows]


def rotate(new_master_key: str):
    """Re-encripta todos los secrets con una MASTER_KEY nueva."""
    old_f = _fernet()
    new_f = Fernet(new_master_key.encode())
    con = _conn()
    rows = con.execute("SELECT key_name, ciphertext FROM vault").fetchall()
    for key, ciphertext in rows:
        con.execute("UPDATE vault SET ciphertext = ? WHERE key_name = ?",
                    (new_f.encrypt(old_f.decrypt(ciphertext)), key))
    con.commit()
    con.close()
    logger.info(f"Vault: {len(rows)} secrets rotados")
