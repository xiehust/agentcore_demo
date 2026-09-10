FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134
ARG PAD_MIB=0
RUN if [ "$PAD_MIB" -gt 0 ]; then dd if=/dev/urandom of=/opt/image-pad.bin bs=1M count="$PAD_MIB"; fi
WORKDIR /app
COPY probe.py /app/probe.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 1000:1000
EXPOSE 8080
CMD ["python", "/app/probe.py"]
