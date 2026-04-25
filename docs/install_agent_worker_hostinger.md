# Agent Worker en Hostinger VPS — instalación desde cero

Guía específica para desplegar el **Agent Worker** (FastAPI + Codex planner + Claude Code CLI) en un **VPS de Hostinger**, incluyendo compra, DNS, SSH, dev environment en VS Code / code-server y deploy final.

**Qué termina corriendo**: un servicio systemd que recibe `POST /task/stream` con un texto, lo planifica con Codex (gpt-5-codex), spawneea Claude Code CLI sobre un repo de GitHub, commitea cambios a una branch `bot-changes` y devuelve un stream SSE con el progreso en vivo.

No cubre dominio específico (astro/reaseguros/etc) — solo el Worker puro.

---

## 0. Qué necesitás antes de empezar

- Cuenta en **Hostinger** (https://www.hostinger.com) con método de pago
- Cuenta en **GitHub** (https://github.com)
- API key de **Anthropic** con acceso a Claude Code → https://console.anthropic.com (plan con crédito)
- API key de **OpenAI** para Codex → https://platform.openai.com
- (Opcional) un dominio — podés comprarlo en Hostinger mismo o traer uno externo

**Tiempo estimado**: 60–90 min la primera vez (incluyendo propagación DNS si usás dominio).

---

## 1. Comprar el VPS en Hostinger

### 1.1 Elegir plan

Entrá a https://www.hostinger.com/vps → elegí un plan. **Mínimo recomendado**: **KVM 2** (2 vCPU / 8 GB RAM / 100 GB NVMe). KVM 1 (1 vCPU / 4 GB) también funciona pero Claude Code CLI con streams largos se nota lento.

**Evitá** los planes "Cloud Startup" o similar (esos son compartidos). Querés **KVM** (virtualización full, acceso root real).

### 1.2 Elegir sistema operativo

Durante el checkout Hostinger te pregunta el OS inicial. Elegí:
- **Ubuntu 24.04 LTS** (recomendado) — o **Ubuntu 22.04 LTS** como alternativa.
- NO elijas "Hostinger VPS OS" ni templates con paneles preinstalados (cPanel, CyberPanel, Coolify, etc.) — agregan overhead que no necesitás.

### 1.3 Esperar el provisioning

Toma 2–5 minutos. Vas a recibir un email con la IP pública del VPS y la password inicial de root. Guardá ambos.

### 1.4 Panel de Hostinger — ubicar tu VPS

Una vez listo:
1. Entrá a https://hpanel.hostinger.com
2. Menú lateral → **VPS** → tu servidor aparece con la IP.
3. Click en el VPS → te abre el dashboard con info del servidor, botones de "Restart", "Rescue mode", **Browser terminal**, "Manage" (nameservers, PTR, firewall).

**Browser terminal**: útil como backup si tu SSH local falla. Se abre desde el mismo panel.

---

## 2. Primera conexión SSH + seguridad inicial

### 2.1 Conectarse como root

Desde tu máquina local:

```bash
ssh root@TU_IP_VPS
# password inicial que te mandó Hostinger por email
```

Si te salta el warning de "REMOTE HOST IDENTIFICATION HAS CHANGED" (porque reutilizaste una IP), borrá la entrada vieja:

```bash
ssh-keygen -R TU_IP_VPS
```

### 2.2 Cambiar la password de root

```bash
passwd
# poné una random fuerte (pegala desde tu password manager)
```

### 2.3 Actualizar todo

```bash
apt update && apt upgrade -y
apt install -y curl wget git build-essential python3 python3-pip python3-venv unzip jq ufw
```

### 2.4 Crear usuario no-root (`agent`)

No vas a correr el Worker como root.

```bash
adduser agent
usermod -aG sudo agent
# Passwordless sudo para 'agent' (queda cómodo para operar sin tipear password):
echo "agent ALL=(ALL) NOPASSWD:ALL" | tee /etc/sudoers.d/agent
chmod 0440 /etc/sudoers.d/agent
```

### 2.5 SSH con llave (sin password)

En tu **máquina local** (Windows/Mac/Linux):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/agent_vps -C "agent@hostinger"
```

Copiá la pública al VPS (todavía conectado como root):

```bash
# desde tu local:
ssh-copy-id -i ~/.ssh/agent_vps.pub agent@TU_IP_VPS
# te pide la password de 'agent' (la que pusiste con adduser)
```

Probá el login con llave:

```bash
ssh -i ~/.ssh/agent_vps agent@TU_IP_VPS
```

Si entra sin pedir password, bien. Guardá la ruta de la llave — te va a servir.

### 2.6 Configurar `~/.ssh/config` local (opcional pero recomendado)

Tu archivo `~/.ssh/config` (crealo si no existe):

```
Host agent-vps
    HostName TU_IP_VPS
    User agent
    IdentityFile ~/.ssh/agent_vps
    ServerAliveInterval 60
    ServerAliveCountMax 3
```

Ahora entrás con `ssh agent-vps`.

### 2.7 Deshabilitar login SSH con password (hardening)

**Solo después** de confirmar que el login con llave funciona. Desde el VPS como `agent`:

```bash
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sudo systemctl restart sshd
```

A partir de acá, nadie entra sin llave SSH. Si perdés tu llave, tenés el **Browser terminal** de Hostinger como rescate.

### 2.8 Firewall UFW

```bash
sudo ufw allow 22/tcp              # SSH
sudo ufw allow 80/tcp              # HTTP (certbot)
sudo ufw allow 443/tcp             # HTTPS (si exponés code-server)
sudo ufw --force enable
sudo ufw status verbose
```

Los puertos del Worker (3335) no se exponen al público — quedan solo en `127.0.0.1`.

### 2.9 Habilitar systemd user lingering

Permite que los servicios del usuario `agent` arranquen al boot y sobrevivan logout:

```bash
sudo loginctl enable-linger agent
```

---

## 3. (Opcional) DNS en Hostinger — subdominio para code-server

Salteá esta sección si no vas a exponer code-server vía HTTPS.

### 3.1 Si el dominio está en Hostinger

1. Panel Hostinger → **Domains** → tu dominio → **DNS/Nameservers**
2. Add new record:
   | Type | Name | Points to | TTL |
   |-|-|-|-|
   | A | `code` | TU_IP_VPS | 3600 |

   (Si querés usar `worker.tudominio.com` en vez, cambiá el Name).

3. Esperá 5–60 min hasta que `dig code.tudominio.com +short` en tu máquina local devuelva la IP del VPS.

### 3.2 Si el dominio está en otro registrador

Apuntá los nameservers a Hostinger o simplemente agregá el mismo A record desde el panel de tu registrador. Lo importante es que el DNS resuelva a tu IP.

### 3.3 PTR record (opcional)

Si querés que el VPS tenga reverse DNS limpio (útil si más tarde montás email):

- Panel Hostinger → tu VPS → **Manage** → **PTR record** → poné `code.tudominio.com`.

Para el Worker no hace falta.

---

## 4. Node.js + Claude Code CLI

Como `agent` (SSH conectado):

### 4.1 nvm + Node 20

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 20 && nvm alias default 20
node --version    # v20.x
```

### 4.2 Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### 4.3 API key de Anthropic

Opción A — config persistente:

```bash
claude config set api_key sk-ant-api03-xxxxxxxxxxxxxxxxxxxx
```

Opción B — env var (lo dejamos en la unit systemd más abajo, sección 7). Recomendada para no pinchar el config.

---

## 5. GitHub — repo que Claude va a editar

### 5.1 Crear el repo

En https://github.com/new creá un repo (público o privado). Ejemplo: `agent-target`. Este es el repo sobre el que Claude hace `Read`/`Edit`/`Bash`.

### 5.2 Token de GitHub

https://github.com/settings/tokens → **Generate new token (classic)** con scope `repo` completo (o un **fine-grained token** limitado solo a ese repo, más seguro). Guardalo — se usa como `GITHUB_TOKEN` en el env.

### 5.3 Clonar en el VPS

```bash
cd ~
git clone https://TU_GITHUB_TOKEN@github.com/TU_USUARIO/agent-target.git
cd agent-target

git config user.email "agent@ejemplo.com"
git config user.name "AgentWorker"

# Branch donde el Worker pushea — NUNCA main
git checkout -b bot-changes
git push -u origin bot-changes
git checkout main
```

---

## 6. Agent Worker — código + dependencias Python

### 6.1 Obtener el source del Worker

```bash
cd ~
mkdir agent-worker && cd agent-worker
# clona tu fork con workers/agent_worker.py, o copiá el archivo directo
git clone https://github.com/TU_USUARIO/agent-worker-src.git .
# estructura mínima necesaria:
#   workers/agent_worker.py
#   requirements.txt (o instalás a mano como en 6.2)
```

### 6.2 Virtualenv + deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install fastapi "uvicorn[standard]" anthropic openai requests pydantic
# Si tenés requirements.txt mejor: pip install -r requirements.txt
```

### 6.3 Env file con secrets

```bash
mkdir -p ~/.config/agent-worker
cat > ~/.config/agent-worker/env <<'EOF'
# === API keys ===
ANTHROPIC_API_KEY=sk-ant-api03-XXXXXXXXXXXX
ANTHROPIC_KEY=sk-ant-api03-XXXXXXXXXXXX
OPENAI_API_KEY=sk-proj-XXXXXXXXXXXX

# === GitHub (para git_commit_push) ===
GITHUB_TOKEN=ghp_XXXXXXXXXXXX

# === Repo que Claude edita ===
REPO_PATH=/home/agent/agent-target

# === Branch destino (NUNCA main — se mergea via PR) ===
BOT_BRANCH=bot-changes

# === Secret del Worker: todo cliente tiene que mandarlo ===
WORKER_SECRET=pone-aca-un-random-de-32-chars

# === Modelo Codex ===
CODEX_MODEL=gpt-5-codex
EOF
chmod 600 ~/.config/agent-worker/env
```

Si tu cuenta OpenAI no tiene acceso a `gpt-5-codex`, cambiá a `gpt-4o` o `gpt-4.1`.

---

## 7. systemd — Worker como servicio de usuario

### 7.1 Unit file

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

Claves:
- `WantedBy=default.target` (no `multi-user.target` — esto es unit de usuario).
- `--host 127.0.0.1` → solo localhost. Si el cliente corre en otro host, poné `0.0.0.0` pero mantené el firewall UFW cerrado en 3335 y usá `WORKER_SECRET` siempre.

### 7.2 Habilitar + arrancar

```bash
systemctl --user daemon-reload
systemctl --user enable --now agent-worker
systemctl --user status agent-worker --no-pager
```

### 7.3 Verificación end-to-end

```bash
# Health:
curl -s http://127.0.0.1:3335/health

# Stream real:
curl -sS -N -X POST http://127.0.0.1:3335/task/stream \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: pone-aca-un-random-de-32-chars" \
  -d '{"task_id":"probe","user_text":"listame los .md del repo","chat_id":0}'
```

Deberías ver el stream SSE `status → plan → claude (tool_use Read/Bash) → summary → done`. Si ves eso, está listo.

Ver logs en vivo:

```bash
journalctl --user -u agent-worker -f
```

---

## 8. code-server (VS Code en browser) — opcional pero cómodo

Deja el VPS editable desde cualquier navegador.

### 8.1 Instalar

```bash
curl -fsSL https://code-server.dev/install.sh | sh
```

### 8.2 Config inicial

```bash
mkdir -p ~/.config/code-server
cat > ~/.config/code-server/config.yaml <<EOF
bind-addr: 127.0.0.1:8443
auth: password
password: PONE-UNA-PASSWORD-FUERTE-ACA
cert: false
EOF
```

**Importante**: dejalo siempre en `127.0.0.1` + `auth: password`. La exposición pública la hace nginx con HTTPS.

### 8.3 systemd unit

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

### 8.4 Extensiones

```bash
code-server --install-extension anthropic.claude-code
code-server --install-extension ms-python.python
code-server --install-extension ms-python.vscode-pylance
code-server --install-extension redhat.vscode-yaml
```

### 8.5 Workspace multi-root

```bash
mkdir -p ~/workspaces
cat > ~/workspaces/agent.code-workspace <<'EOF'
{
  "folders": [
    { "path": "/home/agent/agent-worker", "name": "Agent Worker (source)" },
    { "path": "/home/agent/agent-target", "name": "Target repo (lo que Claude edita)" }
  ],
  "settings": {
    "claude-code.autoResumeLastSession": true,
    "claude-code.defaultSessionBehavior": "resume"
  }
}
EOF
```

### 8.6 nginx + HTTPS con certbot (si tenés el subdominio `code.tudominio.com`)

Saltá esto si preferís acceder por túnel SSH (`ssh -L 8443:127.0.0.1:8443 agent-vps` y abrir http://localhost:8443 en el browser local).

```bash
sudo apt install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/code.tudominio.com >/dev/null <<'EOF'
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
    client_max_body_size 50M;
  }
}
EOF

sudo ln -sf /etc/nginx/sites-available/code.tudominio.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d code.tudominio.com \
  --agree-tos --email tu@email.com --non-interactive --redirect
```

Ahora `https://code.tudominio.com` abre VS Code pidiendo la password que pusiste en `code-server/config.yaml`.

---

## 9. VS Code en tu máquina local (cliente)

Para editar el código del Worker/target sin abrir el browser:

1. Instalar **VS Code** desde https://code.visualstudio.com
2. Extensiones:
   - **Remote - SSH** (`ms-vscode-remote.remote-ssh`)
   - **Claude Code** (`anthropic.claude-code`)
   - **Python**, **Pylance**
3. `F1` → "Remote-SSH: Connect to Host" → `agent-vps` (ya lo tenés en `~/.ssh/config`)
4. Abrí la carpeta `/home/agent/agent-worker` desde el VS Code remoto.

Todas tus ediciones ocurren **en el VPS**, pero con la experiencia local (IntelliSense, debugger, etc.).

---

## 10. Cliente llamando al Worker

Desde cualquier script / app que pueda alcanzar el Worker:

```bash
curl -sS -N -X POST http://127.0.0.1:3335/task/stream \
  -H "Content-Type: application/json" \
  -H "X-Worker-Secret: pone-aca-un-random-de-32-chars" \
  -H "Accept: text/event-stream" \
  -d '{
    "task_id": "req-001",
    "user_text": "Agregá un endpoint GET /ping al repo que devuelva pong en JSON",
    "chat_id": 0
  }'
```

Respuesta: stream SSE con eventos `status → plan → claude (tool_use, tool_result, text) → git (commit + push a bot-changes) → summary → done`.

Para consumirlo desde Node/TypeScript, Python, etc., es un SSE estándar (lee `data: {json}\n\n`).

---

## 11. Checklist final

- [ ] `ssh agent-vps` entra sin password
- [ ] `systemctl --user is-active agent-worker` → `active`
- [ ] `curl http://127.0.0.1:3335/health` → 200
- [ ] `claude --version` imprime versión
- [ ] `node --version` ≥ 20
- [ ] `cd ~/agent-target && git status` en `main`, limpio, remote `origin` conectado
- [ ] `~/.config/agent-worker/env` tiene `chmod 600` y las 3 keys + GITHUB_TOKEN + WORKER_SECRET
- [ ] Un POST de prueba termina con `{type: "done", status: "ok"}` y hay un commit nuevo en `origin/bot-changes`
- [ ] UFW activo (`sudo ufw status` → `Status: active`)
- [ ] SSH con password deshabilitado
- [ ] (Si code-server) `https://code.tudominio.com` pide password y entra

---

## 12. Troubleshooting rápido

| Síntoma | Causa | Fix |
|-|-|-|
| SSH pide password después de copiar la llave | Permisos del `authorized_keys` | En VPS: `chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && chown -R agent:agent ~/.ssh` |
| `systemctl --user` no arranca al boot | Falta lingering | `sudo loginctl enable-linger agent` |
| Worker 500 `ANTHROPIC_KEY missing` | EnvironmentFile no se leyó | Verificar path + que el archivo tenga `chmod 600` y esté sin espacios raros |
| Claude CLI pide login en cada tarea | Falta `ANTHROPIC_API_KEY` como env en la systemd unit | Está en el env file ya (sección 6.3) — reiniciar Worker |
| `git push` falla con 403 | Token expirado o sin scope | Regenerar en GitHub → actualizar env → restart Worker |
| Codex devuelve "model not found" | Sin acceso a `gpt-5-codex` | Cambiar `CODEX_MODEL=gpt-4o` en env |
| Certbot "DNS problem" | DNS todavía no propagó | Esperar + `dig code.tudominio.com +short` hasta ver la IP |
| nginx 502 / WebSocket cae | Falta `proxy_read_timeout` / `Upgrade` headers | Revisar `/etc/nginx/sites-available/code.tudominio.com` contra la sección 8.6 |
| Browser terminal de Hostinger se corta | Timeout del panel | Es normal, usalo solo para rescates — la operación diaria por SSH |

---

## 13. Seguridad — resumen

- API keys solo en `~/.config/agent-worker/env` con `chmod 600`. Nunca commiteadas.
- `WORKER_SECRET` random de 32+ chars. Todos los clientes deben enviarlo como header `X-Worker-Secret`.
- Worker bindeado a `127.0.0.1:3335` — si expones a red, añadí IP whitelisting en UFW.
- code-server siempre con `auth: password`, sobre HTTPS (certbot o túnel SSH).
- GitHub token fine-grained: scope solo al repo target.
- SSH con llave obligatorio, password auth deshabilitada.
- Rotar API keys cada 90 días (calendarizalo).

---

## 14. Operación diaria

| Tarea | Comando |
|-|-|
| Ver logs Worker en vivo | `journalctl --user -u agent-worker -f` |
| Reiniciar Worker | `systemctl --user restart agent-worker` |
| Reiniciar code-server | `systemctl --user restart code-server` |
| Upgradear Claude Code CLI | `npm i -g @anthropic-ai/claude-code@latest && systemctl --user restart agent-worker` |
| Actualizar el Worker (tu source) | `cd ~/agent-worker && git pull && systemctl --user restart agent-worker` |
| Ver uso de disco / RAM | `df -h && free -h` |
| Ver procesos del Worker | `systemctl --user status agent-worker` |
| Rotar una API key | Editar `~/.config/agent-worker/env` + `systemctl --user restart agent-worker` |

---

## 15. Costos aproximados (referencia)

| Concepto | ~USD/mes |
|-|-|
| Hostinger KVM 2 (2 vCPU / 8 GB / 100 GB) | 7–10 |
| Anthropic API (uso normal para dev, Claude Code CLI varias veces/día) | 15–40 |
| OpenAI API (Codex planner + summarizer) | 2–8 |
| Dominio (opcional, anual) | ~10/año |
| **Total mensual** | **~25–60** |

Los costos de API escalan con uso. Si vas a estresarlo, reservá ~100+ USD/mes de buffer para Anthropic.

---

Fin. Si algo no cierra, el orden correcto de debug es: SSH → systemd → env vars → API keys → red.
