# Simple Lark agent for Bedrock AgentCore Runtime (ARM64).
# Deps installed with uv for speed; runs the aiohttp HTTP(8080)+WS(18789) server.
FROM public.ecr.aws/docker/library/python:3.12-slim

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first for layer caching
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

COPY *.py /app/

EXPOSE 8080 18789

# AgentCore health check hits GET /ping on 8080
CMD ["python", "server.py"]
