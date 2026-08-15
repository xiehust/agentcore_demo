# Shared AgentCore microVM Runtime: multi-user concurrency tests

This standalone demo places several cooperative application users inside **one
AgentCore Runtime session** and measures short and long Claude Agent workloads.
It uses `InvokeAgentRuntimeCommand`—not SSM, EC2, ASG, or a managed-host
implementation detail—to sample the active container and verify long-run files.

> Status: a real AWS deployment and billable validation were completed on
> 2026-08-14. The final isolation smoke passed 26/26 checks, short workloads
> passed 14/14 requests at 2/4/8 true concurrency, and two-phase long workloads
> passed 15/15 deterministic end-to-end verifications at 1/2/4/8 concurrency.
> The dedicated test Runtime is removed after validation; the ECR image is
> retained. See the Chinese [`results/REPORT.md`](results/REPORT.md) for raw
> filenames, resource data, evidence limits, and cleanup confirmation.

## What this demo proves—and what it does not

A new random `runtimeSessionId` is generated for each run. Every virtual user
invokes that same session ID and carries a distinct `runtimeUserId` plus
`payload.user_id`. AgentCore therefore routes the run to one dedicated session
microVM; the app multiplexes users inside that container with:

- matching, validated `runtimeUserId` header/payload identity and a hashed
  per-user directory under `/tmp/agentcore-users`;
- a path-checking `PreToolUse` hook (including Glob patterns), workspace
  symlink rejection, and no Bash/Web/Task tools; the app uses bidirectional
  `ClaudeSDKClient` because one-shot `query()` does not execute Python function
  hooks in the pinned SDK;
- a separate Claude conversation ID in each workspace;
- one lock per user (same-user calls serialize, different users can overlap);
- a global Claude-process semaphore.

The **microVM is the isolation boundary between Runtime sessions**. Users put
inside the same session share a container, process trust domain, credentials,
and OS user. The path guard is suitable only for a cooperative or weak-threat
model. It does not make mutually untrusted users into separate tenants. Do not
give end users direct AgentCore invocation permission or expose command
execution to user-provided text.

Default Runtime session storage is ephemeral. `/tmp/agentcore-users` and test
artifacts disappear when the session stops, idles out, or reaches its compute
lifecycle limit. Runtime compute has an **8-hour maximum lifecycle**; this demo
does not configure external persistence or a Capacity Provider filesystem.

## Layout

```text
16-shared-runtime-microvm/
├── app/
│   ├── isolation.py
│   └── server.py
├── docker/Dockerfile
├── scripts/
│   ├── runtime_session.py
│   ├── deploy.sh
│   ├── cleanup.sh
│   ├── invoke_multiuser.py
│   ├── load_test.py
│   └── load_test_longrun.py
├── tests/
├── results/REPORT.md
├── pyproject.toml
├── uv.lock
└── README.md
```

Direct dependencies are exact-pinned and the complete Python resolution is
frozen in `uv.lock`. In particular, `boto3==1.42.59` contains
`invoke_agent_runtime_command`, `invoke_agent_runtime`, and
`stop_runtime_session`. The image uses exact Node.js `22.19.0`, Claude Code
`2.1.232`, and uv `0.8.13` image tags, then installs with `uv sync --frozen`.

## Architecture

```text
alice ─┐  runtimeUserId=alice                    one active Runtime session
bob   ─┼─ InvokeAgentRuntime ─────────────────► dedicated AgentCore microVM
carol ─┘  runtimeSessionId=<same random ID>       ├─ FastAPI process
                                                  ├─ /tmp/agentcore-users/*
operator ─ InvokeAgentRuntimeCommand ────────────► ├─ /proc + cgroup sampler
                                                  └─ deterministic verifier
```

The command API operates in the same active session filesystem and environment
as the app. It does **not** implicitly evaluate shell syntax: code-owned scripts
are base64-transported and run through an explicit `/bin/bash -c` wrapper. No
prompt or user-controlled shell is accepted.

## Prerequisites

- Docker with `linux/arm64` build support.
- AWS CLI v2 configured for the deployment account and region.
- Python 3.13 and uv (recommended) for clients.
- An AgentCore Runtime execution role (`ROLE_ARN`) that can pull the image and
  invoke the configured Bedrock model.
- A Runtime created/redeployed after 2026-03-17, because command execution is
  automatic for new runtimes but older runtimes require redeployment.

Install the exact local dependencies:

```bash
cd 16-shared-runtime-microvm
uv sync --frozen
```

Local unit tests use no AWS credentials and make no network requests.

## IAM

### Test-runner principal

Scope `Resource` to the generated Runtime ARN where your IAM setup permits it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:InvokeAgentRuntimeForUser",
        "bedrock-agentcore:InvokeAgentRuntimeCommand",
        "bedrock-agentcore:StopRuntimeSession"
      ],
      "Resource": "arn:aws:bedrock-agentcore:REGION:ACCOUNT:runtime/RUNTIME_ID"
    }
  ]
}
```

`InvokeAgentRuntimeCommand` can inspect the session filesystem and available
credentials. Keep it on a restricted diagnostic/test role. Do not grant it to
untrusted callers.

### Deployment principal

`scripts/deploy.sh` also needs control-plane create/update/get/list Runtime
permissions, ECR repository/image push permissions, `iam:PassRole` for
`ROLE_ARN`, and `sts:GetCallerIdentity`. `scripts/cleanup.sh` needs
`bedrock-agentcore:DeleteAgentRuntime`; optional ECR image deletion needs
`ecr:BatchDeleteImage`.

The Runtime execution role itself needs the normal AgentCore image-pull/logging
permissions and permission to invoke the chosen Bedrock model. Follow your
organization's existing execution-role policy rather than copying an account-
wide role into this demo.

## Deploy

This deployment path was exercised against AWS on 2026-08-14. To reproduce it,
set an execution role and run:

```bash
cd 16-shared-runtime-microvm
ROLE_ARN='arn:aws:iam::ACCOUNT:role/YOUR_AGENTCORE_RUNTIME_ROLE' \
REGION=us-west-2 \
bash scripts/deploy.sh
```

Useful overrides:

```bash
ROLE_ARN="$ROLE_ARN" \
RUNTIME_NAME=shared_runtime_microvm \
REPO=launchpad-agents \
TAG=shared-runtime-microvm-v1 \
MODEL_ID=us.anthropic.claude-sonnet-4-6 \
MAX_PARALLEL_AGENTS=8 \
MAX_TURNS=64 \
bash scripts/deploy.sh
```

The script builds/pushes an ARM64 image, creates or updates a normal Runtime,
waits for `READY`, and atomically writes `runtime.json`. It deliberately omits
`capacityProviderConfiguration` and `filesystemConfigurations`. It refuses to
convert an existing Capacity Provider Runtime with the same name.

## Test 1: multi-user smoke

```bash
uv run python scripts/invoke_multiuser.py
```

The smoke test:

1. warms one new shared session;
2. runs a command in that exact session and compares `/proc` boot ID and
   hostname with the application fingerprint;
3. concurrently creates and reads a unique token for at least three users;
4. confirms all app responses use one complete process fingerprint (boot ID,
   run ID, PID, and hostname) but distinct workspaces;
5. attempts relative and absolute cross-workspace reads;
6. resumes each user's Claude conversation and checks token recall with no
   foreign token;
7. calls `StopRuntimeSession` in `finally`.

Options:

```bash
uv run python scripts/invoke_multiuser.py \
  --users alice,bob,carol \
  --request-timeout 900 \
  --results-dir results
```

## Test 2: short concurrency ramp

Start conservatively:

```bash
uv run python scripts/load_test.py \
  --levels 2,4,8 \
  --success-floor 0.80 \
  --request-timeout 900 \
  --monitor-duration 3600
```

Each level barrier-releases N fresh users. Every request must return exactly its
unique marker, a fresh Claude session, a workspace unique within the level, and
the complete warmup process fingerprint. The result records success/failure,
p50/p90/max latency, errors, workspaces, Claude session metadata, and distinct
process count.

After warmup, a single detached Python sampler is started through
`InvokeAgentRuntimeCommand`. Every two seconds it reads:

- CPU deltas from `/proc/stat`;
- used/available memory from `/proc/meminfo`;
- load1 from `/proc/loadavg`;
- `node`/`claude` process count from `/proc/*/comm`;
- cgroup v2 `memory.current` and `memory.max` when readable.

The sampler lives under `/tmp/agentcore-loadtest/<run-id>/`; cleanup reads the
recorded PID and validates its command line before signaling it. No broad
`pkill` is used.

## Test 3: two-phase long concurrency ramp

A one-user smoke is recommended before any wider, potentially expensive run:

```bash
uv run python scripts/load_test_longrun.py \
  --levels 1 \
  --success-floor 0.75 \
  --request-timeout 1800 \
  --monitor-duration 7200
```

Only after reviewing that result should an operator explicitly choose higher
levels:

```bash
uv run python scripts/load_test_longrun.py --levels 2,4,8
```

Each user performs:

1. **foundation**: creates four offline project files;
2. **final QA**: resumes phase 1, adds two files, and fixes all six.

The app evidence is atomically checkpointed **before** command verification.
A deterministic Python verifier then runs inside the same active microVM and
requires exactly:

```text
index.html  about.html  styles.css  app.js  README.md  loadtest.json
```

It rejects symlinks, missing/extra entries, undersized files, broken HTML/CSS/JS
references, missing 480px/768px rules, absent `localStorage`/`textContent`, any
`innerHTML`, a wrong run token, and manifest key/value/file-set mismatches.
Agent self-report and artifact-verified success are separate fields.

## Command and failure semantics

`scripts/runtime_session.py` is the only command/invocation implementation used
by both load tests. Command success requires all of:

- command API HTTP status `200`;
- the response confirms the requested `runtimeSessionId`;
- exactly one `contentStart`, followed by zero or more stdout/stderr
  `contentDelta` events and one `contentStop`;
- no stream exception, unknown event, malformed chunk, duplicate, or
  out-of-order content event;
- `contentStop.status == "COMPLETED"`;
- `contentStop.exitCode == 0`.

The envelope matches boto3's official `chunk` event union. Only HTTP 409
`RetryableConflictException` provisioning/teardown conflicts are retried, with
finite exponential backoff. Other errors fail immediately.

Every completed load level is atomically written to JSON. A monitoring or
artifact-command failure cannot erase agent responses: results expose
`monitor_available`, `monitor_error(s)`, `verification_available`, and
`verification_error`. Missing verification is never presented as verified
success.

## Cleanup and cost control

All three clients call `StopRuntimeSession` from `finally` by default. The two
load clients share a finalizer that reaches session cleanup even if monitor
finalization is interrupted. Stop success requires HTTP 200 and the exact
requested session ID. This avoids waiting for idle timeout and continuing to
pay for unused compute.

For exceptional debugging only:

```bash
STOP_SESSION=0 uv run python scripts/load_test.py --levels 1
# equivalent: --keep-session
```

This prints and records a warning. The session can remain billable until idle
or the 8-hour lifecycle limit. Stop it explicitly as soon as debugging ends.

Delete the deployed Runtime when finished:

```bash
bash scripts/cleanup.sh
```

The ECR image and IAM role are retained by default. To delete only the tagged
image too:

```bash
DELETE_ECR_IMAGE=1 bash scripts/cleanup.sh
```

## Local validation

```bash
uv lock --check
uv sync --frozen
python3 -m compileall -q app scripts tests
python3 -m unittest discover -s tests -v
for file in scripts/*.sh; do bash -n "$file"; done
python3 scripts/invoke_multiuser.py --help
python3 scripts/load_test.py --help
python3 scripts/load_test_longrun.py --help
uvx --from ruff==0.12.11 ruff check app scripts tests
uvx --from ruff==0.12.11 ruff format --check app scripts tests
uvx --from pyright==1.1.411 pyright
```

These checks validate code, parsers, mocked event streams, isolation paths,
monitor CSV statistics, atomic writes, and the six-file verifier. The final
2026-08-14 gate passed **45/45 unit tests**, Ruff check/format, Pyright with zero
errors or warnings, shell syntax, all three CLI help paths, server import, and
`git diff --check`. Local checks alone do not establish cloud capacity; the live
AWS evidence and its limits are recorded in `results/REPORT.md`.

## Reading results

Generated JSON is ignored by Git and stored under `results/` by default. Do not
infer a concurrency recommendation from request count alone: the app semaphore
may queue excess users, and task duration changes process residency. Report
both configured `MAX_PARALLEL_AGENTS` and request concurrency, and distinguish:

- agent marker success;
- session/workspace/fingerprint contract success;
- artifact-verified success;
- resource-monitor availability.

The EC2 Capacity Provider figures in `15-shared-runtime-instance/` are reference
data for a different compute topology. They must not be copied or relabeled as
microVM results.
