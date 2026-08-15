# Research: microVM Runtime command execution

Verified on 2026-08-14 UTC.

## Official contract

Sources:

- [Execute shell commands in AgentCore Runtime sessions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html)
- [InvokeAgentRuntimeCommand API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeAgentRuntimeCommand.html)
- [Stop a running session](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-stop-session.html)
- [Use isolated sessions for agents](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)

Findings:

1. `InvokeAgentRuntimeCommand` runs inside the same active Runtime session container, filesystem, and environment as `InvokeAgentRuntime`; it does not create a separate resource.
2. Command execution does not block agent invocations on the same session, so it can launch a detached monitor before the concurrency ramp.
3. Responses are HTTP/2 event streams containing `contentStart`, zero or more `contentDelta` values with stdout/stderr, and `contentStop` with `exitCode` and `status` (`COMPLETED` or `TIMED_OUT`). A non-zero exit code is a command failure, not an API exception.
4. Required caller permission is `bedrock-agentcore:InvokeAgentRuntimeCommand`. Cleanup requires `bedrock-agentcore:StopRuntimeSession`; user-aware invocation additionally requires `InvokeAgentRuntimeForUser`.
5. Commands are one-shot and stateless between calls, but files and detached background processes remain in the same active session. Persist state in the filesystem and include `cd`/environment setup in every command.
6. Request limits: command 1..65536 bytes, timeout 1..3600 seconds, session ID 33..256 characters. The API rate limit documented for command execution is 25 TPS.
7. `ResourceNotFoundException` means the session is absent/inactive. HTTP 409 `RetryableConflictException` during provisioning or teardown is transient and should use short bounded exponential backoff.
8. Agent runtimes created after 2026-03-17 support command execution automatically; older runtimes must be redeployed.
9. The command has full access to the session container filesystem and configured credentials. It belongs on a restricted diagnostic/orchestration role and must never execute untrusted user-provided shell text.
10. Default Runtime sessions use dedicated microVMs and an ephemeral session filesystem. State disappears after stop/idle/lifetime termination unless external persistence is configured.

## Local SDK/CLI check

- boto3: `1.42.59`
- botocore: `1.42.97`
- boto3 `bedrock-agentcore` client exposes `invoke_agent_runtime_command`, `invoke_agent_runtime`, and `stop_runtime_session`.
- The operation model accepts `body.command` and `body.timeout`; output contains `statusCode` and `stream`.
- Installed AWS CLI does **not** expose `aws bedrock-agentcore invoke-agent-runtime-command`; test scripts must use boto3 rather than assuming CLI support.

## Design implication

SSM-specific instance lookup, host path mapping, and EC2 resource sampling from demo 15 must not be copied. All deterministic checks and monitoring for demo 16 run through the supported command operation after a warmup invocation activates the exact shared session. The test must stop that session in `finally` to avoid idle compute charges.
