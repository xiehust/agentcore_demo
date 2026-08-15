FROM node:22.19.0-bookworm-slim AS node

RUN npm install -g @anthropic-ai/claude-code@2.1.232 \
    && npm cache clean --force

FROM ghcr.io/astral-sh/uv:0.8.13-python3.13-bookworm-slim

WORKDIR /app

# Copy the exact Node.js/Claude Code toolchain used by claude-agent-sdk.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app/ ./app/

RUN mkdir -p /tmp/agentcore-users && chmod 700 /tmp/agentcore-users
ENV DOCKER_CONTAINER=1 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 \
    USERS_ROOT=/tmp/agentcore-users

EXPOSE 8080
CMD [".venv/bin/python3", "app/server.py"]
