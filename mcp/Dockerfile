FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir mcp fastmcp starlette uvicorn paramiko httpx

COPY mcp_server.py .

CMD ["sh", "-c", "uvicorn mcp_server:app --host 0.0.0.0 --port ${PORT:-3350}"]
