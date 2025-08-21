# FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.13-slim
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Install system dependencies including Node.js for playwright-mcp
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs zip \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
# Copy entire project (respecting .dockerignore)
COPY . .
RUN mkdir -p docs
RUN uv sync 

# Signal that this is running in Docker for host binding logic
ENV DOCKER_CONTAINER=1
ENV CLAUDE_CODE_USE_BEDROCK=1


EXPOSE 8080

# CMD [".venv/bin/opentelemetry-instrument", ".venv/bin/python3", "src/agentcore_runtime.py"]
CMD [".venv/bin/python3", "claude_code_agent.py"]

