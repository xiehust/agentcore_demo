# Ping-pong agent image with size calibration via incompressible pad layers.
#
# Pad data comes from /dev/urandom (incompressible), so the ECR compressed
# size ~= uncompressed size and pull cost matches the nominal image size.
# Each PADn_MB build arg produces its own layer (chunks of <=500MB), mimicking
# realistic multi-layer images uniformly across size variants.
# NOTE: never write-then-delete pad data — deleting in a later layer does not
# shrink the image, it only hides the file.

FROM python:3.12-slim

# Real runtime SDK — the agent runs on BedrockAgentCoreApp like production
# agents do. Installed before the pad layers so the measured base size
# (and therefore the computed pad) includes it.
RUN pip install --no-cache-dir bedrock-agentcore

ARG PAD1_MB=0
ARG PAD2_MB=0
ARG PAD3_MB=0
ARG PAD4_MB=0

RUN if [ "$PAD1_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_1.bin bs=1M count="$PAD1_MB"; fi
RUN if [ "$PAD2_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_2.bin bs=1M count="$PAD2_MB"; fi
RUN if [ "$PAD3_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_3.bin bs=1M count="$PAD3_MB"; fi
RUN if [ "$PAD4_MB" -gt 0 ]; then dd if=/dev/urandom of=/opt/pad_4.bin bs=1M count="$PAD4_MB"; fi

COPY app/main.py /app/main.py

EXPOSE 8080
CMD ["python", "/app/main.py"]
