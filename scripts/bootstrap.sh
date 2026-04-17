#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — Cukinator Bot Recovery Script
# Uso: bash bootstrap.sh TELEGRAM_TOKEN ANTHROPIC_KEY GAS_URL
# O con variables de entorno ya seteadas: bash bootstrap.sh
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/cuki82/cukinator-bot"
INSTALL_DIR="/workspace/cukinator-bot"
LOG_FILE="/workspace/bot.log"
PID_FILE="/workspace/bot.pid"
DATA_DIR="/data"

# ── Colores ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
info() { echo -e "${CYAN}[..] $*${NC}"; }
warn() { echo -e "${YELLOW}[!!] $*${NC}"; }
fail() { echo -e "${RED}[XX] $*${NC}"; exit 1; }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════╗"
echo "║        CUKINATOR BOT — BOOTSTRAP RECOVERY        ║"
echo "╚══════════════════════════════════════════════════╝"
echo -e "${NC}"

# ── Step 1: Credenciales ──────────────────────────────────────────────────────
info "Step 1/6: Verificando credenciales..."

# Aceptar por argumento posicional o variable de entorno
TELEGRAM_TOKEN="${TELEGRAM_TOKEN:-${1:-}}"
ANTHROPIC_KEY="${ANTHROPIC_KEY:-${2:-}}"
GAS_URL="${GAS_URL:-${3:-}}"

[[ -z "$TELEGRAM_TOKEN" ]] && fail "TELEGRAM_TOKEN no proporcionado. Uso: bash bootstrap.sh TOKEN ANTHROPIC_KEY GAS_URL"
[[ -z "$ANTHROPIC_KEY"  ]] && fail "ANTHROPIC_KEY no proporcionado."
[[ -z "$GAS_URL"        ]] && fail "GAS_URL no proporcionado."

# Validar token contra Telegram API
info "Validando token con Telegram..."
TG_RESP=$(curl -s "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMe")
if echo "$TG_RESP" | grep -q '"ok":true'; then
    BOT_NAME=$(echo "$TG_RESP" | grep -oP '(?<="username":")[^"]+')
    ok "Token válido → @${BOT_NAME}"
else
    fail "Token inválido o error de red: $TG_RESP"
fi

# ── Step 2: Repositorio ───────────────────────────────────────────────────────
info "Step 2/6: Sincronizando repositorio..."

if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "Repo existe, haciendo pull..."
    git -C "$INSTALL_DIR" pull --ff-only origin main && ok "Repo actualizado" || warn "Pull falló, usando versión local"
else
    info "Clonando repo..."
    git clone "$REPO_URL" "$INSTALL_DIR" && ok "Repo clonado"
fi

cd "$INSTALL_DIR"

# ── Step 3: Sistema ───────────────────────────────────────────────────────────
info "Step 3/6: Verificando dependencias del sistema..."

MISSING_SYS=()
command -v ffmpeg    &>/dev/null || MISSING_SYS+=("ffmpeg")
command -v espeak-ng &>/dev/null || MISSING_SYS+=("espeak-ng")
fc-list | grep -qi "DejaVu" || MISSING_SYS+=("fonts-dejavu-mono")

if [[ ${#MISSING_SYS[@]} -gt 0 ]]; then
    info "Instalando: ${MISSING_SYS[*]}"
    apt-get update -qq && apt-get install -y -qq "${MISSING_SYS[@]}" gcc g++ 2>&1 | tail -2
    ok "Dependencias del sistema instaladas"
else
    ok "Dependencias del sistema OK"
fi

# ── Step 4: Python packages ───────────────────────────────────────────────────
info "Step 4/6: Instalando paquetes Python..."

# Instalar en orden correcto (numpy primero por pyswisseph/whisper)
PIP="pip install -q --no-warn-script-location"

install_if_missing() {
    local pkg="$1"
    local import_name="${2:-$1}"
    if ! python -c "import ${import_name}" &>/dev/null 2>&1; then
        info "  Instalando $pkg..."
        $PIP "$pkg" 2>&1 | tail -1
    fi
}

# Core numérico (debe ir primero)
install_if_missing "numpy==1.26.4"           "numpy"
install_if_missing "pyswisseph==2.10.3.2"    "swisseph"
install_if_missing "openai-whisper"          "whisper"

# Telegram
install_if_missing "python-telegram-bot[job-queue]==22.7" "telegram"

# IA
install_if_missing "anthropic==0.94.0"       "anthropic"

# Utilidades
install_if_missing "ddgs==9.13.0"            "ddgs"
install_if_missing "fpdf2"                   "fpdf"
install_if_missing "geopy"                   "geopy"
install_if_missing "timezonefinder"          "timezonefinder"
install_if_missing "httpx==0.27.2"           "httpx"
install_if_missing "requests"                "requests"
install_if_missing "pytz"                    "pytz"
install_if_missing "paramiko"                "paramiko"
install_if_missing "yt-dlp"                  "yt_dlp"

ok "Paquetes Python OK"

# ── Step 5: Entorno ───────────────────────────────────────────────────────────
info "Step 5/6: Configurando entorno..."

mkdir -p "$DATA_DIR"

# Escribir .env (por si algún módulo lo necesita)
cat > "$INSTALL_DIR/.env" <<EOF
TELEGRAM_TOKEN=${TELEGRAM_TOKEN}
ANTHROPIC_KEY=${ANTHROPIC_KEY}
GAS_URL=${GAS_URL}
DB_PATH=${DATA_DIR}/memory.db
EOF

ok "Entorno configurado"

# ── Step 6: Arrancar bot ──────────────────────────────────────────────────────
info "Step 6/6: Arrancando bot..."

# Matar instancia anterior si existe
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        warn "Matando instancia anterior (PID $OLD_PID)..."
        kill "$OLD_PID" && sleep 2
    fi
    rm -f "$PID_FILE"
fi

# También buscar por nombre de proceso
pkill -f "python.*bot.py" 2>/dev/null || true
sleep 1

# Arrancar
nohup env \
    TELEGRAM_TOKEN="$TELEGRAM_TOKEN" \
    ANTHROPIC_KEY="$ANTHROPIC_KEY" \
    GAS_URL="$GAS_URL" \
    DB_PATH="${DATA_DIR}/memory.db" \
    python "$INSTALL_DIR/bot.py" > "$LOG_FILE" 2>&1 &

BOT_PID=$!
echo $BOT_PID > "$PID_FILE"
info "Bot iniciado con PID $BOT_PID, esperando confirmación..."

# Esperar hasta 15s que arranque
for i in {1..15}; do
    sleep 1
    if ! kill -0 $BOT_PID 2>/dev/null; then
        echo ""
        fail "El bot murió al arrancar. Últimas líneas del log:\n$(tail -20 $LOG_FILE)"
    fi
    if grep -q "Bot en línea\|Application started" "$LOG_FILE" 2>/dev/null; then
        break
    fi
done

# Verificación final
if kill -0 $BOT_PID 2>/dev/null && grep -q "Application started" "$LOG_FILE" 2>/dev/null; then
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "╔══════════════════════════════════════════════════╗"
    echo "║           ✅  BOT EN LÍNEA Y FUNCIONANDO          ║"
    echo "╚══════════════════════════════════════════════════╝"
    echo -e "${NC}"
    ok "@${BOT_NAME} corriendo — PID $BOT_PID"
    ok "Logs: tail -f $LOG_FILE"
    ok "Parar: kill $BOT_PID"
else
    echo ""
    warn "El bot arrancó pero no confirmó 'Application started'. Verificar logs:"
    tail -10 "$LOG_FILE"
fi
