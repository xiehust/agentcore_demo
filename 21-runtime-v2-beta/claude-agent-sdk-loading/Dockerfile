# claude-agent-sdk's linux/aarch64 wheel bundles the Claude Code CLI binary,
# so the image needs no Node.js.
FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    HOME=/tmp/agent-home \
    AGENT_WORKDIR=/tmp/agent-work

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && mkdir -p /tmp/agent-home /tmp/agent-work
COPY app/ app/

EXPOSE 8080
CMD ["python", "app/main.py"]
