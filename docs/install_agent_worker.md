# Agent Worker — instalación desde cero

Guía paso a paso para dejar corriendo el **Agent Worker** (FastAPI + Codex planner + Claude Code CLI) en un VPS Linux limpio, más el entorno de desarrollo en **VS Code / code-server**.

**Qué hace el Agent Worker**: recibe un `user_text` por HTTP/SSE, le pasa el texto a Codex (`gpt-5-codex` vía OpenAI) para convertirlo en un prompt técnico, después lanza **Claude Code CLI** con `--output-format stream-json`, va reenviando al cliente cada `tool_use` / texto / resultado, commitea lo que Claude cambió en `git` y cierra con un resumen de Codex en criollo.

**No cubre** nada de dominio específico (astro, reaseguros, etc.). Solo el Worker puro.

---

## 0. Requisitos previos del operador

- Cuenta en un proveedor de VPS (Hostinger, DigitalOcean, Hetzner, AWS Lightsail — cualquiera que te dé root SSH). Recomendado: **4 vCPU / 8 GB RAM / 80 GB disco** mínimo si vas a correr Claude Code CLI con streams largos.
- Cuenta de **GitHub** con un repo donde Claude va a trabajar (puede ser tuyo o de una org).
- **API key de Anthropic** con acceso a Claude Code (el CLI la consume): https://console.anthropic.com
- **API key de OpenAI** para Codex planner + summarizer: https://platform.openai.com
- Un dominio opcional si querés exponer code-server por HTTPS (con SSL via certbot).

---

## 1. VPS — preparación de base

Asumo **Ubuntu 22.04 / 24.04** o **Debian 12**. Si usás otra distro ajustá los `apt` a tu gestor.

### 1.1 Conectarse y actualizar

```bash
ssh root@TU_IP_VPS
apt update && apt upgrade -y
```

### 1.2 Crear un usuario no-root para el worker

No corras el Worker como root. Creá un usuario dedicado (ejemplo: `agent`):

```bash
adduser agent
usermod -aG sudo agent
# Passwordless sudo opcional (cuidado si es VPS compartido):
# echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent
```

### 1.3 Configurar acceso SSH con llave

En tu máquina local:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agent_ed25519 -C "agent@worker"
ssh-copy-id -i ~/.ssh/agent_ed25519.pub agent@TU_IP_VPS
```

Probá que entra sin password:

```bash
ssh -i ~/.ssh/agent_ed25519 agent@TU_IP_VPS
```

### 1.4 Habilitar systemd user lingering

Esto permite que los servicios del usuario `agent` arranquen al boot y sobrevivan logout:

```bash
sudo loginctl enable-linger agent
```

### 1.5 Firewall mínimo (UFW)

```bash
sudo ufw allow 22/tcp              # SSH
sudo ufw allow 80/tcp              # HTTP (para certbot)
sudo ufw allow 443/tcp             # HTTPS (si vas a exponer code-server)
sudo ufw --force enable
```

Los puertos internos del Worker (3335) los dejamos solo en `127.0.0.1` — no se exponen al público.

---

## 2. Dependencias del sistema

Como `agent`:

```bash
ssh -i ~/.ssh/agent_ed25519 agent@TU_IP_VPS
sudo apt install -y git build-essential python3 python3-pip python3-venv curl wget unzip jq
```

### 2.1 Node.js 20 (para Claude Code CLI)

Node se instala con `nvm` (evita root):

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 20 && nvm alias default 20
node --version     # debería imprimir v20.x
```

### 2.2 Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version   # verifica instalación
```

Login con API key (una sola vez):

```bash
claude config set api_key sk-ant-api03-xxxxxxxx
# o configurar via env var ANTHROPIC_API_KEY (lo hacemos en la systemd unit más abajo)
```

---

## 3. GitHub — repo + token + SSH

### 3.1 Crear un repo en GitHub

En https://github.com/new creá un repo (público o privado). Ejemplo: `agent-worker-target`. Este es el repo donde **Claude va a editar código**.

### 3.2 Token de GitHub (para push automatizado)

En https://github.com/settings/tokens → **Generate new token (classic)** con scopes `repo` (full control). Guardalo — se usa una vez.

### 3.3 Clonar el repo en la VPS

```bash
cd ~
git clone https://TU_GITHUB_TOKEN@github.com/TU_USUARIO/agent-worker-target.git
cd agent-worker-target

# Configurar identidad del worker para los commits que va a hacer
git config user.email "agent@ejemplo.com"
git config user.name "AgentWorker"

# Crear la branch donde el Worker va a pushear (NO pushea a main nunca)
git checkout -b bot-changes
git push -u origin bot-changes
git checkout main
```

### 3.4 (Opcional) SSH key en lugar de token

Si preferís SSH en vez de HTTPS+token:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_agent -C "agent@github"
cat ~/.ssh/github_agent.pub  # pegar en https://github.com/settings/keys
# Cambiar remote a ssh:
git remote set-url origin git@github.com:TU_USUARIO/agent-worker-target.git
```

---

## 4. Agent Worker — instalar

### 4.1 Obtener el código

El Worker vive en `workers/agent_worker.py`. Clonalo donde tengas la fuente (o copialo directamente):

```bash
cd ~
mkdir agent-worker && cd agent-worker

# Opción A: clonar tu repo con el source
git clone https://github.com/TU_USUARIO/agent-worker.git .

# Opción B: copiar los archivos mínimos a mano
# Necesitás al menos: workers/agent_worker.py + requirements.txt
```

### 4.2 Virtualenv + dependencias

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install fastapi "uvicorn[standard]" anthropic openai requests pydantic
# Si hay un requirements.txt, mejor:
# pip install -r requirements.txt
```

### 4.3 Directorio de trabajo + env file

El Worker necesita saber dónde está el repo que Claude va a tocar y las API keys:

```bash
mkdir -p ~/.config/agent-worker
cat > ~/.config/agent-worker/env <<'EOF'
# === API keys ===
ANTHROPIC_API_KEY=sk-ant-api03-xxxxxxxxxxxxxxxxxxxx
ANTHROPIC_KEY=sk-ant-api03-xxxxxxxxxxxxxxxxxxxx
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxx

# === GitHub (para git_commit_push) ===
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxx

# === Repo que Claude va a editar ===
REPO_PATH=/home/agent/agent-worker-target

# === Branch destino (NUNCA main — los merges se hacen por PR) ===
BOT_BRANCH=bot-changes

# === Secret compartido con el cliente que llama al Worker ===
WORKER_SECRET=cambia-esto-por-algo-random

# === Modelo Codex (planner + summarizer) ===
CODEX_MODEL=gpt-5-codex

# === Puerto local del Worker ===
WORKER_PORT=3335
EOF
chmod 600 ~/.config/agent-worker/env
```

---

## 5. systemd — dejar el Worker corriendo como servicio de usuario

### 5.1 Unit file

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/agent-worker.service <<'EOF'
[Unit]
Description=Agent Worker (FastAPI :3335)
After=network.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/agent/agent-worker
EnvironmentFile=/home/agent/.config/agent-worker/env
ExecStart=/home/agent/agent-worker/.venv/bin/python -m uvicorn workers.agent_worker:app --host 127.0.0.1 --port 3335 --log-level info
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
```

**Notas importantes:**
- `WantedBy=default.target` (NO `multi-user.target`) porque esto es unit de *usuario*. Si usás `multi-user.target` systemd tira un warning y el servicio no arranca al boot.
- `--host 127.0.0.1` deja el Worker solo accesible local — si el cliente corre en otro container/máquina, poné `0.0.0.0` y protegé con firewall + `WORKER_SECRET`.

### 5.2 Habilitar y arrancar

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-worker
systemctl --user status agent-worker --no-pager
# logs en vivo:
journalctl --user -u agent-worker -f
```

### 5.3 Verificación

```bash
curl -s http://127.0.0.1:3335/health
# → {"ok": true, ...} o similar según tu health endpoint

curl -sS -N -X POST http://127.0.0.1:3335/task/stream \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: cambia-esto-por-algo-random" \
  -d '{"task_id":"probe","user_text":"listame los archivos del repo","chat_id":0}'
# → stream de eventos SSE: status, plan, claude, summary, done.
```

Si ves `status → plan → claude (con tool_use Read/Bash) → summary → done`, el Worker funciona end-to-end.

---

## 6. Code-server (VS Code en el browser) — opcional

Si querés editar el código del Worker y del repo-target desde el browser (útil si trabajás desde múltiples máquinas):

### 6.1 Instalar

```bash
curl -fsSL https://code-server.dev/install.sh | sh
```

### 6.2 Config mínima

```bash
mkdir -p ~/.config/code-server
cat > ~/.config/code-server/config.yaml <<'EOF'
bind-addr: 127.0.0.1:8443
auth: none                        # si lo exponés público, usá 'password' + password: XXX
cert: false
EOF
```

**Ojo**: `auth: none` solo es seguro si code-server queda en `127.0.0.1` y se accede vía túnel SSH o nginx con auth. **No** lo dejes en `0.0.0.0` sin password.

### 6.3 systemd unit

```bash
cat > ~/.config/systemd/user/code-server.service <<'EOF'
[Unit]
Description=code-server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/code-server
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now code-server
```

### 6.4 Extensiones recomendadas

Desde el terminal del VPS:

```bash
code-server --install-extension anthropic.claude-code
code-server --install-extension ms-python.python
code-server --install-extension ms-python.vscode-pylance
code-server --install-extension redhat.vscode-yaml
```

La extensión `anthropic.claude-code` te da el chat de Claude Code adentro de VS Code (usa la misma sesión que el CLI).

### 6.5 Workspace multi-root

Si querés tener el source del Worker + el repo-target visibles al mismo tiempo:

```bash
mkdir -p ~/workspaces
cat > ~/workspaces/agent.code-workspace <<'EOF'
{
  "folders": [
    { "path": "/home/agent/agent-worker", "name": "Agent Worker (source)" },
    { "path": "/home/agent/agent-worker-target", "name": "Target repo (lo que Claude edita)" }
  ],
  "settings": {
    "claude-code.autoResumeLastSession": true,
    "claude-code.defaultSessionBehavior": "resume"
  }
}
EOF
```

Abrilo con `code-server ~/workspaces/agent.code-workspace` o desde la UI.

### 6.6 Exponer code-server vía HTTPS (opcional, con nginx + certbot)

Si querés acceder desde `https://code.tudominio.com` en lugar de túnel SSH:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/code.tudominio.com <<'EOF'
server {
  listen 80;
  server_name code.tudominio.com;

  location / {
    proxy_pass http://127.0.0.1:8443;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 86400;
  }
}
EOF

sudo ln -sf /etc/nginx/sites-available/code.tudominio.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d code.tudominio.com --agree-tos --email tu@email.com --non-interactive --redirect
```

Ahora también **cambiá** `~/.config/code-server/config.yaml` a `auth: password` y poné un `password: XXXX` fuerte — sin eso queda expuesto el editor al mundo.

```bash
systemctl --user restart code-server
```

---

## 7. Codex (OpenAI) — verificación

El Worker usa Codex en dos lugares: planner (`codex_plan`) y summarizer (`codex_summarize`). Ya pusiste la `OPENAI_API_KEY` en el env file.

Probá que funciona:

```bash
curl -sS https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5-codex","messages":[{"role":"user","content":"decime 2+2"}]}' \
  | jq -r '.choices[0].message.content'
# → "4"
```

Si tu cuenta no tiene acceso a `gpt-5-codex`, cambiá `CODEX_MODEL` en el env file a `gpt-4o` o `gpt-4.1` — el Worker los soporta igual.

---

## 8. Claude Code CLI — prueba end-to-end

Parado en el repo-target:

```bash
cd ~/agent-worker-target
claude -p "listame los archivos .md del repo" --output-format stream-json --verbose
```

Deberías ver una ráfaga de JSON con `tool_use` (Bash, Read), `tool_result`, `text`, y al final un `result` con la respuesta final. Si esto funciona standalone, entonces `agent_worker.py` también va a funcionar — él solo hace exactamente esto con el texto que le mandás.

---

## 9. VS Code en máquina local (cliente)

Si además querés tener VS Code **local** en tu máquina (no solo el browser):

1. Descargar desde https://code.visualstudio.com
2. Instalar extensiones:
   - **Claude Code** (`anthropic.claude-code`)
   - **Remote - SSH** (`ms-vscode-remote.remote-ssh`) — para conectarte al VPS como si estuvieras local
   - **Python**, **Pylance**
3. Conexión SSH:
   - `F1` → "Remote-SSH: Connect to Host" → `agent@TU_IP_VPS`
   - Usa la llave `~/.ssh/agent_ed25519` (definila en `~/.ssh/config`).

`~/.ssh/config` local:

```
Host agent-vps
    HostName TU_IP_VPS
    User agent
    IdentityFile ~/.ssh/agent_ed25519
    ServerAliveInterval 60
```

Ahora `F1 → Remote-SSH → agent-vps` y ya estás editando en el VPS con todas las tools locales.

---

## 10. Cliente del Worker (cómo llamarlo)

Desde cualquier lado (curl / fetch / python / node) que pueda alcanzar `http://TU_IP_VPS:3335` (si está en `0.0.0.0`) o que tenga un túnel SSH al `127.0.0.1:3335`:

```bash
curl -sS -N -X POST http://127.0.0.1:3335/task/stream \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: cambia-esto-por-algo-random" \
  -H "Accept: text/event-stream" \
  -d '{
    "task_id": "req-001",
    "user_text": "Agregá un endpoint GET /ping al repo que devuelva pong",
    "chat_id": 0
  }'
```

Vas a recibir un stream SSE con eventos del tipo `{type: "status"|"plan"|"claude"|"git"|"summary"|"done"|"error", data: ...}`.

---

## 11. Checklist de verificación

Al terminar deberías poder responder "sí" a todo esto:

- [ ] `systemctl --user is-active agent-worker` → `active`
- [ ] `curl http://127.0.0.1:3335/health` responde 200
- [ ] `claude --version` imprime versión
- [ ] `node --version` ≥ 20
- [ ] `cd ~/agent-worker-target && git status` limpio, en branch `main`, con remote `origin` conectado
- [ ] `~/.config/agent-worker/env` tiene `chmod 600` y contiene las 3 API keys + GITHUB_TOKEN + WORKER_SECRET
- [ ] Un `POST /task/stream` de prueba termina en un `{type: "done", data: {status: "ok"}}` y el Worker pushea un commit a `bot-changes`
- [ ] (Opcional) `https://code.tudominio.com` muestra VS Code con auth

---

## 12. Troubleshooting rápido

| Síntoma | Causa probable | Fix |
|-|-|-|
| `systemctl --user` no encuentra la unit | Falta `loginctl enable-linger agent` | `sudo loginctl enable-linger agent` y reloguear |
| Worker 500 al arrancar: `ANTHROPIC_KEY missing` | `EnvironmentFile` no se leyó | Verificar path + `chmod` del env file |
| Claude CLI pide login en cada tarea | Falta `ANTHROPIC_API_KEY` como env | Agregar al env file y re-exportar |
| `git push` falla con 403 | Token expirado o scope insuficiente | Regenerar token con scope `repo` completo |
| Codex devuelve "model not found" | Tu cuenta no tiene `gpt-5-codex` | Cambiar `CODEX_MODEL=gpt-4o` |
| Stream se corta a los ~60s | Timeout de nginx/proxy | Subir `proxy_read_timeout` a 600s |
| `WantedBy=multi-user.target` warning | Typo en la unit de usuario | Cambiar a `WantedBy=default.target` + `daemon-reload` |

---

## 13. Seguridad — checklist final

- API keys solo en `~/.config/agent-worker/env` con `chmod 600` — nunca commiteadas al repo.
- `WORKER_SECRET` random de 32+ chars. Todo cliente debe mandarlo en el header `X-Worker-Secret`.
- Worker en `127.0.0.1:3335` salvo que explícitamente necesites acceso de red — en ese caso, firewall que filtre.
- code-server: `auth: password` si se expone público, o solo accesible por túnel SSH.
- El user `agent` no necesita sudo passwordless — solo si vas a correr `systemctl` a nivel system (no hace falta para user units).
- GitHub token como fine-grained personal access token con scope solo al repo-target, no a todo tu GitHub.
- Rotar las API keys (Anthropic/OpenAI/GitHub) cada 90 días o si se filtraron.

---

## 14. Operación diaria

- **Ver logs del Worker**: `journalctl --user -u agent-worker -f`
- **Reiniciar Worker después de deploy**: `systemctl --user restart agent-worker`
- **Actualizar repo-target**: Claude lo hace solo — vos solo mergés los PRs de `bot-changes` a `main` cuando quieras liberar cambios.
- **Rotar API keys**: editar `~/.config/agent-worker/env` + `systemctl --user restart agent-worker`.
- **Upgradear Claude Code CLI**: `npm install -g @anthropic-ai/claude-code@latest && systemctl --user restart agent-worker`.

Fin.
