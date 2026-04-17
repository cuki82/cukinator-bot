# CUKINATOR SYSTEM — CONTEXT FOR CLAUDE CODE

## What this is

A Telegram bot (`@CukinatorBot`) that acts as a personal AI assistant and remote control panel for a full AI stack running on a VPS. The bot is the single interface for everything.

---

## Architecture

```
User
  ↓ Telegram
@CukinatorBot (Python bot)
  ↓ Anthropic API (claude-opus-4-5)
  ↓ Tools (VPS, GitHub, Gmail, Calendar, Astro, Reservations)
  ↓
VPS Hostinger (31.97.151.119)
  ├── Open WebUI      :3000  (AI chat interface)
  ├── LiteLLM         :4000  (proxy with 134 models)
  ├── Scraper Meitre  :3334  (restaurant reservation checker)
  ├── MCP Server      :8080  (Railway service, 21 tools)
  └── OpenClaw        :18789 (Managed Agent — Operational Agent)

Railway (cloud deployment)
  ├── Cukinator Bot   (main bot process)
  └── MCP Server      (aware-courage-production-2769.up.railway.app)

GitHub
  └── cuki82/cukinator-bot (source of truth)
```

---

## Repository Structure

```
cukinator-bot/
├── bot.py                  # Telegram entry point, registers all handlers
├── bot_core.py             # Core: Claude API, all tools, ask_claude()
├── transcribe.py           # Audio transcription via Whisper (local)
├── swiss_engine.py         # Astrology engine (pyswisseph)
├── memory_store.py         # Persistent memory in SQLite
├── config_store.py         # Versioned config in SQLite
├── agent_ops.py            # Changelog, secrets, skills registry
├── reinsurance_kb.py       # Reinsurance knowledge base
│
├── handlers/
│   ├── message_handler.py  # Text/voice input handler
│   ├── callback_handler.py # Inline button callbacks
│   ├── gmail_handler.py    # /gmail command
│   ├── calendar_handler.py # /calendar command
│   ├── astro_handler.py    # /astro, /cartas commands
│   └── vps_handler.py      # /vps SSH commands
│
├── modules/
│   ├── ssh_executor.py     # SSH via paramiko (VPS_PRIVATE_KEY env var)
│   ├── reservas.py         # Restaurant availability via scraper
│   └── ...
│
├── mcp/
│   ├── mcp_server.py       # FastMCP server with 21 tools (SSE transport)
│   ├── Dockerfile          # Deployed on Railway
│   └── railway.toml
│
├── orchestrator.py         # Intent classification + Repo Lock (v1)
├── orchestrator_v2.py      # Orchestrator with Haiku decision + agent routing
├── multi_agent.py          # Agent teams (Operational, Research, Personal, Astrology, Reinsurance)
├── intent_router.py        # Keyword-based intent classifier (no API call)
├── mcp_client.py           # MCP SSE client for bot
├── agent_worker.py         # FastAPI worker for Claude Code tasks (VPS :3335)
├── worker_client.py        # HTTP client to call agent_worker
│
├── Dockerfile              # python:3.11, ffmpeg, whisper, all deps
├── railway.toml            # Railway deployment config
├── requirements.txt        # Python dependencies
└── bootstrap.sh            # Recovery script
```

---

## Environment Variables (Railway + VPS)

```bash
# Core
TELEGRAM_TOKEN=8744132762:AAGc-...
ANTHROPIC_KEY=sk-ant-api03-...
GAS_URL=https://script.google.com/macros/s/.../exec  # Gmail/Calendar relay

# GitHub
GITHUB_TOKEN=ghp_...

# VPS SSH
VPS_HOST=31.97.151.119
VPS_USER=cukibot
VPS_PRIVATE_KEY=-----BEGIN OPENSSH PRIVATE KEY-----...

# MCP
MCP_URL=http://aware-courage.railway.internal:8080

# Optional
DB_PATH=/data/memory.db
ELEVENLABS_KEY=sk_...
ELEVENLABS_VOICE=SHcpmnTftylBb6nJGEXY
WEATHER_API_KEY=6fc4ecceb823f299b4115a9f414c9fc7
```

---

## Core: ask_claude()

The main function in `bot_core.py`. Receives user text, runs Claude with all tools in a loop until `end_turn`, returns `(response_text, pdf_path, extra_files)`.

```python
def ask_claude(chat_id, user_text, user_name=None, allow_voice=False):
    # 1. Classify intent (keyword-based, no API call)
    intent = classify_intent(user_text)  # from multi_agent.py
    
    # 2. Check repo lock for coding tasks
    if intent == "coding_task": check_repo_lock()
    
    # 3. Claude API loop with tools
    while iteration < max_iterations:
        response = claude.messages.create(model, system, tools, messages)
        if stop_reason == "tool_use": execute_tools()
        else: return response_text
```

---

## Available Tools in Claude

### Conversational / Utility
- `get_time` — current time for any timezone
- `get_weather` — weather via OpenWeatherMap
- `search_web` — DuckDuckGo search
- `buscar_video` — YouTube search via yt-dlp
- `enviar_voz` — TTS via ElevenLabs
- `buscar_reserva` — restaurant availability via VPS scraper

### GitHub / Code
- `github_push` — push files to `bot-changes` branch (never main)
- `github_pr` — create Pull Request bot-changes → main

### VPS Operations
- `vps_exec` — SSH command execution
- `vps_leer_archivo` — read file via SFTP
- `vps_escribir_archivo` — write file via SFTP
- `vps_docker` — Docker container control (ps/restart/logs/stats)

### Gmail / Calendar (via Google Apps Script)
- `gmail_leer` — read emails
- `gmail_enviar` — send email
- `gmail_ver_email` — open specific email
- `gmail_descargar_adjunto` — download attachment
- `calendar_ver` — view events
- `calendar_crear` — create event

### Astrology
- `calcular_carta_natal` — natal chart calculation
- `astro_guardar_perfil` — save astrological profile
- `astro_ver_perfil` — view saved chart
- `astro_listar_perfiles` — list profiles
- `astro_eliminar_perfil` — delete profile

### Memory / Knowledge
- `memory_buscar` — search conversation history
- `memory_guardar_hecho` — save important fact
- `memory_persona` — person-specific memory
- `memory_stats` — memory statistics
- `config_guardar` / `config_leer` / `config_listar` — persistent config
- `ri_consultar` — search reinsurance knowledge base
- `ri_ingestar` — index document in KB
- `ri_listar_documentos` / `ri_stats` — KB management

### Agent Ops
- `agent_log` — log action to changelog
- `agent_guardar_secret` — store API key securely
- `agent_registrar_skill` — register new capability
- `agent_estado` — full system status

### MCP Layer
- `mcp_tool` — call any tool on MCP server (21 tools: vps_status, docker_ps, repo_status, search_memory, etc.)

---

## Repo Safety Rules

1. **Never push to main directly** — always `bot-changes`
2. **Always create PR** after push
3. **Protected files** (bot cannot modify these autonomously):
   - `bot.py`, `bot_core.py`, `orchestrator_v2.py`, `multi_agent.py`
   - `handlers/message_handler.py`, `handlers/callback_handler.py`
   - `Dockerfile`, `requirements.txt`
4. **Repo Lock** — prevents concurrent operations on same repo

---

## Multi-Agent Architecture (Implemented, partially active)

```
Intent Router (keyword-based, instant)
    ├── conversational    → Claude direct
    ├── coding_task       → Operational Agent
    ├── research_task     → Research Agent  
    ├── personal_task     → Personal Agent
    ├── astrology_task    → Astrology Agent
    ├── reinsurance_task  → Reinsurance Agent
    └── mixed_task        → parallel agents
```

Files:
- `orchestrator_v2.py` — Haiku decides, routes to specialist agents
- `multi_agent.py` — Agent implementations with system prompts + tool subsets
- `intent_router.py` — Keyword classifier (no API latency)

**Current status:** Intent classification active, agent routing disabled in production (latency issues being resolved).

---

## Operational Agent — PENDING

**The key missing piece.** Needs a real executor for:
- Read/edit repo files
- Run tests/validation
- Git commit/push
- Open PRs

**Planned architecture:**
```
Bot → HTTP → OpenClaw (VPS :18789)
                ↓
            Managed Agent
                ↓
            bash/git/filesystem tools
```

Note: Anthropic products cannot connect to OpenClaw directly. The connection is Bot (Python HTTP client) → OpenClaw API.

---

## Database Schema (SQLite at /data/memory.db)

```sql
messages          -- conversation history with sessions
sessions          -- conversation groupings
memory_index      -- facts, topics, entities
person_memory     -- per-person context
configurations    -- versioned key-value configs
agent_changelog   -- operation history
agent_secrets     -- masked API keys
agent_skills      -- registered capabilities
perfiles_astro    -- astrological profiles
reinsurance_*     -- KB tables (documents, chunks, concepts, qa, summaries)
```

---

## Key Behaviors

- **Identity:** Bot responds as "Cukinator" (never "Claude")
- **Owner:** chat_id `8626420783` has access to all tools
- **TTS:** ElevenLabs voice "COCOBASILE" for audio responses
- **STT:** Local Whisper `base` model for voice transcription
- **Language:** Rioplatense Spanish, casual professional tone
- **Format:** Structured output for technical responses, direct for casual
- **Buttons:** `[BOTONES: Op1 | Op2 | Op3]` syntax triggers inline keyboard
- **PDF:** Natal charts can be exported as PDF

---

## VPS Services Detail

| Service | Container | Port | Purpose |
|---|---|---|---|
| Open WebUI | open-webui-3000 | 3000 | AI chat UI |
| LiteLLM | litellm (docker-compose) | 4000 | Model proxy |
| LiteLLM DB | litellm_db | 5432 | PostgreSQL |
| Prometheus | litellm_prometheus_1 | 9090 | Metrics |
| Scraper | scraper-reservas-scraper-1 | 3334 | Meitre scraper |
| OpenClaw | openclaw | 18789 | Managed Agent |
| Traefik | traefik-o69g-traefik-1 | 80/443 | Reverse proxy |

---

## What Works Today

- ✅ Telegram bot receiving and responding to messages
- ✅ Claude with 30+ tools
- ✅ VPS control via SSH (docker, files, services)
- ✅ Gmail and Calendar via Google Apps Script
- ✅ Natal chart calculation and PDF export
- ✅ Restaurant availability checking (Meitre)
- ✅ Voice in/out (Whisper + ElevenLabs)
- ✅ GitHub push/PR workflow (bot-changes only)
- ✅ Repo Lock for concurrent operation protection
- ✅ Persistent memory and knowledge base
- ✅ MCP Server with 21 tools (Railway)
- ✅ Inline buttons in responses
- ✅ PDF and photo processing

## What's Next

- ⬜ Operational Agent via OpenClaw (main missing piece)
- ⬜ Full multi-agent routing without latency
- ⬜ Move bot from Railway to VPS (reduce latency)
- ⬜ Railway API integration (deploy/logs/restart)
- ⬜ Complete testing suite
