"""Minimal ping-pong agent for AgentCore Runtime cold-start benchmarking.

Uses the real `bedrock-agentcore` Python SDK (BedrockAgentCoreApp), the same
server real runtime agents run on, so the benchmark mocks realistic agent
code. The SDK serves the AgentCore HTTP protocol on 0.0.0.0:8080:

- GET  /ping         -> {"status": "Healthy"} (provided by the SDK)
- POST /invocations  -> the entrypoint's return value as JSON

`proc_start_ts` is recorded at module import, so a client can decompose
container-boot -> first-request time from in-VM timestamps.
"""

import time

from bedrock_agentcore.runtime import BedrockAgentCoreApp

PROC_START_TS = time.time()

app = BedrockAgentCoreApp()


@app.entrypoint
def handler(payload):
    return {
        "message": "pong",
        "proc_start_ts": PROC_START_TS,
        "request_ts": time.time(),
        "echo": payload,
    }


if __name__ == "__main__":
    app.run()
