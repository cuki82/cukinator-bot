"""
mcp_client.py — Cliente MCP interno para el bot de Telegram.

El bot usa este módulo para llamar al MCP Server en Railway
via el dominio interno (bypasea CDN/Fastly).

URL interna: http://aware-courage.railway.internal:8080
URL pública:  https://aware-courage-production-2769.up.railway.app (solo para dev)
"""

import os
import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)

# URL del MCP server — usa interno en Railway, público en dev
MCP_INTERNAL_URL = os.environ.get(
    "MCP_URL",
    "http://aware-courage.railway.internal:8080"
)
MCP_SSE_URL = f"{MCP_INTERNAL_URL}/sse"

_session = None
_session_lock = asyncio.Lock()


async def get_mcp_session():
    """Retorna una sesión MCP activa, creándola si no existe."""
    global _session
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        if _session is not None:
            return _session

        read, write = await sse_client(MCP_SSE_URL).__aenter__()
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        _session = session
        log.info(f"MCP session initialized: {MCP_SSE_URL}")
        return session
    except Exception as e:
        log.error(f"MCP session error: {e}")
        return None


async def mcp_call_tool(tool_name: str, args: dict) -> str:
    """
    Llama a un tool del MCP server.
    Retorna el resultado como string.
    """
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(MCP_SSE_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
                if result.content:
                    return "\n".join(
                        c.text for c in result.content
                        if hasattr(c, "text")
                    )
                return "(sin output)"
    except Exception as e:
        log.error(f"MCP tool {tool_name} error: {e}")
        return f"Error MCP: {e}"


async def mcp_list_tools() -> list:
    """Lista los tools disponibles en el MCP server."""
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        async with sse_client(MCP_SSE_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.inputSchema or {}
                    }
                    for t in result.tools
                ]
    except Exception as e:
        log.error(f"MCP list_tools error: {e}")
        return []


def call_mcp_tool_sync(tool_name: str, args: dict) -> str:
    """Versión síncrona para usar dentro del bot (que no es async)."""
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(mcp_call_tool(tool_name, args))
        loop.close()
        return result
    except Exception as e:
        return f"Error MCP: {e}"
