FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Node.js 22 for the Claude Code CLI that claude-agent-sdk drives.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code

COPY pyproject.toml ./
RUN uv sync

COPY app/ ./app/

# Fallback workspace root for local runs without the capacity-provider volume;
# on AgentCore the runtime mounts the EBS volume at /mnt/scratch.
RUN mkdir -p /mnt/scratch/users

ENV DOCKER_CONTAINER=1
ENV CLAUDE_CODE_USE_BEDROCK=1
ENV CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
ENV USERS_ROOT=/mnt/scratch/users

EXPOSE 8080

CMD [".venv/bin/python3", "app/server.py"]
