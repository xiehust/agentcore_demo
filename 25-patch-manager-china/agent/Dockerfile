# build context = project root (infra/06_runtime.py): docker build -f agent/Dockerfile .
FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# memory library, installed as a normal package (decoupled from the agent code)
COPY agentmem /tmp/agentmem
RUN pip install --no-cache-dir --no-deps /tmp/agentmem && rm -rf /tmp/agentmem
COPY agent/*.py ./
RUN useradd -m -u 1000 agent
USER agent
EXPOSE 8080
CMD ["python", "app.py"]
