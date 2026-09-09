FROM python:3.12-slim
WORKDIR /app
ARG KUBECTL_VERSION=v1.36.4
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSLo /usr/local/bin/kubectl https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/arm64/kubectl \
    && curl -fsSLo /tmp/kubectl.sha256 https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/arm64/kubectl.sha256 \
    && echo "$(cat /tmp/kubectl.sha256)  /usr/local/bin/kubectl" | sha256sum --check \
    && chmod 755 /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/* /tmp/kubectl.sha256
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home agent
COPY agent.py .
USER 10001
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "agent.py"]
