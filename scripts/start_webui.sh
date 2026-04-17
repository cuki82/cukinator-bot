#!/bin/bash
# Carga ANTHROPIC_KEY del vault y arranca Open WebUI + LiteLLM
set -e
cd /home/cukibot/cukinator-bot/docker

export ANTHROPIC_KEY=$(cd /home/cukibot/cukinator-bot && python3 -c "
import sys, os
sys.path.insert(0, '.')
os.environ['DB_PATH'] = '/home/cukibot/data/memory.db'
os.environ['MASTER_KEY'] = '3vPeFgOxPAe7MuS_tkCo_VphyYSniWynkCd7ViIxyRM='
from services.vault import get
print(get('ANTHROPIC_KEY') or get('ANTHROPIC_API_KEY') or '')
" 2>/dev/null)

if [ -z "$ANTHROPIC_KEY" ]; then
    echo "ERROR: No se pudo obtener ANTHROPIC_KEY del vault"
    exit 1
fi

echo "Iniciando Open WebUI + LiteLLM..."
docker compose -f docker-compose-webui.yml up -d
sleep 5
docker compose -f docker-compose-webui.yml ps
echo ""
echo "Open WebUI: http://31.97.151.119:8181"
echo "LiteLLM:    http://31.97.151.119:4000"
