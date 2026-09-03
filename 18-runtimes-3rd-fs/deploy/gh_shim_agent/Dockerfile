# Strands agent + GitHub CLI + gh shim, for AgentCore Runtime (microVM, linux/arm64).
# Build context = 18-runtimes-3rd-fs/ (the shim and token resolver come from demo/secretless_auth/).
FROM --platform=linux/arm64 python:3.12-slim-bookworm

ARG GH_VERSION=2.83.2
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz" \
       | tar -xz -C /tmp \
    && mv "/tmp/gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/bin/gh \
    && rm -rf /tmp/gh_* /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/gh_shim_agent/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The shim: /opt/shim/gh shadows /usr/bin/gh via PATH order. git_askpass.py is the token resolver it calls.
COPY demo/secretless_auth/gh_wrapper.sh /opt/shim/gh
COPY demo/secretless_auth/git_askpass.py /opt/shim/git_askpass.py
COPY deploy/gh_shim_agent/print_token_env.sh /opt/shim/print_token_env.sh
RUN chmod 755 /opt/shim/gh /opt/shim/git_askpass.py /opt/shim/print_token_env.sh

COPY deploy/gh_shim_agent/agent.py ./agent.py

ENV PATH="/opt/shim:${PATH}" \
    GH_REAL=/usr/bin/gh \
    GH_ALLOWED_SUBCOMMANDS="api pr issue repo search" \
    TOKEN_SOURCE=identity \
    IDENTITY_FLOW=api_key \
    GIT_TERMINAL_PROMPT=0

EXPOSE 8080
CMD ["opentelemetry-instrument", "python3", "agent.py"]
