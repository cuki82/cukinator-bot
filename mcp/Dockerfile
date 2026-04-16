FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir mcp fastmcp starlette uvicorn paramiko httpx

COPY mcp_server.py .

CMD ["uvicorn", "mcp_server:app", "--host", "0.0.0.0", "--port", "3350"]
