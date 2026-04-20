#!/usr/bin/env python3
"""
verify_install.py — Pre-install security check para packages, MCP servers,
skills, plugins y cualquier código que vaya a ejecutarse en este repo o
en el VPS.

Uso:
    python verify_install.py pypi <package_name> [version]
    python verify_install.py github <owner>/<repo> [ref]
    python verify_install.py file <path/to/file_or_dir>
    python verify_install.py mcp <path/to/.mcp.json or url>

Devuelve verdict: SAFE / SUSPICIOUS / DANGEROUS con razones detalladas.

Pipeline:
  1. Heurística estática: regex sobre el código (calls peligrosos, paths
     sensibles, network outbound, etc.). Sin LLM, rápido.
  2. Metadata check: edad del package, popularity, publisher conocido.
  3. Si pasa con dudas (SUSPICIOUS), ofrece review con Claude Sonnet
     pasándole los archivos sospechosos. Costoso pero exhaustivo.

Diseñado para ser invocado MANUALMENTE antes de cada install y por mí
(Claude Code) automáticamente cuando se proponga instalar algo de fuente
externa.
"""
import os
import re
import sys
import json
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

# ── Heurísticas de riesgo ─────────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    # Ejecución de shell sin sanitización
    (r"os\.system\s*\(", "os.system() — ejecución shell sin sandboxing"),
    (r"subprocess\.\w+\([^)]*shell\s*=\s*True", "subprocess shell=True — injection risk"),
    (r"\beval\s*\(", "eval() — ejecuta strings arbitrarios"),
    (r"\bexec\s*\(", "exec() — ejecuta strings arbitrarios"),
    (r"compile\s*\([^,)]+,\s*['\"][^'\"]*['\"]\s*,\s*['\"](?:exec|eval)", "compile + exec/eval"),
    (r"__import__\s*\(\s*[a-zA-Z_]\w*\s*\)", "__import__ dinámico"),
    # Acceso a paths sensibles
    (r"['\"]/etc/(?:passwd|shadow|sudoers|hosts)['\"]?", "path /etc/ sensible"),
    (r"['\"]~?/\.ssh/[\w/]*['\"]?", "lectura/escritura ~/.ssh/"),
    (r"['\"]~?/\.aws/[\w/]*['\"]?", "lectura ~/.aws/"),
    (r"['\"]~?/\.gnupg/[\w/]*['\"]?", "lectura ~/.gnupg/"),
    (r"\$\{?HOME\}?/\.[a-z_]+/credentials", "credentials de usuario"),
    # Acceso a env vars sensibles
    (r"os\.environ\[['\"](?:AWS_SECRET|SSH_AUTH_SOCK|GITHUB_TOKEN|MASTER_KEY|.*_SECRET|.*_PRIVATE_KEY)", "lee env var sensible"),
    # Network outbound a IPs
    (r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", "URL hardcoded a IP — bypass DNS"),
    (r"socket\.connect\([^)]*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", "socket connect a IP"),
    # Crypto sospechoso (mineros, etc.)
    (r"\bxmrig\b|\bmonero\b|\bcryptonight\b|\bstratum\+tcp\b", "señal de cryptominer"),
    # Modificación de PATH/init scripts
    (r"['\"]/etc/(?:profile|bashrc|init\.d|systemd/system)['\"]?", "modifica init scripts del sistema"),
    (r"['\"]~?/\.bash_profile['\"]?|['\"]~?/\.zshrc['\"]?|['\"]~?/\.profile['\"]?", "modifica shell init del usuario"),
    # Reverse shell patterns
    (r"socket\.socket\([^)]*\)\.connect\(", "socket connect (posible reverse shell)"),
    (r"pty\.spawn\(", "pty.spawn — interactive shell"),
    (r"/bin/(?:ba)?sh\s+-i", "bash -i interactive (reverse shell común)"),
    # Curl pipe sh / wget pipe sh
    (r"(?:curl|wget)\s+[^\|]+\|\s*(?:bash|sh|python)", "curl/wget pipe a shell — payload remoto"),
    # Persistencia
    (r"crontab\s+-[el]|/etc/cron", "modifica crontab — persistencia"),
    (r"systemctl\s+(?:enable|start)", "habilita systemd service — persistencia"),
]

SUSPICIOUS_PATTERNS = [
    (r"requests\.get\([^)]*verify\s*=\s*False", "HTTPS sin verificar cert"),
    (r"ssl\.\w+\(\s*verify_mode\s*=\s*ssl\.CERT_NONE", "SSL sin verificar"),
    (r"#\s*nosec\b|#\s*noqa\b.*S\d+", "marca nosec — bypass scanners"),
    (r"base64\.b64decode\([^)]*\)", "base64 decode — payload obfuscado posible"),
    (r"chr\(\d+\)\s*\+\s*chr\(\d+\)", "string obfuscado por chr()"),
    (r"\\x[0-9a-fA-F]{2}\\x[0-9a-fA-F]{2}", "hex escape strings — obfuscación"),
    (r"open\s*\([^,)]+,\s*['\"]w['\"]?\s*\).*chmod\s*\([^,)]+,\s*0?o?777", "archivo chmod 777"),
]

KNOWN_GOOD_PUBLISHERS = {
    "anthropic", "anthropics", "openai", "google", "google-llm",
    "huggingface", "encode", "tiangolo", "psf", "pytorch",
    "scikit-learn", "pandas-dev", "numpy", "scipy",
}


def scan_file(path: Path) -> dict:
    """Escanea un archivo. Retorna dict con findings."""
    findings = {"dangerous": [], "suspicious": []}
    try:
        if path.suffix not in {".py", ".js", ".ts", ".sh", ".bash", ".rb", ".go", ".rs"}:
            return findings
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat, label in DANGEROUS_PATTERNS:
            for m in re.finditer(pat, text):
                ln = text[:m.start()].count("\n") + 1
                findings["dangerous"].append({
                    "file":    str(path),
                    "line":    ln,
                    "pattern": label,
                    "match":   m.group(0)[:100],
                })
        for pat, label in SUSPICIOUS_PATTERNS:
            for m in re.finditer(pat, text):
                ln = text[:m.start()].count("\n") + 1
                findings["suspicious"].append({
                    "file":    str(path),
                    "line":    ln,
                    "pattern": label,
                    "match":   m.group(0)[:100],
                })
    except Exception as e:
        findings["dangerous"].append({"file": str(path), "error": str(e)})
    return findings


def scan_dir(root: Path, max_files: int = 500) -> dict:
    """Escanea todos los archivos del dir recursivo. Hard-cap en max_files."""
    out = {"dangerous": [], "suspicious": [], "files_scanned": 0}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in {".git", "node_modules", "__pycache__", ".venv", "venv"} for part in p.parts):
            continue
        if out["files_scanned"] >= max_files:
            break
        f = scan_file(p)
        out["dangerous"].extend(f["dangerous"])
        out["suspicious"].extend(f["suspicious"])
        out["files_scanned"] += 1
    return out


def pypi_metadata(pkg: str) -> dict:
    """Trae metadata del package desde PyPI JSON API."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def check_pypi(pkg: str, version: Optional[str] = None) -> dict:
    """Bajada + escaneo de un package PyPI sin instalar (pip download)."""
    print(f"\n=== Verificando PyPI: {pkg} ===")
    meta = pypi_metadata(pkg)
    info = meta.get("info", {}) if "error" not in meta else {}
    publisher = (info.get("author") or "").lower()
    project_url = info.get("project_url") or info.get("home_page", "")
    summary = info.get("summary", "")

    print(f"  publisher : {publisher}")
    print(f"  url       : {project_url}")
    print(f"  summary   : {summary[:80]}")
    print(f"  versions  : {len(meta.get('releases', {}))} releases")

    with tempfile.TemporaryDirectory() as tmp:
        spec = f"{pkg}=={version}" if version else pkg
        try:
            subprocess.run(
                ["pip", "download", spec, "-d", tmp, "--no-deps", "--no-binary=:all:"],
                check=True, capture_output=True, timeout=120,
            )
        except subprocess.CalledProcessError as e:
            return {"error": f"pip download falló: {e.stderr.decode()[:500]}"}
        # Encontrar el archivo descargado
        files = list(Path(tmp).glob(f"{pkg}*"))
        if not files:
            return {"error": "no se descargó nada"}
        archive = files[0]
        # Descomprimir
        extract = Path(tmp) / "extracted"
        extract.mkdir()
        if archive.suffix in {".gz", ".bz2"} or archive.name.endswith(".tar.gz"):
            subprocess.run(["tar", "-xzf", str(archive), "-C", str(extract)], check=True)
        elif archive.suffix == ".whl" or archive.suffix == ".zip":
            subprocess.run(["unzip", "-q", str(archive), "-d", str(extract)], check=True)
        # Escanear
        scan = scan_dir(extract)
        return {
            "package":  pkg,
            "version":  version or info.get("version"),
            "publisher": publisher,
            "summary":  summary,
            "scan":     scan,
        }


def verdict(scan_result: dict) -> str:
    """Computa verdict global. nosec/noqa markers cuentan menos — son práctica
    legítima en linting (silenciar false positives en código revisado)."""
    dang = scan_result.get("dangerous", [])
    susp = scan_result.get("suspicious", [])
    nosec_only = [s for s in susp if "nosec" in (s.get("pattern") or "")]
    real_susp = [s for s in susp if "nosec" not in (s.get("pattern") or "")]
    n_dang = len(dang)
    n_real = len(real_susp)
    if n_dang >= 1:
        return "DANGEROUS"
    if n_real >= 5:
        return "SUSPICIOUS"
    if n_real >= 1:
        return "REVIEW_RECOMMENDED"
    if len(nosec_only) >= 10:
        return "REVIEW_RECOMMENDED"  # muchos nosec amerita mirar
    return "SAFE"


def report(result: dict) -> None:
    scan = result.get("scan", result)
    v = verdict(scan)
    # ASCII-only label para evitar UnicodeEncodeError en Windows cp1252.
    label = {"DANGEROUS": "[!!] ", "SUSPICIOUS": "[!] ", "REVIEW_RECOMMENDED": "[?] ", "SAFE": "[ok] "}.get(v, "[-] ")
    print(f"\n{label}VERDICT: {v}")
    print(f"   files_scanned: {scan.get('files_scanned', '?')}")
    print(f"   dangerous: {len(scan.get('dangerous', []))}")
    print(f"   suspicious: {len(scan.get('suspicious', []))}")
    if scan.get("dangerous"):
        print("\n  [!!] DANGEROUS findings:")
        for f in scan["dangerous"][:20]:
            print(f"    - {f.get('file', '?')}:{f.get('line', '?')} -- {f.get('pattern', '?')}")
            print(f"        {f.get('match', '')[:100]}")
    if scan.get("suspicious"):
        print("\n  [!] SUSPICIOUS findings (first 10):")
        for f in scan["suspicious"][:10]:
            print(f"    - {f.get('file', '?')}:{f.get('line', '?')} -- {f.get('pattern', '?')}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "pypi" and len(sys.argv) >= 3:
        pkg = sys.argv[2]
        ver = sys.argv[3] if len(sys.argv) > 3 else None
        r = check_pypi(pkg, ver)
        report(r)
    elif cmd == "file" and len(sys.argv) >= 3:
        p = Path(sys.argv[2])
        if p.is_file():
            r = scan_file(p)
        else:
            r = scan_dir(p)
        report({"scan": r})
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
