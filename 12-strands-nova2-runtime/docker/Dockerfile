# AgentCore Runtime requires linux/arm64 images.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/sigv4_auth.py agent/agent.py /app/

# BedrockAgentCoreApp serves /invocations and /ping on 8080.
EXPOSE 8080
CMD ["python", "/app/agent.py"]
