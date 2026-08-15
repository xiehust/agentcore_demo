# AgentCore Runtime Command Contract

## Scenario: deterministic work inside an active Runtime session

### 1. Scope / Trigger

Use this contract when project code calls `InvokeAgentRuntimeCommand` to inspect, monitor, build, test, or verify files inside an active Amazon Bedrock AgentCore Runtime session. It applies to default microVM Runtime sessions and any other Runtime mode that officially supports the operation.

The operation replaces unsupported host access; it is not an SSM substitute that exposes the host. It runs in the same **session container, filesystem, and environment** as `InvokeAgentRuntime`. Never depend on EC2 instance lookup, Auto Scaling groups, host volume paths, containerd, or other managed-host implementation details for a microVM test.

### 2. Signatures

Boto3 command invocation:

```python
response = client.invoke_agent_runtime_command(
    agentRuntimeArn=runtime_arn,
    runtimeSessionId=session_id,
    qualifier="DEFAULT",  # optional
    contentType="application/json",
    accept="application/vnd.amazon.eventstream",
    body={"command": command, "timeout": timeout_seconds},
)
```

Session cleanup:

```python
response = client.stop_runtime_session(
    agentRuntimeArn=runtime_arn,
    runtimeSessionId=session_id,
    qualifier="DEFAULT",  # optional
    clientToken=idempotency_token,
)
```

Required caller actions are `bedrock-agentcore:InvokeAgentRuntimeCommand` and `bedrock-agentcore:StopRuntimeSession`. User-aware agent calls also require `InvokeAgentRuntimeForUser`.

### 3. Contracts

Request:

| Field | Contract |
|---|---|
| `agentRuntimeArn` | Non-empty Runtime ARN. Use the same ARN as the warmup invocation. |
| `runtimeSessionId` | Same active session as the agent invocation; 33..256 characters. |
| `body.command` | Code-owned command string only; UTF-8 size 1..65536 bytes. It is **not** implicitly evaluated as shell source. Never concatenate user/prompt values into it. |
| `body.timeout` | 1..3600 seconds. |
| `accept` | `application/vnd.amazon.eventstream`. |

Response stream:

```text
stream
  -> chunk.contentStart                  exactly once, first
  -> chunk.contentDelta{stdout,stderr}   zero or more
  -> chunk.contentStop{exitCode,status}  exactly once, last
```

Success requires every condition below:

1. HTTP/API status is 200.
2. Returned `runtimeSessionId` equals the requested ID.
3. There is exactly one correctly ordered start/stop sequence.
4. No event-stream exception, unknown member, malformed chunk, duplicate, or iteration error occurred.
5. `contentStop.status == "COMPLETED"`.
6. `contentStop.exitCode == 0`.

`InvokeAgentRuntimeCommand` does not implicitly run shell syntax. Pipes, redirects, quoting, and `&&` must not be assumed to work merely because they appear in `body.command`. To execute a code-owned script, transport the script as encoded data and invoke an explicit interpreter, for example:

```python
encoded = base64.b64encode(script.encode()).decode("ascii")
pipeline = f"printf '%s' '{encoded}' | base64 -d | /bin/bash"
command = f"/bin/bash -c {json.dumps(pipeline)}"
```

The extra `/bin/bash -c` is required: the 2026-08-14 live smoke showed that sending the base64 pipeline directly treated it as ordinary command arguments and printed encoded text instead of executing the pipeline. Callers must never pass untrusted text into this wrapper.

Commands are one-shot processes. Environment changes do not carry to later commands; filesystem changes and intentionally detached processes can persist while the session remains active. Encode `cd` and environment setup in every command.

For test clients, default cleanup is `StopRuntimeSession` from `finally`. Retaining a session must require an explicit debug flag and warn that compute can remain billable.

### 4. Related Claude Agent SDK hook contract

Python function hooks in `ClaudeAgentOptions.hooks` require the SDK's bidirectional control protocol. In `claude-agent-sdk==0.1.1`, use `ClaudeSDKClient`, not the one-shot `query()` helper, when a Python `PreToolUse` hook is part of the security or correctness contract:

```python
async with ClaudeSDKClient(options=options) as client:
    await client.query(prompt)
    async for message in client.receive_response():
        ...
```

A live integration probe must force a guarded tool call and require both `denied_count > 0` and absence of protected content. “No leak” alone is insufficient because the model may decline the request without invoking the hook. The 2026-08-14 live test observed `denied_count=0` with one-shot `query()` and `denied_count=1` for both relative and absolute path probes after switching to `ClaudeSDKClient`.

### 5. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Session has not been activated or is gone | Treat `ResourceNotFoundException` as terminal; warm up the exact session first. |
| Session is provisioning or tearing down | Retry only HTTP 409 / `RetryableConflictException` with short bounded exponential backoff. |
| API returns 403 | Fail immediately and report missing command permission. |
| API returns 429/500 | Do not silently convert to command success; apply only an explicitly designed API retry policy. |
| Stream contains an exception event | Fail even if another chunk looks successful. |
| `contentStop` is missing | Fail as incomplete output. |
| `status == TIMED_OUT` | Fail as timeout. |
| `exitCode != 0` | Fail as command failure; this is not an API exception. |
| Returned session ID differs | Fail; evidence came from the wrong/unconfirmed session. |
| Stop returns non-200 or a different session ID | Record cleanup failure and return a non-success operational result. |
| Monitoring/verification command fails after agent work | Preserve the agent checkpoint; mark monitoring/verification unavailable, never verified-success. |
| Guard probe returns no leak but `denied_count == 0` | Fail the hook integration contract; model behavior is not proof that the hook ran. |

### 6. Good / Base / Bad Cases

- **Good:** warm up a new session, start a run-scoped `/proc` monitor using a static base64-encoded script through explicit `/bin/bash -c`, invoke users in that same session, read/verify outputs through command events, atomically save evidence, then stop the session in `finally`.
- **Base:** run a single static executable command, consume all stream events, and check status plus exit code before using stdout.
- **Bad:** assume HTTP 200 means `false` or a timed-out command succeeded; ignore `contentStop`; send a shell pipeline without an explicit shell; run a command against a made-up inactive session; interpolate a workspace path or prompt directly into shell; use SSM/EC2 host internals; rely on one-shot `query()` for Python hooks; or let monitor cleanup prevent session cleanup.

### 7. Tests Required

Unit tests must assert:

1. Valid ordered `contentStart` / delta / `contentStop` streams fold stdout and stderr.
2. Missing/duplicate/out-of-order/unknown/malformed events and stream iteration failures are rejected.
3. HTTP failure, mismatched session ID, timeout status, and non-zero exit code are rejected independently.
4. Only 409 conflicts retry, retries are bounded, and other exceptions pass through.
5. Shell scripts are encoded and the generated command contains explicit `/bin/bash -c`; the encoded script can be recovered exactly.
6. Monitoring CSV parse errors and absent windows remain explicit failures.
7. Agent evidence is checkpointed before deterministic verification.
8. A finalizer interruption cannot skip `StopRuntimeSession`; stop response status and ID are checked.
9. SDK model checks use a boto3/botocore version that exposes the command operation.
10. Static hook-contract tests reject replacing `ClaudeSDKClient` with one-shot `query()`.
11. A live guard probe requires a successful invocation, `denied_count > 0`, and no protected token in output.

A live smoke test, when explicitly approved, must compare the command-side boot ID/hostname with the application fingerprint from the same session and record cleanup success.

### 8. Wrong vs Correct

#### Wrong: treating the first event as command success

```python
response = client.invoke_agent_runtime_command(...)
print(next(iter(response["stream"])))
# HTTP returned, therefore assume the command passed.
```

This ignores later stderr, event exceptions, timeout status, and non-zero exit codes.

#### Wrong: assuming implicit shell evaluation

```python
command = "printf '%s' '<base64>' | base64 -d | bash"
# The pipe is not guaranteed to be interpreted by a shell.
```

#### Correct

```python
result = parse_every_event(response["stream"])
if response["statusCode"] != 200:
    raise RuntimeError("command API failed")
if result.stop is None or result.stop["status"] != "COMPLETED":
    raise RuntimeError("command did not complete")
if result.stop["exitCode"] != 0:
    raise RuntimeError(f"command exited {result.stop['exitCode']}")
```

The concrete reference implementation is `16-shared-runtime-microvm/scripts/runtime_session.py`; the live behavior is recorded in `16-shared-runtime-microvm/results/REPORT.md` and the raw 2026-08-14 JSON files.
