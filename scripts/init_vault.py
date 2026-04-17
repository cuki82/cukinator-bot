"""
scripts/init_vault.py — Inicializar vault con todas las credentials.
Correr UNA SOLA VEZ en el VPS o Railway.

Uso:
    source .env.local && python scripts/init_vault.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet

if not os.environ.get("MASTER_KEY"):
    key = Fernet.generate_key().decode()
    print(f"\n{'='*60}")
    print(f"MASTER_KEY generada — GUARDA ESTO EN RAILWAY/VPS ENV VARS:")
    print(f"\nMASTER_KEY={key}\n")
    print(f"{'='*60}\n")
    os.environ["MASTER_KEY"] = key

os.environ.setdefault("DB_PATH", "/data/memory.db")

from services.vault import init, set as vault_set

init()

SECRETS = [
    ("TELEGRAM_TOKEN",          "telegram",    "Bot token"),
    ("ANTHROPIC_KEY",           "anthropic",   "API key principal"),
    ("ANTHROPIC_API_KEY",       "anthropic",   "API key LiteLLM"),
    ("OPENAI_API_KEY",          "openai",      "API key"),
    ("DEEPSEEK_API_KEY",        "deepseek",    "API key"),
    ("GEMINI_API_KEY",          "google",      "Gemini API key"),
    ("GITHUB_TOKEN",            "github",      "Personal access token"),
    ("VPS_PRIVATE_KEY",         "vps",         "SSH private key"),
    ("VPS_HOST",                "vps",         "IP del VPS"),
    ("VPS_USER",                "vps",         "Usuario SSH"),
    ("VPS_PORT",                "vps",         "Puerto SSH"),
    ("GAS_URL",                 "google",      "Apps Script relay Gmail/Calendar"),
    ("GMAIL_OWNER",             "google",      "Email owner"),
    ("ELEVENLABS_KEY",          "elevenlabs",  "TTS API key"),
    ("ELEVENLABS_VOICE",        "elevenlabs",  "Voice ID COCOBASILE"),
    ("WEATHER_API_KEY",         "openweather", "Weather API"),
    ("LITELLM_MASTER_KEY",      "litellm",     "LiteLLM master key"),
    ("LITELLM_DB_URL",          "litellm",     "Postgres URL"),
    ("RAILWAY_PROJECT_ID",      "railway",     "Project ID"),
    ("RAILWAY_SERVICE_BOT_ID",  "railway",     "Bot service ID"),
    ("RAILWAY_SERVICE_MCP_ID",  "railway",     "MCP service ID"),
    ("MCP_URL",                 "mcp",         "MCP URL interno Railway"),
    ("MCP_URL_PUBLIC",          "mcp",         "MCP URL publico"),
    ("WORKER_SECRET",           "worker",      "Agent worker secret"),
    ("AGENT_WORKER_URL",        "worker",      "Agent worker URL"),
    ("TELEGRAM_OWNER_CHAT_ID",  "telegram",    "Owner chat ID"),
]

print("Cargando secrets al vault...\n")
loaded, skipped = 0, 0
for key, service, description in SECRETS:
    value = os.environ.get(key)
    if value:
        masked = vault_set(key, value, service=service, description=description)
        print(f"  ✓ {key:<35} {masked}")
        loaded += 1
    else:
        print(f"  ⚠ {key:<35} (no encontrada en env, skip)")
        skipped += 1

print(f"\n✓ {loaded} secrets cargados, {skipped} faltantes.")
if skipped:
    print("Para los faltantes: setealos en .env.local y corré el script de nuevo.")
