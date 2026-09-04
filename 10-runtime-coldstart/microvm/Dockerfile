# Ping-pong agent image for AWS Lambda MicroVMs, size-calibrated with
# incompressible /dev/urandom pad layers exactly like docker/Dockerfile.
#
# Lambda builds this Dockerfile itself (CreateMicrovmImage pulls a zip with
# this file + app.py from S3), starts CMD, waits for the /ready hook and then
# snapshots memory + disk. CreateMicrovmImage has no build-args parameter, so
# scripts/deploy_microvm.sh rewrites the PADn_MB defaults below per size
# variant before zipping. The same file builds locally with plain `docker
# build --build-arg` for the contract test (scripts/test_local_microvm.sh).
#
# Base: the AWS-provided AL2023 minimal container image, recommended for
# snapshot compatibility (AWS-patched OpenSSL). MicroVMs are ARM64 only.

FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3.12 python3.12-pip && dnf clean all

# Same runtime SDK as the AgentCore variant, so the served code path is identical.
RUN pip3.12 install --no-cache-dir bedrock-agentcore

ARG PAD1_MB=0
ARG PAD2_MB=0
ARG PAD3_MB=0
ARG PAD4_MB=0

RUN if [ "$PAD1_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_1.bin bs=1M count="$PAD1_MB"; fi
RUN if [ "$PAD2_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_2.bin bs=1M count="$PAD2_MB"; fi
RUN if [ "$PAD3_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_3.bin bs=1M count="$PAD3_MB"; fi
RUN if [ "$PAD4_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_4.bin bs=1M count="$PAD4_MB"; fi

COPY app.py /app/app.py
WORKDIR /app

# 8080: agent (BedrockAgentCoreApp) — default proxy target port.
# 9000: lifecycle hooks (must be EXPOSEd and bound to 0.0.0.0).
EXPOSE 8080 9000
CMD ["python3.12", "/app/app.py"]
