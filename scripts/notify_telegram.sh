#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────
# notify_telegram.sh — Notifica al owner por Telegram cuando un service falla.
# Triggereado por systemd OnFailure= en cukinator.service y compañeros.
#
# Uso: notify_telegram.sh <service_name>
#   Lee TELEGRAM_TOKEN y OWNER_TELEGRAM_ID del vault via get_env.py (o env).
#   Si fallan, intenta systemctl show para obtener TELEGRAM_TOKEN.
# ─────────────────────────────────────────────────────────────────────────

SERVICE="${1:-unknown.service}"
TS=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

# Intentar leer las credenciales desde el environment del service principal
if [ -z "$TELEGRAM_TOKEN" ]; then
    TELEGRAM_TOKEN=$(systemctl --user show cukinator -p Environment --value | tr ' ' '\n' | grep '^TELEGRAM_TOKEN=' | cut -d= -f2-)
fi
if [ -z "$OWNER_TELEGRAM_ID" ]; then
    OWNER_TELEGRAM_ID=$(systemctl --user show cukinator -p Environment --value | tr ' ' '\n' | grep '^OWNER_TELEGRAM_ID=' | cut -d= -f2-)
fi

# Fallbacks
OWNER_TELEGRAM_ID="${OWNER_TELEGRAM_ID:-8626420783}"

if [ -z "$TELEGRAM_TOKEN" ]; then
    echo "[notify] sin TELEGRAM_TOKEN; no puedo notificar" >&2
    exit 1
fi

# Últimas 10 líneas del journal del service para contexto
JOURNAL=$(journalctl --user -u "$SERVICE" -n 10 --no-pager 2>/dev/null | tail -8 | sed 's/^/    /')

MSG=$(cat <<EOF
🚨 *Cukinator: service falló*

📌 service: \`$SERVICE\`
🕒 timestamp: $TS

\`\`\`
$JOURNAL
\`\`\`
EOF
)

curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -d "chat_id=${OWNER_TELEGRAM_ID}" \
    -d "parse_mode=Markdown" \
    --data-urlencode "text=${MSG}" > /dev/null

echo "[notify] aviso enviado para $SERVICE"
