#!/usr/bin/env bash
# cuki-remote.sh
# Mantiene una sesión de Claude Code corriendo en tmux para que el Remote
# Control desde la app iOS pueda conectarse SIEMPRE al VPS, sin que el user
# tenga que SSH-ear y tipear `claude` manualmente.
#
# Diseño: el service systemd llama a este script con Type=oneshot. El script
# crea (idempotente) una tmux session detached llamada "cuki-remote" con
# claude adentro. La session sobrevive aunque cierres el SSH.
#
# Para entrar a verla: tmux attach -t cuki-remote
# Para tirarla:       tmux kill-session -t cuki-remote
set -euo pipefail

SESSION="cuki-remote"
WORKDIR="${WORKDIR:-$HOME/cukinator-bot}"
PREFIX="${PREFIX:-VPS}"

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' ya existe — nada para hacer"
    exit 0
fi

cd "$WORKDIR"
tmux new-session -d -s "$SESSION" -n claude \
    "claude --remote-control-session-name-prefix \"$PREFIX\""
echo "tmux session '$SESSION' creada — claude corriendo dentro"
echo "→ attach: tmux attach -t $SESSION"
