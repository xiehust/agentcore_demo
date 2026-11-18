# Build from 28-nitro-ecs. Stock DSH, native ARM64, no baked credentials.
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24-bookworm-slim
ARG DSH_VERSION=0.2.2-rc.1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/dsh
RUN npm init -y >/dev/null && npm install --omit=dev --no-audit --no-fund "@deepseek-ai/dsh@${DSH_VERSION}" \
    && npm cache clean --force
COPY app/server.mjs /opt/dsh/server.mjs
RUN mkdir -p /workspace && chown node:node /workspace
# Bootstrap owns fresh ECS anonymous volumes, then drops to UID/GID 1000.
USER root
ENV HOST=127.0.0.1 PORT=8080 DSH_WORKDIR=/workspace DSH_HOME=/workspace/.dsh \
    HOME=/workspace AWS_EC2_METADATA_DISABLED=true NODE_ENV=production
EXPOSE 8080
CMD ["node", "/opt/dsh/server.mjs"]
