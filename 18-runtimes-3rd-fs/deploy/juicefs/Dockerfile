FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY deploy/juicefs/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts/09-install-s5cmd.py /tmp/install-s5cmd.py
RUN python /tmp/install-s5cmd.py && cp /build/s5cmd/s5cmd /usr/local/bin/s5cmd \
    && rm -rf /build/s5cmd /tmp/install-s5cmd.py
COPY demo/juicefs/s5_transfer.py demo/juicefs/agent.py demo/juicefs/workspace.py demo/juicefs/session_binding.py ./
COPY build/juicefs-fixtures/ /opt/fixtures/
EXPOSE 8080
CMD ["python", "agent.py"]
