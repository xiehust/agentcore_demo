# Design: microVM shared Runtime concurrency test

## 1. Scope and layout

Create a standalone sibling demo under `16-shared-runtime-microvm/`. It intentionally mirrors the externally useful structure of `15-shared-runtime-instance/`, but replaces all Capacity Provider/SSM/EC2 behavior with default AgentCore microVM sessions and the supported command API.

```text
16-shared-runtime-microvm/
├── app/{server.py,isolation.py}
├── docker/Dockerfile
├── scripts/
│   ├── runtime_session.py
│   ├── deploy.sh
│   ├── cleanup.sh
│   ├── invoke_multiuser.py
│   ├── load_test.py
│   └── load_test_longrun.py
├── tests/{test_isolation.py,test_runtime_session.py}
├── results/REPORT.md
├── pyproject.toml
├── README.md
└── .gitignore
```

## 2. Runtime topology

A test run creates one random `runtimeSessionId`. The first `InvokeAgentRuntime` warms and activates its dedicated microVM. Every virtual user then invokes the same Runtime ARN and session ID while carrying a distinct `runtimeUserId`. AgentCore routes all calls to the same microVM/session; the app creates a workspace and Claude conversation state per user.

The deploy script creates a normal Runtime by omitting `capacityProviderConfiguration` and capacity-provider filesystems. Workspaces use `/tmp/agentcore-users`; they are deliberately ephemeral and disappear when the session terminates.

## 3. Application contract

The FastAPI `/invocations` contract and SSE events remain compatible with demo 15. Isolation is application-level inside the shared session: strict user IDs, hashed workspaces, path guards, per-user locks, per-user Claude session metadata, and a global agent semaphore. The server fingerprint includes boot ID, process run ID, PID, and hostname.

This does not turn users sharing one session into independent microVM tenants. The microVM is the boundary between sessions; users inside it share a container/process trust domain.

## 4. Client utility

`scripts/runtime_session.py` owns duplicated mechanics:

- load and validate `runtime.json`;
- create a configured `bedrock-agentcore` client;
- parse app SSE;
- call `InvokeAgentRuntimeCommand` and fold `contentStart`, `contentDelta`, `contentStop`, and exception events into a deterministic result;
- retry only retryable 409 conflicts with bounded backoff;
- run base64-encoded bash scripts without quoting ambiguity;
- start/stop/read a session-local Python monitor;
- parse monitor CSV and compute per-window statistics;
- atomically write JSON checkpoints;
- stop the runtime session and validate the returned ID/status.

A command is successful only when the HTTP status is 200, no stream exception occurred, `contentStop.status == COMPLETED`, and `exitCode == 0`. Missing `contentStop` is a failure.

## 5. Resource monitor

After warmup, a short command writes a Python sampler to a run-specific `/tmp/agentcore-loadtest/<run-id>/` directory and starts it under `nohup`. Every two seconds it reads:

- CPU utilization from deltas in `/proc/stat`;
- used/available memory from `/proc/meminfo`;
- load1 from `/proc/loadavg`;
- `node`/`claude` process count from `/proc/*/comm`;
- cgroup v2 `memory.current` and `memory.max` when readable.

Later commands read the CSV while the sampler continues. Final collection stops the recorded PID and reads the last checkpoint. The run-specific directory prevents one test from killing another.

## 6. Test flows

### Multi-user smoke

Warm up, command-probe the same session, concurrently write/read unique tokens for three users, attempt cross-workspace reads, verify per-user memory, compare fingerprints, save JSON, then stop in `finally`.

### Short load

For each configurable level, use a barrier to release N users into one shared session. Each user starts a fresh app-level Claude session and requests an exact marker. Save request results immediately, then fetch monitor data and attach window statistics. Stop when success falls below the threshold.

### Long load

Each user completes foundation and final-QA invocations. Phase 2 must resume phase 1. After each level, command execution runs an embedded deterministic Python verifier inside the same container against each returned workspace. It validates six files, sizes, references, responsive breakpoints, safe DOM writes, the unique token, and manifest fields. Agent results are checkpointed before command verification so command failure cannot erase evidence.

## 7. Lifecycle and failures

All top-level scripts wrap cloud work in `try/finally`. Unless `STOP_SESSION=0`, finalization best-effort stops monitoring and calls `StopRuntimeSession`. A failed stop makes the script fail or records a cleanup error. `STOP_SESSION=0` is for debugging only and leaves the microVM billable until idle timeout.

Command monitoring/verification failures do not rewrite agent success as verified success. Results explicitly expose `monitor_error`, `verification_available`, and cleanup status.

## 8. Validation strategy

Local tests mock event-stream payloads and exercise parsing, command success/failure, CSV statistics, session IDs, user/path isolation, atomic JSON writes, explicit `/bin/bash -c` shell transport, and the `ClaudeSDKClient` hook contract. Final validation passed compileall, shell syntax, 45/45 unit tests, Ruff, and Pyright. The authorized cloud run produced the raw JSON cited in `results/REPORT.md`; every recorded session stopped successfully and the dedicated Runtime was deleted from the control plane.

## 9. Authorized cloud execution and reporting

The approved execution uses account `434444145045` in `us-west-2`, the existing AgentCore execution role `AmazonBedrockAgentCoreSDKRuntime-us-west-2-6b8cf5ef59`, Runtime name `shared_runtime_microvm`, and image tag `shared-runtime-microvm-v1`. Deployment must prove the resulting Runtime has neither `capacityProviderConfiguration` nor `filesystemConfigurations`.

Run order is isolation smoke, short levels 2/4/8, long level 1, then long levels 2/4. Long level 8 is conditional on 100% verified success, command-monitor availability, successful session cleanup, and adequate memory headroom at level 4. Every run uses a fresh session and must record a successful stop response.

After raw JSON analysis, rewrite `results/REPORT.md` in Chinese with UTC timestamps, Runtime version/image/model, exact commands, result filenames, success/latency/resource tables, uncertainty, and comparison methodology. Delete the dedicated test Runtime after report completion; retain the ECR image unless the user asks to delete it.
