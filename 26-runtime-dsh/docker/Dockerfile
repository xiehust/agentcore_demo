# DeepSeek Harness (DSH) headless agent behind an AgentCore Runtime HTTP wrapper.
# Build context is the demo root: docker build -f docker/Dockerfile .
FROM --platform=linux/arm64 public.ecr.aws/docker/library/node:24-bookworm-slim

ARG DSH_VERSION=0.2.2-rc.1
ARG IMAGE_TAG=dev

# git and ripgrep are what DSH's file and shell tools expect to find.
# python3 runs the page pinner (app/pinner.py) used on platform V2.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git ripgrep python3 \
    && rm -rf /var/lib/apt/lists/*

# DSH is installed as shipped on npm: the point is to measure the stock agent.
WORKDIR /opt/dsh
RUN npm init -y >/dev/null \
    && npm install --omit=dev --no-audit --no-fund "@deepseek-ai/dsh@${DSH_VERSION}" \
    && npm cache clean --force

COPY app/server.mjs /opt/dsh/server.mjs
RUN mkdir -p /workspace && chown node:node /workspace

USER node
ENV DSH_HOME=/home/node/.dsh \
    DSH_WORKDIR=/workspace \
    IMAGE_TAG=${IMAGE_TAG} \
    NODE_ENV=production \
    PORT=8080 \
    HOST=0.0.0.0

# Materialize the headless profile at build time so no session pays for it.
RUN /opt/dsh/node_modules/.bin/dsh headless --help >/dev/null

# Last, so regenerating the manifest only rebuilds this layer. The manifest is
# produced by scripts/gen_pin_manifest.sh from this same image.
COPY app/pinner.py app/pin-manifest.txt /opt/dsh/

# No port is published by this image; AgentCore reaches 8080 inside the microVM.
EXPOSE 8080
CMD ["node", "/opt/dsh/server.mjs"]
