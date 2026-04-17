#!/bin/bash
# setup_worker_vps.sh — Instala el Agent Worker en el VPS
# Correr como: bash /home/cukibot/cukinator-bot/scripts/setup_worker_vps.sh

set -e
echo "=== Setup Agent Worker ==="

REPO_PATH="/home/cukibot/cukinator-bot"
SERVICE_SRC="$REPO_PATH/scripts/cukinator-worker.service"
SERVICE_DST="/etc/systemd/system/cukinator-worker.service"

# 1. Pull latest
echo "[1] Actualizando repo..."
cd $REPO_PATH
git pull origin main

# 2. Instalar dependencias del worker
echo "[2] Instalando uvicorn + fastapi..."
pip3 install --break-system-packages uvicorn fastapi pydantic httpx 2>/dev/null || \
pip3 install uvicorn fastapi pydantic httpx

# 3. Verificar que anthropic y paramiko estén
pip3 install --break-system-packages anthropic paramiko 2>/dev/null || true

# 4. Instalar el service como root o con sudo
echo "[3] Instalando systemd service..."
if [ -w /etc/systemd/system ]; then
    cp "$SERVICE_SRC" "$SERVICE_DST"
elif command -v sudo &>/dev/null; then
    sudo cp "$SERVICE_SRC" "$SERVICE_DST"
else
    echo "ERROR: No puedo copiar a /etc/systemd/system. Corre como root o con sudo."
    exit 1
fi

# 5. Reload + enable + start
echo "[4] Habilitando y arrancando el worker..."
if command -v sudo &>/dev/null; then
    sudo systemctl daemon-reload
    sudo systemctl enable cukinator-worker
    sudo systemctl start cukinator-worker
else
    systemctl daemon-reload
    systemctl enable cukinator-worker
    systemctl start cukinator-worker
fi

# 6. Verificar
sleep 2
echo "[5] Estado del servicio:"
systemctl status cukinator-worker --no-pager -l | head -20

# 7. Probar endpoint
sleep 3
echo "[6] Probando /health:"
curl -s http://localhost:3335/health || echo "No responde aun, espera 5s mas..."
echo ""
echo "=== Setup completo ==="
echo "Logs: journalctl -u cukinator-worker -f"
