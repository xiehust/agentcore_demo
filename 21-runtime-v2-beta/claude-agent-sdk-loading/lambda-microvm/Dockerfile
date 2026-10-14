# Lambda builds this Dockerfile itself from the S3 zip that deploy_microvm.py
# assembles (this file, ../requirements.txt, ../app/main.py, hooks.py), starts
# CMD, waits for /ready and snapshots memory + disk. AL2023 minimal is the base
# AWS recommends for snapshot compatibility; MicroVMs are ARM64 only.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CLAUDE_CODE_USE_BEDROCK=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    HOME=/tmp/agent-home \
    AGENT_WORKDIR=/tmp/agent-work

WORKDIR /app
COPY requirements.txt .
RUN pip3.12 install -r requirements.txt
COPY main.py hooks.py ./

# 8080: agent (proxy target), 9000: lifecycle hooks
EXPOSE 8080 9000
CMD ["python3.12", "hooks.py"]
