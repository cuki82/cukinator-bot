# Cukinator MCP Server

Un solo servicio Railway con 4 servidores MCP montados en paths distintos.

## Endpoints

| Servidor | Path | Descripción |
|---|---|---|
| ops | `/ops/mcp` | VPS operations (docker, ssh, servicios) |
| github | `/github/mcp` | Repo control (branches, files, PRs) |
| memory | `/memory/mcp` | Memoria y historial de conversaciones |
| knowledge | `/knowledge/mcp` | Knowledge base (reaseguros, documentos) |

## Variables de entorno en Railway

```
GITHUB_TOKEN=ghp_...
GITHUB_REPO=cuki82/cukinator-bot
DB_PATH=/data/memory.db   (con volume montado)
VPS_HOST=31.97.151.119
VPS_USER=cukibot
VPS_PRIVATE_KEY=-----BEGIN OPENSSH...
MCP_SECRET=tu-token-secreto   (opcional)
PORT=3350
```

## Cómo lo usa el bot

En `bot_core.py`, las llamadas a Claude incluyen:

```python
client.beta.messages.create(
    model="claude-opus-4-5",
    messages=[...],
    mcp_servers=[
        {
            "type": "url",
            "url": f"{MCP_URL}/ops/mcp",
            "name": "ops",
        },
        {
            "type": "url", 
            "url": f"{MCP_URL}/github/mcp",
            "name": "github",
        },
        {
            "type": "url",
            "url": f"{MCP_URL}/memory/mcp",
            "name": "memory",
        },
        {
            "type": "url",
            "url": f"{MCP_URL}/knowledge/mcp",
            "name": "knowledge",
        },
    ],
    extra_headers={"anthropic-beta": "mcp-client-2025-04-04"}
)
```

## Deploy

1. Crear nuevo servicio en Railway
2. Conectar este repo (o subir los archivos)
3. Agregar las variables de entorno
4. Railway usa el Dockerfile automáticamente
5. Agregar volume en `/data` para persistir la DB
